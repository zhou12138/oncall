# OnCall-Agent Development Backlog

> Generated: 2026-05-10  
> Codebase: `oncall_agent/` — v0.2.4, ~3,100 LOC, Python 3.10+  
> Architecture: FastAPI HTTP API → Intent extraction → 3-step pipeline (Triage/WoW/Reason) → MCP backends → Teams notification

---

## How to Read This Backlog

Each item has:
- **Priority** — P0 (blocking) / P1 (high) / P2 (medium) / P3 (nice-to-have)
- **Effort** — XS (<2h) / S (2–4h) / M (4–8h) / L (1–2d) / XL (2–5d)
- **Area** — Bug / Feature / Refactor / Test / DevEx / Infra
- **Files affected** — specific file paths

---

## P0 — Blocking / Critical

### BKL-001 · Windows compatibility: `fcntl` missing on non-Linux platforms
**Priority:** P0 · **Effort:** S · **Area:** Bug  
**File:** `oncall_agent/memory/store.py:51–53, 193–213`

`OncallMemory.add()` uses `fcntl.flock()` for exclusive file locking. `fcntl` does not exist on Windows, so the conditional import at the top (`try: import fcntl except ImportError: fcntl = None`) correctly skips it — but **the lock is then silently skipped**. In a concurrent Windows deployment (or WSL2 without `fcntl`), two FastAPI worker tasks running simultaneously can corrupt `memory.json`.

**Fix:**
- Replace `fcntl` with `filelock` (cross-platform) or Python's `threading.Lock()` for in-process concurrency + `msvcrt.locking` for Windows file locking.
- Alternatively, enforce single-writer via an async `asyncio.Lock` at the `OncallMemory` instance level (since the HTTP server is single-process async).

```python
# Minimal fix: asyncio.Lock for in-process safety
class OncallMemory:
    def __init__(self, ...):
        self._write_lock = asyncio.Lock()
    async def add_async(self, section, entry):
        async with self._write_lock:
            ...
```

---

### BKL-002 · Memory eviction bug: `_evict_oldest` doesn't account for entry to be added
**Priority:** P0 · **Effort:** XS · **Area:** Bug  
**File:** `oncall_agent/memory/store.py:159–167`

`_evict_oldest(section, needed_chars)` loops while `_section_char_count(section) + needed_chars > limit` — this correctly computes remaining capacity. However, **it only reloads the section data before computing**, not after eviction. In a heavily concurrent scenario, after eviction the file may be re-read with stale data mid-loop. The deeper issue is that the section char count is recomputed from the in-memory `self.data` list, which after `_reload_under_lock()` may be stale if the disk was written concurrently. 

Additionally, if a **single new entry is larger than the entire section limit**, the while loop evicts everything in the section (empties it) but still cannot fit the new entry — the entry is then added anyway, immediately overflowing the limit.

**Fix:** Add a guard after eviction: if `needed_chars > limit`, log a warning and skip the write rather than adding an oversized entry.

---

### BKL-003 · `_run_pipeline` background task has no timeout
**Priority:** P0 · **Effort:** S · **Area:** Bug  
**File:** `oncall_agent/api.py:128–185`

`asyncio.create_task(_run_pipeline(run_id, req))` is fire-and-forget with no timeout. A single slow ADX query or LLM hang will hold the task forever, leaking memory in `_runs`. Under load, this accumulates indefinitely.

**Fix:**
```python
asyncio.create_task(asyncio.wait_for(_run_pipeline(run_id, req), timeout=300))
```
Or wrap with a structured timeout inside `_run_pipeline` and record `status="timeout"` in `_runs`.

---

### BKL-004 · `_runs` in-memory store grows without bound
**Priority:** P0 · **Effort:** S · **Area:** Bug  
**File:** `oncall_agent/api.py:66–70`

`_runs: dict[str, dict] = {}` is a module-level dict that is never evicted. Each pipeline run appends to it (including full result payloads). A busy deployment receiving 100+ incidents/day will exhaust memory over time.

**Fix:**
- Cap to last N runs with an `OrderedDict` + eviction: `if len(_runs) > MAX_RUNS: _runs.popitem(last=False)`.
- Or persist to SQLite/Redis for Phase 2 durability. Document the current limitation clearly.

---

### BKL-005 · `log_enricher.py` uses f-string interpolation into KQL (not parameterized)
**Priority:** P0 · **Effort:** XS · **Area:** Bug / Security  
**File:** `oncall_agent/ingestion/log_enricher.py:28–36`

```python
_LOG_QUERY_TEMPLATE = """
let SignalName = '{signal_name}';
...
"""
query = _LOG_QUERY_TEMPLATE.format(signal_name=safe_signal, time_window=safe_window)
```

Even though `sanitize_signal_name()` is called before interpolation, the f-string approach is architecturally inconsistent with `step1_triage.py` and `step2_wow.py` which use **parameterized queries** via `{"parameters": {"p_SignalName": signal_name}}`. The enricher should use the same pattern.

**Fix:** Rewrite to use declare/parameterized query like step1 and step2.

---

## P1 — High Priority

### BKL-006 · No integration tests for MCP provider path
**Priority:** P1 · **Effort:** M · **Area:** Test  
**Files:** `tests/`, `oncall_agent/providers.py`, `oncall_agent/steps/`

All existing tests (`test_memory.py`, `test_incident.py`, `test_parsing.py`, `test_sanitize.py`) test individual units. There are **zero integration tests** for:
- The `MCPProvider` path (`step1_triage`, `step2_wow`, `step3_reason` with mock MCP servers)
- The full `OncallOrchestrator.run()` pipeline end-to-end
- The `/trigger` → `/runs/{run_id}` async polling flow via `httpx.AsyncClient` + FastAPI `TestClient`

**Fix:** Add `tests/test_integration.py` using:
```python
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch
```
Cover: happy path, step failure mid-pipeline, timeout, ICM webhook ingestion.

---

### BKL-007 · No rate limiting on `/trigger` endpoint
**Priority:** P1 · **Effort:** S · **Area:** Feature / Security  
**File:** `oncall_agent/api.py`

The `/trigger` and `/webhooks/icm` endpoints accept unlimited concurrent requests. A misconfigured alert loop or external flood can spawn thousands of LLM + MCP calls, exhausting API quotas and memory.

**Fix:** Add per-IP or global rate limiting using `slowapi` (FastAPI-native):
```python
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
limiter = Limiter(key_func=get_remote_address)
@app.post("/trigger")
@limiter.limit("10/minute")
async def trigger_oncall(...):
```

---

### BKL-008 · `copilot_proxy.py` uses hard-coded VS Code client ID
**Priority:** P1 · **Effort:** XS · **Area:** Feature  
**File:** `oncall_agent/copilot_proxy.py:24`

```python
GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"  # VS Code Copilot client ID
```

The proxy impersonates the VS Code extension. This is fragile: GitHub could revoke/rotate the client ID. The agent should support configuring an **OAuth App client ID** from `~/.oncall/config.json`, enabling organizations to register their own Copilot-compatible OAuth app.

**Fix:** Move to config: `llm.github_client_id` with the VS Code ID as default. Add `oncall onboard` step to optionally configure a custom client ID.

---

### BKL-009 · `OncallMemory.recall()` is O(N) linear scan — no index
**Priority:** P1 · **Effort:** M · **Area:** Performance  
**File:** `oncall_agent/memory/store.py:220–250`

The Jaccard-based recall does a full linear scan of all incidents on every Step 3 call. At 4,000 chars per section with ~50-char entries, that's ~80 incidents. Fine now, but if the limit is raised or sections are extended, this will degrade.

**Fix (short-term):** Acceptable as-is under current limits. Document in code.  
**Fix (long-term):** Pre-compute token sets on load, maintain an inverted index `token → [entry_indices]` for O(k × bucket_size) lookup. No external dependencies needed.

---

### BKL-010 · `step3_reason.py` — LLM response parsing is fragile (markdown stripping)
**Priority:** P1 · **Effort:** XS · **Area:** Bug  
**File:** `oncall_agent/steps/step3_reason.py:87–103`

```python
if text.startswith("```"):
    text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
```

This strips only leading ` ``` ` but will fail for:
- ` ```json\n{...}\n``` ` (language specifier on first line)
- Multiple code fences in the response
- Non-leading ` ``` ` blocks

The intent extraction in `api.py` correctly uses `response_format={"type": "json_object"}` to get clean JSON. `step3_reason` should do the same.

**Fix:** Pass `response_format={"type": "json_object"}` to the step3 LLM call (already done for intent extraction — use the same pattern). Remove the brittle markdown stripping.

---

### BKL-011 · `workspace.append_memory()` has off-by-one in comment-block skipping
**Priority:** P1 · **Effort:** XS · **Area:** Bug  
**File:** `oncall_agent/workspace.py:103–116`

The loop that skips `<!-- ... -->` comment blocks increments `insert_idx` before checking if the current line ends the comment:
```python
while insert_idx < len(lines) and lines[insert_idx].strip().startswith("<!--"):
    insert_idx += 1
    while insert_idx < len(lines) and "-->" not in lines[insert_idx - 1]:
        insert_idx += 1
```
The inner `while` checks `lines[insert_idx - 1]` (just-passed line) rather than `lines[insert_idx]` (current). This means the loop can advance one too many lines, inserting the timestamp entry **after** the first real content line of the section instead of before it.

**Fix:** Rewrite using a cleaner state machine or just scan for `-->` terminator correctly.

---

### BKL-012 · `routing.py` — static keyword routing with no fallback strategy
**Priority:** P1 · **Effort:** S · **Area:** Feature  
**File:** `oncall_agent/routing.py`

The routing table is hard-coded with 10 keywords. There's no way to:
- Configure routes at runtime without code changes
- Add regex patterns (e.g., `"Edge.*Crash"` → `"client-team"`)
- Support workspace-specific routing overrides
- Log routing decisions with signal name for traceability

**Fix:** Load routes from `~/.oncall/config.json` under `routing.rules`. Fall back to the hard-coded defaults. Add a `route_incident()` log line at INFO level with the matched keyword and owner.

---

### BKL-013 · No `/runs` list endpoint — can only poll individual runs
**Priority:** P1 · **Effort:** XS · **Area:** Feature  
**File:** `oncall_agent/api.py`

`GET /runs/{run_id}` exists, but there is no `GET /runs` to list all active/recent runs. The `/incidents` endpoint lists incidents (from the orchestrator registry) but not the full run status stored in `_runs`.

**Fix:** Add `GET /runs?status=running|completed|failed&limit=50` that returns the `_runs` dict filtered/paged. Reuse `RunStatusResponse`.

---

### BKL-014 · Authentication token (X-API-Key) comparison is not constant-time
**Priority:** P1 · **Effort:** XS · **Area:** Security  
**File:** `oncall_agent/api.py:49–57`

```python
if not presented or presented != expected:
```

String comparison with `!=` is not constant-time and is vulnerable to timing attacks. Although this is low-risk for an internal service, it's a security best practice issue.

**Fix:**
```python
import hmac
if not hmac.compare_digest(presented.encode(), expected.encode()):
```

---

## P2 — Medium Priority

### BKL-015 · Add `oncall trigger` CLI subcommand
**Priority:** P2 · **Effort:** S · **Area:** DevEx  
**File:** `oncall_agent/cli.py`

The CLI has `oncall chat` (TUI) and `oncall serve` (API server) but no direct way to fire a pipeline from the command line without starting the full API server. Power users and CI pipelines want:
```bash
oncall trigger "High CPU in WestUS2 region" --repo Microsoft/Edge
```

**Fix:** Add `cli.py` command `trigger` that calls `OncallOrchestrator().run()` directly (without HTTP), prints the result as JSON or formatted Rich output. Include `--workspace`, `--model`, `--channel` flags.

---

### BKL-016 · `CopilotProxy` — no retry on 429 rate limit responses
**Priority:** P2 · **Effort:** S · **Area:** Reliability  
**File:** `oncall_agent/copilot_proxy.py:170–200, 210–240`

`chat_completion()` and `chat_completion_stream()` do not retry on HTTP 429 (Too Many Requests) from the Copilot API. A single rate-limited request immediately raises an exception, failing the entire pipeline run.

**Fix:** Add exponential backoff retry (3 attempts, respect `Retry-After` header if present) for 429 responses, consistent with `MCPClient`'s retry logic in `mcp_clients/client.py`.

---

### BKL-017 · `step2_wow.py` — GitHub recent changes query hits ADX, not GitHub MCP
**Priority:** P2 · **Effort:** XS · **Area:** Bug  
**File:** `oncall_agent/steps/step2_wow.py:59–72`

```python
gh_result = await adx_client.call_tool("execute_query", {
    "query": GITHUB_RECENT_CHANGES_QUERY,
    ...
})
```

The `GITHUB_RECENT_CHANGES_QUERY` queries a table `GitHubMetrics` — presumably via ADX. But the `github_client` MCP (passed in the function signature) is never used. Either:
1. This is intentional (GitHub data is mirrored to ADX) and the parameter naming is misleading.
2. This is a bug — it should use `github_client.call_tool("list_recent_prs", {...})`.

**Fix:** Either rename `github_client` to `_unused` and add a comment, or fix to use the GitHub MCP client with an appropriate tool call. Add a test that verifies which client is called.

---

### BKL-018 · Missing `__all__` in `oncall_agent/steps/__init__.py` and other `__init__.py` files
**Priority:** P2 · **Effort:** XS · **Area:** DevEx  
**File:** `oncall_agent/steps/__init__.py`, `oncall_agent/connectors/__init__.py`, `oncall_agent/ingestion/__init__.py`, `oncall_agent/cards/__init__.py`

All `__init__.py` files are empty or near-empty. There are no `__all__` declarations, no re-exports. This makes the package harder to use as a library and IDE autocomplete less effective.

**Fix:** Define `__all__` and re-export key symbols in each `__init__.py`.

---

### BKL-019 · No health check for MCP server connectivity
**Priority:** P2 · **Effort:** S · **Area:** Feature  
**File:** `oncall_agent/api.py:222–226`, `oncall_agent/mcp_clients/client.py`

`GET /health` returns `{"status": "ok"}` but does not verify MCP server connectivity. A deployment where ADX/Teams/GitHub MCP servers are unreachable will return healthy but fail on every pipeline run.

**Fix:** Add `GET /health/ready` that attempts `MCPClient.list_tools()` on each configured server and reports per-server status. Keep `/health` as a liveness probe (fast) and `/health/ready` as a readiness probe.

---

### BKL-020 · `onboard.py` wizard has no validation of MCP server URLs
**Priority:** P2 · **Effort:** S · **Area:** DevEx  
**File:** `oncall_agent/onboard.py`

The onboarding wizard collects MCP server URLs but doesn't validate them (no ping/connectivity check). A user entering a wrong URL will only discover it when they trigger a pipeline.

**Fix:** After collecting each MCP URL, attempt `MCPClient(name, url).initialize()` with a short timeout (5s) and show a green check or red X. Allow the user to retry or skip.

---

### BKL-021 · `OncallMemory` section limits are not configurable at runtime
**Priority:** P2 · **Effort:** XS · **Area:** Feature  
**File:** `oncall_agent/memory/store.py:20–28`, `oncall_agent/config.py`

`DEFAULT_SECTION_LIMITS` and `TOTAL_CHAR_LIMIT` are module-level constants. They can be passed to `OncallMemory.__init__()` but `config.py` has no `memory.section_limits` or `memory.total_limit` settings. Users cannot tune memory capacity without editing source.

**Fix:** Add to `Config`: `memory_section_limits: dict = ...` and `memory_total_limit: int = 10000`. Pass to `OncallMemory` in the orchestrator and API.

---

### BKL-022 · `adaptive.py` card uses `Action.OpenUrl` for Ack/Escalate — should use `Action.Http`
**Priority:** P2 · **Effort:** XS · **Area:** Bug  
**File:** `oncall_agent/cards/adaptive.py:64–76`

The Adaptive Card actions use `Action.OpenUrl` which opens a browser tab when clicked in Teams. The actual action endpoint `POST /actions/{run_id}/ack` requires an HTTP POST, not a browser navigation. Users clicking "Acknowledge" in Teams will just open a URL in their browser, not actually call the API.

**Fix:** Use `Action.Http` (Teams-specific Adaptive Card action) with `method: "POST"` and `url: "{base_url}/ack"`. Note: `Action.Http` requires the bot framework or connector URL to be public-facing.

---

### BKL-023 · `tui.py` — `oncall: <signal>` trigger path blocks the event loop during pipeline
**Priority:** P2 · **Effort:** S · **Area:** Bug  
**File:** `oncall_agent/tui.py`

The TUI's `oncall:` command triggers `await self.orchestrator.run(...)` inline in the prompt-toolkit event loop. During LLM streaming (which can take 15–60s), the terminal is unresponsive — no keyboard input, no spinner updates, no cancellation.

**Fix:** Run the orchestrator in a background `asyncio.Task`, update the spinner from a periodic callback, and allow `Ctrl+C` to cancel.

---

### BKL-024 · No `pytest` CI configuration — tests must be run manually
**Priority:** P2 · **Effort:** XS · **Area:** Infra  
**Files:** `pyproject.toml`, `.github/workflows/` (missing)

`pyproject.toml` lists `pytest` and `pytest-asyncio` as dev deps but has no `[tool.pytest.ini_options]` section. There is no CI workflow file. Running `pytest` without configuration will miss async tests (need `asyncio_mode = "auto"`).

**Fix:**
1. Add to `pyproject.toml`:
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```
2. Add `.github/workflows/ci.yml` with `pytest` + `ruff check` on push/PR.

---

### BKL-025 · `config.py` — no validation that LLM API base URL is reachable
**Priority:** P2 · **Effort:** XS · **Area:** DevEx  
**File:** `oncall_agent/config.py:89–93`

`Config.validate()` only checks `llm_api_key` presence. It doesn't validate `llm_api_base` is a valid URL, or that the configured MCP server URLs are well-formed. A misconfigured `llm_api_base` (e.g., `"localhost:8080"` missing scheme) will produce a confusing error at runtime.

**Fix:** Add URL validation using `urllib.parse.urlparse()` in `Config.validate()`. Check scheme is `http` or `https`, host is non-empty.

---

### BKL-026 · Intent extraction prompt does not handle non-English incident text
**Priority:** P2 · **Effort:** S · **Area:** Feature  
**File:** `oncall_agent/api.py:90–105` (INTENT_SYSTEM_PROMPT)

The intent extraction prompt does not mention language handling. ICM incidents from global teams may contain Japanese, Chinese, Spanish, or Portuguese text. The LLM will likely still handle it (GPT-4o is multilingual) but `signal_name` extraction may produce non-ASCII identifiers that then fail `sanitize_signal_name()`.

**Fix:** 
1. Add to `INTENT_SYSTEM_PROMPT`: "If the incident is in a non-English language, translate `signal_name` and `description` to English."
2. Consider relaxing `sanitize_signal_name` to allow Unicode letters (`re.compile(r'^[\w.\-\s]+$', re.UNICODE)`).

---

## P3 — Nice to Have / Future

### BKL-027 · Structured logging — switch from `extra={}` dicts to `structlog`
**Priority:** P3 · **Effort:** M · **Area:** Refactor  
**File:** `oncall_agent/logging_config.py`, all files using `logger.info(..., extra={...})`

The project uses Python's standard `logging` with `extra={}` dicts to emit structured log fields. This works but `structlog` provides better ergonomics (context binding, processor pipelines, native JSON output) and is becoming the async Python standard.

**Fix:** Migrate to `structlog` with `structlog.contextvars.bind_contextvars(run_id=rid)` replacing `run_id_scope()`.

---

### BKL-028 · `workspace.py` — `append_memory()` is not thread/async safe
**Priority:** P3 · **Effort:** S · **Area:** Bug  
**File:** `oncall_agent/workspace.py:91–125`

`append_memory()` does a read-modify-write of `memory.md` using plain `open()` with no file locking. Concurrent pipeline runs writing to the same workspace memory file will have race conditions (lost updates).

**Fix:** Use the same atomic write pattern as `OncallMemory._save()` (temp file + `os.replace`) and an advisory file lock.

---

### BKL-029 · Add `GET /memory/search?q=<keyword>` endpoint
**Priority:** P3 · **Effort:** XS · **Area:** Feature  
**File:** `oncall_agent/api.py`

`OncallMemory` has a `search(section, keyword)` method but it's not exposed via the HTTP API. The existing `GET /memory` dumps the entire memory. Adding search would let dashboards and Copilot extensions query past incidents programmatically.

**Fix:** Add `GET /memory/search?q=<keyword>&section=incidents` returning matching entries.

---

### BKL-030 · `step1_triage.py` — `ParseError` message includes raw MCP text (potential data leak)
**Priority:** P3 · **Effort:** XS · **Area:** Security  
**File:** `oncall_agent/steps/step1_triage.py:46–50`

```python
raise ParseError(
    f"triage verdict not found in MCP response: {text[:200]!r}"
)
```

The first 200 chars of the MCP response are included in the exception message. If this exception is caught and surfaced in an API response or a Teams notification, it could leak raw ADX query results (which may contain PII or internal IP addresses).

**Fix:** Log the full text at DEBUG level, but raise the exception with only a generic message: `"triage verdict not found in MCP response (check DEBUG logs)"`.

---

### BKL-031 · `MCPClient` — no connection pool sharing across providers
**Priority:** P3 · **Effort:** M · **Area:** Performance  
**File:** `oncall_agent/mcp_clients/client.py`, `oncall_agent/providers.py`

`MCPProvider` creates three separate `MCPClient` instances, each with its own `httpx.AsyncClient` pool. In the TUI (`tui.py`), a new `OncallOrchestrator()` is created per session, which creates new providers (and new HTTP pools). HTTP connection pools are never explicitly closed (`aclose()` is only in `__aexit__`).

**Fix:** Register `MCPClient.aclose()` as a FastAPI shutdown event (`@app.on_event("shutdown")`). Add a singleton/cached provider at the API level.

---

### BKL-032 · Add `CHANGELOG.md` and semantic versioning discipline
**Priority:** P3 · **Effort:** XS · **Area:** DevEx  

The project is at v0.2.4 in `pyproject.toml` but there is no `CHANGELOG.md`, no git tags for releases, and no release workflow. Contributors don't know what changed between versions.

**Fix:** Create `CHANGELOG.md` (Keep a Changelog format), tag v0.2.4 in git, and add a release step to the CI workflow.

---

### BKL-033 · `ingestion/__init__.py` — `parse_icm_payload` import not re-exported
**Priority:** P3 · **Effort:** XS · **Area:** DevEx  
**File:** `oncall_agent/ingestion/__init__.py`

`api.py` uses `from oncall_agent.ingestion import parse_icm_payload, verify_icm_signature`. These are defined in `oncall_agent/ingestion/icm_webhook.py` and the `__init__.py` re-exports them. This works but the `__init__.py` doesn't define `__all__`, making it unclear what the public API of the ingestion package is.

**Fix:** Add `__all__ = ["parse_icm_payload", "verify_icm_signature"]` to `oncall_agent/ingestion/__init__.py`.

---

### BKL-034 · `verify_hmac` uses `hmac.new()` — should be `hmac.new()` (check Python version)
**Priority:** P3 · **Effort:** XS · **Area:** Bug  
**File:** `oncall_agent/ingestion/icm_webhook.py:72`

```python
expected = hmac.new(
    secret.encode("utf-8"), payload_bytes, hashlib.sha256
).hexdigest()
```

`hmac.new()` is the correct function (it's an alias for `hmac.HMAC()`), so this works. However, `hmac.new()` was deprecated in Python 3.4 in favor of `hmac.digest()` (Python 3.7+) for one-shot HMAC computation. The current usage constructs an object just to call `.hexdigest()` — `hmac.digest()` is more idiomatic and slightly faster.

**Fix:**
```python
expected = hmac.digest(secret.encode("utf-8"), payload_bytes, "sha256").hex()
```

---

## Backlog Summary

| ID | Title | Priority | Effort | Area |
|----|-------|----------|--------|------|
| BKL-001 | Windows fcntl compatibility / concurrent memory corruption | P0 | S | Bug |
| BKL-002 | Memory eviction bug for oversized entries | P0 | XS | Bug |
| BKL-003 | Background pipeline task has no timeout | P0 | S | Bug |
| BKL-004 | `_runs` dict grows without bound (memory leak) | P0 | S | Bug |
| BKL-005 | log_enricher uses f-string KQL interpolation (not parameterized) | P0 | XS | Security |
| BKL-006 | No integration tests for MCP provider / end-to-end pipeline | P1 | M | Test |
| BKL-007 | No rate limiting on /trigger and /webhooks/icm | P1 | S | Security |
| BKL-008 | Hard-coded VS Code OAuth client ID is fragile | P1 | XS | Feature |
| BKL-009 | `recall()` is O(N) linear scan with no index | P1 | M | Performance |
| BKL-010 | LLM response markdown stripping is fragile in step3 | P1 | XS | Bug |
| BKL-011 | workspace.append_memory() off-by-one in comment skipping | P1 | XS | Bug |
| BKL-012 | Static keyword routing — not configurable or logged | P1 | S | Feature |
| BKL-013 | No `GET /runs` list endpoint | P1 | XS | Feature |
| BKL-014 | API key comparison is not constant-time | P1 | XS | Security |
| BKL-015 | Add `oncall trigger` CLI subcommand | P2 | S | DevEx |
| BKL-016 | CopilotProxy doesn't retry on 429 rate limit | P2 | S | Reliability |
| BKL-017 | step2 GitHub recent changes queries ADX instead of GitHub MCP | P2 | XS | Bug |
| BKL-018 | Missing `__all__` in package `__init__.py` files | P2 | XS | DevEx |
| BKL-019 | No readiness check for MCP server connectivity | P2 | S | Feature |
| BKL-020 | Onboard wizard doesn't validate MCP server URLs | P2 | S | DevEx |
| BKL-021 | Memory section limits not configurable via config.json | P2 | XS | Feature |
| BKL-022 | Adaptive Card uses Action.OpenUrl instead of Action.Http | P2 | XS | Bug |
| BKL-023 | TUI blocks event loop during pipeline execution | P2 | S | Bug |
| BKL-024 | No pytest CI configuration or GitHub Actions workflow | P2 | XS | Infra |
| BKL-025 | Config.validate() doesn't validate URL format | P2 | XS | DevEx |
| BKL-026 | Intent extraction doesn't handle non-English incident text | P2 | S | Feature |
| BKL-027 | Migrate from stdlib logging+extra to structlog | P3 | M | Refactor |
| BKL-028 | workspace.append_memory() not async-safe | P3 | S | Bug |
| BKL-029 | Add GET /memory/search endpoint | P3 | XS | Feature |
| BKL-030 | ParseError includes raw MCP text (potential data leak) | P3 | XS | Security |
| BKL-031 | MCPClient pools not shared or closed on shutdown | P3 | M | Performance |
| BKL-032 | No CHANGELOG or git release tags | P3 | XS | DevEx |
| BKL-033 | ingestion/__init__.py missing `__all__` | P3 | XS | DevEx |
| BKL-034 | verify_hmac uses deprecated hmac.new() | P3 | XS | Bug |

---

## Suggested Sprint Plan

### Sprint 1 — Stability (P0s + critical P1s)
- BKL-001 Windows file locking
- BKL-002 Memory eviction overflow guard
- BKL-003 Pipeline task timeout
- BKL-004 _runs bounded eviction
- BKL-005 Parameterize log_enricher KQL
- BKL-010 Fix step3 JSON parsing (use response_format)
- BKL-014 Constant-time API key comparison

**Estimated effort:** ~1.5 days

### Sprint 2 — Reliability & Observability
- BKL-006 Integration tests
- BKL-007 Rate limiting
- BKL-013 GET /runs list endpoint
- BKL-019 /health/ready MCP connectivity check
- BKL-024 pytest CI + GitHub Actions

**Estimated effort:** ~2 days

### Sprint 3 — Features & Polish
- BKL-012 Configurable routing
- BKL-015 oncall trigger CLI command
- BKL-016 CopilotProxy 429 retry
- BKL-020 Onboard URL validation
- BKL-022 Adaptive Card Action.Http
- BKL-026 Non-English intent handling

**Estimated effort:** ~2 days
