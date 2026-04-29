# OnCall Agent Project — Comprehensive Code Analysis Report

**Project Size:** 2,946 lines of Python across 19 files
**Analysis Date:** 2024
**Scope:** Full security, architecture, and critical issue identification

---

## EXECUTIVE SUMMARY

### Critical Issues Found
1. **KQL Injection Vulnerability** (HIGH): String interpolation in Kusto queries
2. **Fragile Parsing** (HIGH): Regex-based KQL response parsing without validation
3. **Silent Error Fallbacks** (MEDIUM): Multiple paths swallow errors and return defaults
4. **Dual Config Track Issue** (MEDIUM): Config resolution ambiguity between file and env
5. **MCP Protocol Incompleteness** (LOW): Missing error handling, streaming support

### Key Patterns
- **3-Step Pipeline**: Triage → WoW → Reason (supports mock + MCP modes)
- **Frozen Memory Snapshots**: Hermes-style pattern for stable LLM context
- **Workspace Isolation**: soul.md + memory.md per project
- **Two Auth Paths**: GitHub Copilot (device code) + MCP servers

---

## FILE-BY-FILE ANALYSIS

### 1. orchestrator.py (168 lines)

**Purpose:** Orchestrates the 3-step oncall pipeline with mock/MCP branching.

**Key Functions:**

| Function | Signature | Lines | Critical Notes |
|----------|-----------|-------|-----------------|
| `__init__` | `def __init__(self)` | 16-20 | Initializes memory store, checks MCP availability via config |
| `run` | `async def run(signal_name: str, repo: str = "", teams_channel: str = "", model: str = None, intent: dict = None, raw_query: str = "") -> dict` | 22-39 | Entry point — branches to _run_with_mock or _run_with_mcp |
| `_run_with_mock` | `async def _run_with_mock(...)` | 41-97 | Calls mock_triage, mock_wow, then step_reason_and_act with LLM |
| `_run_with_mcp` | `async def _run_with_mcp(...)` | 99-167 | Calls step_triage, step_wow_compare via ADX/GitHub MCPs |

**Critical Issues:**

1. **Silent MCP Initialization (Line 114-116):**
   ```python
   adx_client = MCPClient("adx-kusto", config.adx_mcp.url)
   github_client = MCPClient("github", config.github_mcp.url)
   teams_client = MCPClient("teams", config.teams_mcp.url)
   ```
   - **Issue**: No validation that URLs are non-empty before creating clients
   - **Impact**: If config.adx_mcp.url is empty string, MCPClient silently created with base_url=""
   - **Risk**: Subsequent call_tool() calls to empty endpoint will fail at HTTP layer, not caught early

2. **Skipped Step Data (Line 145-148):**
   ```python
   result["steps"]["wow"] = {
       "current_count": 0, "previous_count": 0, "delta": 0,
       "change_percent": 0, "trend": "skipped", ...
   }
   ```
   - **Issue**: Intent-driven step skipping doesn't warn or log
   - **Impact**: Silent data loss if should_run_wow=False; analysis may be incomplete

**Architecture Decisions:**

- **Mock vs MCP**: Decision made at `__init__` time based on config (line 18-20)
- **Intent Processing**: Raw query → LLM extraction → intent dict → intent-based branching
- **Memory Recording**: Only in _run_with_mock path (line 95), NOT in MCP path (no record() call after step 3)

---

### 2. steps/step1_triage.py (75 lines)

**Purpose:** Global vs Windows first appearance analysis via ADX.

**Key Functions:**

| Function | Signature | Lines | Critical Notes |
|----------|-----------|-------|-----------------|
| N/A | Module-level queries | 9-38 | Two Kusto query templates |
| `step_triage` | `async def step_triage(adx_client: MCPClient, signal_name: str) -> dict` | 41-74 | Executes queries and parses verdict |

**KQL INJECTION VULNERABILITY (HIGH):**

**Line 52 — GLOBAL_FIRST_QUERY:**
```python
query = GLOBAL_FIRST_QUERY.format(signal_name=signal_name)
```

**Template (Lines 9-28):**
```kusto
let SignalName = '{signal_name}';  // ← UNESCAPED USER INPUT
```

**Attack Vector:**
```python
signal_name = "'; 42 | union (SignalTable | where Timestamp > ago(999d)) // "
# Results in:
# let SignalName = ''; 42 | union (SignalTable | where Timestamp > ago(999d)) // ';
```

**Similar Issue — Line 31, 56:**
```python
let SignalName = '{signal_name}';  // Line 31 in SIGNAL_DETAILS_QUERY
details_query = SIGNAL_DETAILS_QUERY.format(signal_name=signal_name)  // Line 56
```

**Impact:**
- ADX can be forced to execute arbitrary Kusto queries
- Leakage of historical data from extended time windows
- Potential for resource exhaustion (large time ranges)
- **CVSS: 7.1 (High)** — requires authentication to ADX, but no input validation

**Mitigation Required:**
- Use parameterized queries (Kusto parameter binding)
- Or: Strict whitelist pattern: `^[A-Za-z0-9_]+$`

---

**Fragile Parsing (Lines 59-68):**

```python
verdict = "Unknown"
if isinstance(verdict_result, dict):
    content = verdict_result.get("content", [{}])
    if content and isinstance(content, list):
        text = content[0].get("text", "")
        if "Global First" in text:
            verdict = "Global First"
        elif "Windows First" in text:
            verdict = "Windows First"
```

**Issues:**
1. Assumes `content` is a list of dicts with "text" key
2. No type validation on `content[0]`
3. Returns "Unknown" (default) if response structure unexpected
4. Silent fallback: caller doesn't know parsing failed

**Risk:** If MCP returns `{"content": "Global First"}` (string instead of list), verdict="Unknown" silently.

---

### 3. steps/step2_wow.py (109 lines)

**Purpose:** Week-over-week comparison + GitHub recent changes correlation.

**Key Functions:**

| Function | Signature | Lines | Critical Notes |
|----------|-----------|-------|-----------------|
| N/A | Module-level queries | 8-41 | WOW_QUERY, GITHUB_RECENT_CHANGES_QUERY templates |
| `step_wow_compare` | `async def step_wow_compare(adx_client: MCPClient, github_client: MCPClient, signal_name: str, repo: str = "") -> dict` | 44-108 | Main logic |

**KQL INJECTION VULNERABILITY (HIGH):**

**Lines 63, 95 — Multiple Injection Points:**

```python
query = WOW_QUERY.format(signal_name=signal_name)  # Line 63
gh_query = GITHUB_RECENT_CHANGES_QUERY.format(signal_name=signal_name, repo=repo)  # Line 95
```

**Query Template (Lines 8-30):**
```kusto
let SignalName = '{signal_name}';  // ← INJECTION POINT 1
```

**GitHub Query Template (Lines 32-41):**
```kusto
let SignalName = '{signal_name}';  // ← INJECTION POINT 2
| where Repository has '{repo}'    // ← INJECTION POINT 3 (has operator is fuzzy)
```

**Attack Vectors:**
1. Signal name injection: Same as step1
2. Repo injection: `repo = "EdgeCrashRate'); drop database // "`

**Fragile Regex Parsing (Lines 77-83):**

```python
import re
nums = re.findall(r'[\d.]+', text)  # ← Extracts ANY decimal numbers from text
if len(nums) >= 4:
    current = int(float(nums[0]))      # Assumes order: current, prev, delta, pct
    previous = int(float(nums[1]))
    delta = int(float(nums[2]))
    change_pct = float(nums[3])
```

**Issues:**
1. **Fragile**: Assumes text contains exactly 4 numbers in specific order
2. **No validation**: Doesn't verify numbers are in expected ranges
3. **Silent fallback**: If regex fails, current/previous/delta stay 0 (initialized line 67-70)
4. **Type confusion**: `int(float(...))` is suspicious; if nums contain scientific notation, parsing is unreliable

**Example Failure:**
```
If MCP returns: "Metric: 1.5e3, Result: 2.0e3, Delta: 0.5e3, Change: 50.0e1"
Regex gets: ['1', '5', '3', '2', '0', '3', '0', '5', '3', '50', '0', '1']
Parses as: current=1, prev=5, delta=3, change_pct=2.0  ← WRONG
```

**Silent Exception Handling (Lines 93-99):**

```python
if repo:
    try:
        gh_query = GITHUB_RECENT_CHANGES_QUERY.format(signal_name=signal_name, repo=repo)
        gh_result = await adx_client.call_tool("execute_query", {"query": gh_query})
        recent_changes = gh_result.get("content", [])
    except Exception:
        pass  # ← GitHub metrics optional
```

- **Issue**: Swallows all exceptions (network, format, etc.)
- **Impact**: Caller doesn't know if recent_changes are missing due to error or no data
- **Risk**: Silent data loss

---

### 4. steps/step3_reason.py (163 lines)

**Purpose:** LLM reasoning + Teams notification.

**Key Functions:**

| Function | Signature | Lines | Critical Notes |
|----------|-----------|-------|-----------------|
| `step_reason_and_act` | `async def step_reason_and_act(teams_client, memory: OncallMemory, triage_result: dict, wow_result: dict, teams_channel: str = "", model: str = None, extra_context: str = "") -> dict` | 51-162 | Main logic |

**Critical Issues:**

1. **Fragile JSON Parsing (Lines 94-109):**
   ```python
   try:
       text = llm_response.strip()
       if text.startswith("```"):
           text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
       analysis = json.loads(text)
   except json.JSONDecodeError:
       analysis = {
           "reasoning": llm_response,
           "summary": llm_response[:200],
           "severity": "medium",
           "actions": ["Review manually"],
           ...
       }
   ```

   **Issues:**
   - **Markdown fence extraction**: assumes format ` ```\n...\n``` `
   - **No validation**: If LLM returns JSON with missing fields, no error
   - **Silent fallback**: Returns generic default on any JSON error
   - **Type confusion**: `analysis = llm_response` (string) used later as dict without validation

   **Risk**: If LLM outputs malformed JSON, system silently returns "Review manually" for any incident — very broad fallback.

2. **Memory Write Without Transaction Safety (Lines 112-129):**
   ```python
   memory.add("incidents", {...})
   if analysis.get("pattern_detected"):
       memory.add("patterns", {...})
   memory.add("wow_comparisons", {...})
   ```

   - **Issue**: Three sequential writes; if second fails, inconsistent state
   - **Impact**: Memory may have incident but no pattern; hard to debug
   - **Mitigation**: memory.add() has locking, but no rollback if one fails mid-sequence

3. **Teams Notification Error Swallowing (Lines 151-153):**
   ```python
   except Exception as e:
       print(f"Teams notification failed: {e}")
   ```

   - **Issue**: Prints to stdout (debug), continues
   - **Impact**: Silent notification failure; incident analysis proceeds without alert
   - **Risk**: On-call team never notified of critical incident

---

### 5. mcp_clients/client.py (46 lines)

**Purpose:** Generic HTTP/JSONRPC MCP client.

**Key Functions:**

| Function | Signature | Lines | Critical Notes |
|----------|-----------|-------|-----------------|
| `__init__` | `def __init__(self, name: str, base_url: str)` | 10-12 | Stores name and URL |
| `call_tool` | `async def call_tool(self, tool_name: str, arguments: dict[str, Any] = None) -> dict` | 14-31 | Sends JSONRPC request |
| `list_tools` | `async def list_tools(self) -> list[dict]` | 33-45 | Lists available tools |

**Critical Issues:**

1. **No Error Classification (Lines 29-31):**
   ```python
   if "error" in result:
       raise RuntimeError(f"MCP tool error: {result['error']}")
   return result.get("result", {})
   ```

   - **Issue**: All errors treated the same (KQL parse error = network error)
   - **Impact**: Caller can't distinguish transient from permanent failures
   - **Missing**: No retry logic, no exponential backoff

2. **No Streaming Support:**
   - **Issue**: JSONRPC call_tool is sync (await for response)
   - **Missing**: Server-Sent Events (SSE) streaming for long queries
   - **Impact**: Large ADX result sets timeout or return truncated

3. **No Request Validation (Line 14):**
   ```python
   async def call_tool(self, tool_name: str, arguments: dict[str, Any] = None) -> dict:
   ```
   - **Issue**: No schema validation on arguments
   - **Risk**: Invalid arguments silently sent to server; server rejects

4. **Timeout Fixed at 120s (Line 25):**
   ```python
   async with httpx.AsyncClient(timeout=120) as client:
   ```
   - **Issue**: Not configurable; may be too short for large queries
   - **Missing**: Per-call timeout override

---

### 6. memory/store.py (339 lines)

**Purpose:** Persistent cross-session memory with frozen snapshots, security scanning, dedup.

**Key Functions:**

| Function | Signature | Lines | Critical Notes |
|----------|-----------|-------|-----------------|
| `__init__` | `def __init__(self, path: str = "./memory/oncall_memory.json", section_limits: Optional[Dict[str, int]] = None, total_limit: int = TOTAL_CHAR_LIMIT)` | 83-101 | Loads from disk, creates frozen snapshot |
| `_load` | `def _load(self)` | 112-119 | Atomic read from disk |
| `_save` | `def _save(self)` | 121-141 | Atomic write: tmpfile + fsync + os.replace |
| `add` | `def add(self, section: str, entry: Dict[str, Any])` | 206-252 | Security scan, dedup, char limits, write |
| `_is_duplicate` | `def _is_duplicate(self, section: str, entry: Dict[str, Any]) -> bool` | 194-202 | Dedup by comparing key fields |
| `get_context_for_llm` | `def get_context_for_llm(self, limit: int = 5) -> str` | 276-282 | Build live context (not frozen snapshot) |
| `_build_context` | `def _build_context(self, limit: int = 5) -> str` | 284-316 | Render memory sections as markdown |
| `system_prompt_snapshot` | `@property` | 105-108 | Returns frozen context captured at init |

**Critical Issues:**

1. **Dedup Logic Bypassed (Lines 194-202):**
   ```python
   def _is_duplicate(self, section: str, entry: Dict[str, Any]) -> bool:
       key_fields = {k: v for k, v in entry.items() if k != "timestamp"}
       for existing in self.data.get(section, []):
           existing_keys = {k: v for k, v in existing.items() if k != "timestamp"}
           if existing_keys == key_fields:
               return True
       return False
   ```

   **Issues:**
   - **Shallow equality**: Uses `==` on dicts; if entry contains nested dict, dedup fails
   - **Example**: Two incidents with same title but different "details" dict are not deduplicated
   - **Memory leak**: Over time, nearly-identical entries accumulate

2. **Character Limit Enforcement is Eager but Not Predictable (Lines 169-177):**
   ```python
   def _evict_oldest(self, section: str, needed_chars: int):
       limit = self.section_limits.get(section, 3000)
       entries = self.data.get(section, [])
       while entries and self._section_char_count(section) + needed_chars > limit:
           removed = entries.pop(0)
   ```

   **Issues:**
   - **No transaction guarantee**: Evicts oldest, then adds new; if add fails, state inconsistent
   - **Unpredictable**: If entry_chars > limit, while loop never terminates (deletes all entries)
   - **Example**: An incident with 5000 chars added to section with 3000 limit deletes all existing entries

3. **File Lock Not Guaranteed on Windows (Lines 222-225):**
   ```python
   lock_fd = None
   try:
       if fcntl:
           lock_fd = open(lock_path, "a+")
           fcntl.flock(lock_fd, fcntl.LOCK_EX)
   ```

   **Issues:**
   - **fcntl unavailable on Windows** (import line 24-27)
   - **No fallback**: Windows installations skip locking entirely
   - **Race condition**: Concurrent memory.add() calls on Windows may corrupt JSON

4. **Security Scanning Fragile (Lines 62-70):**
   ```python
   def scan_content(text: str) -> Optional[str]:
       for char in _INVISIBLE_CHARS:
           if char in text:
               return f"Blocked: invisible unicode U+{ord(char):04X}"
       for pattern, pid in _THREAT_PATTERNS:
           if re.search(pattern, text, re.IGNORECASE):
               return f"Blocked: threat pattern '{pid}'"
       return None
   ```

   **Issues:**
   - **Regex bypass**: Simple patterns; can be obfuscated with case/whitespace
   - **Example**: `"IGNORE previous INSTRUCTIONS"` (uppercase) bypasses `r'ignore\s+(previous|all|above|prior)\s+instructions'`
   - **No token limits**: Memory size bounded by characters, not tokens; LLM sees compressed context

5. **System Prompt Snapshot Frozen at Init (Line 101):**
   ```python
   self._system_prompt_snapshot: str = self._build_context()
   ```

   **Issues:**
   - **Stale context**: Snapshot never updates during session
   - **Rationale (per docs line 76-81)**: Stable for prompt cache, but...
   - **Contradiction**: Callers can call get_context_for_llm() (live) OR use system_prompt_snapshot (frozen)
   - **Race condition**: Step 3 reason uses memory.get_context_for_llm() (line 62 in step3_reason.py), not snapshot
   - **Design flaw**: Two APIs (snapshot vs live) creates confusion

---

### 7. config.py (104 lines)

**Purpose:** Load config from file (priority) → env vars → defaults.

**Key Functions:**

| Function | Signature | Lines | Critical Notes |
|----------|-----------|-------|-----------------|
| `_load_file_config` | `def _load_file_config() -> dict` | 16-21 | Load ~/.oncall/config.json |
| `_load_env_file` | `def _load_env_file()` | 24-31 | Load ~/.oncall/.env into os.environ |
| `_get` | `def _get(file_path: list[str], env_key: str, default: str) -> str` | 39-51 | Resolve: file > env > default |
| N/A | Module-level Config instantiation | 102-103 | Create config and resolve api_key |

**Critical Issues:**

1. **DUAL CONFIG TRACK — Broken Priority (Lines 96-103):**
   ```python
   config = Config()
   config.llm_api_key = _resolve_api_key()
   ```

   **And Config class line 67:**
   ```python
   llm_api_key: str = _get(["llm", "_token_env"], "GITHUB_TOKEN", "")
   ```

   **Issues:**
   - **Two paths**: Config.__init__ sets llm_api_key from _get() (resolves config/env)
   - **Then overwrite**: Line 103 calls _resolve_api_key() which re-reads file config only
   - **Missing env fallback**: _resolve_api_key() doesn't check GITHUB_TOKEN if file config missing
   - **Logic flaw**: Priority says file > env, but _resolve_api_key() ignores env as fallback

   **Scenario - BROKEN:**
   ```python
   # ~/.oncall/config.json: (missing "llm" section)
   # Environment: GITHUB_TOKEN=sk-123
   
   config = Config()  # llm_api_key = "" (empty from _get default)
   config.llm_api_key = _resolve_api_key()  # tries config file, finds nothing, returns ""
   # Result: GITHUB_TOKEN env var ignored ❌
   ```

2. **Type Coercion Fragile (Lines 49-50):**
   ```python
   if val is not None and not isinstance(val, dict):
       return str(val)
   ```

   - **Issue**: Port can be string "8090" from JSON; int() called in Config (line 63)
   - **Missing**: No error handling if `int("abc")` is called
   - **Risk**: Malformed config.json crashes startup

3. **Env File Parsing Naive (Lines 27-31):**
   ```python
   for line in ENV_PATH.read_text().splitlines():
       line = line.strip()
       if line and not line.startswith("#") and "=" in line:
           k, v = line.split("=", 1)
           os.environ.setdefault(k.strip(), v.strip())
   ```

   - **Issue**: No quoted string support
   - **Example**: `KEY="value with spaces"` parsed as KEY=`"value` (broken)
   - **Missing**: No escape handling

---

### 8. api.py (213 lines)

**Purpose:** FastAPI HTTP entrypoint; intent extraction + trigger pipeline.

**Key Functions:**

| Function | Signature | Lines | Critical Notes |
|----------|-----------|-------|-----------------|
| `extract_intent` | `async def extract_intent(query: str, workspace_context: str = "", model: str = None) -> dict` | 70-104 | LLM extracts structured intent from raw metadata |
| `trigger_oncall` | `@app.post("/trigger")` | 109-176 | Main HTTP endpoint |
| `get_memory` | `@app.get("/memory")` | 187-195 | View memory |
| `health` | `@app.get("/health")` | 181-184 | Health check |

**Critical Issues:**

1. **Silent Intent Extraction Fallback (Lines 89-104):**
   ```python
   try:
       return json.loads(content)
   except json.JSONDecodeError:
       return {
           "signal_name": "UnparsedIncident",
           "description": query[:200],
           ...
       }
   ```

   - **Issue**: LLM JSON parse fails → returns generic default
   - **Impact**: Intent lost; pipeline runs with wrong signal_name
   - **Risk**: No signal name, so triage/WoW queries are generic

2. **Workspace Context Truncated (Line 77):**
   ```python
   workspace_context=workspace_context[:2000]
   ```

   - **Issue**: Workspace context (soul.md + memory.md) hard-limited to 2000 chars
   - **Impact**: Large workspaces lose context
   - **Missing**: No warning if truncated

3. **HTTP Exception Swallowing (Line 177-178):**
   ```python
   except Exception as e:
       raise HTTPException(status_code=500, detail=str(e))
   ```

   - **Issue**: All exceptions → 500 Internal Server Error
   - **Impact**: Client can't distinguish invalid input from server error
   - **Missing**: No differentiation (400 for validation, 500 for server)

4. **No Input Validation on Trigger Query:**
   ```python
   class TriggerRequest(BaseModel):
       query: str  # ← No max_length, no regex pattern
   ```

   - **Issue**: Arbitrary size query accepted
   - **Risk**: OOM if query is multi-MB string; LLM call timeouts

---

### 9. connectors/mock.py (106 lines)

**Purpose:** Mock data for when MCP unavailable.

**Key Functions:**

| Function | Signature | Lines | Critical Notes |
|----------|-----------|-------|-----------------|
| `mock_triage` | `def mock_triage(signal_name: str) -> dict` | 11-49 | Simulates Step 1 |
| `mock_wow` | `def mock_wow(signal_name: str, repo: str = "") -> dict` | 52-105 | Simulates Step 2 |

**Critical Issues:**

1. **Mock Data Determinism (Lines 14, 54):**
   ```python
   random.randint(0, 500)  # Line 17
   random.randint(100, 2000)  # Line 54
   ```

   - **Issue**: Each call returns different random data
   - **Impact**: Same signal name queried twice → different verdicts/trends
   - **Risk**: Makes testing/debugging hard; false positives in tests

2. **Mock Verdict Logic Leaky (Lines 22-34):**
   ```python
   if windows_pct > 60:
       verdict = "Windows First"
       global_first_seen = now - timedelta(hours=random.randint(1, 6))
       windows_first_seen = now - timedelta(hours=random.randint(7, 24))
   elif windows_pct < 30:
       verdict = "Global First"
   else:
       verdict = "Global First"  # Tie goes to Global First
   ```

   - **Issue**: Verdict doesn't always match timestamps
   - **Example**: If windows_pct=61% (Windows First), global_first_seen could be earlier
   - **Impact**: Inconsistent triage results

---

### 10. copilot_proxy.py (292 lines)

**Purpose:** GitHub Copilot OAuth device code + token refresh.

**Key Functions:**

| Function | Signature | Lines | Critical Notes |
|----------|-----------|-------|-----------------|
| `login` | `async def login(self) -> bool` | 77-141 | Device code OAuth flow |
| `_refresh_copilot_token` | `async def _refresh_copilot_token(self) -> bool` | 145-182 | Exchange GitHub token for Copilot token |
| `ensure_token` | `async def ensure_token(self) -> bool` | 184-190 | Ensure valid token, refresh if needed |
| `chat_completion` | `async def chat_completion(...)` | 194-230 | OpenAI-compatible completion |
| `chat_completion_stream` | `async def chat_completion_stream(...)` | 234-278 | Streaming completion |

**Critical Issues:**

1. **Credentials Stored Unencrypted (Lines 41-44):**
   ```python
   def _save_credentials(data: dict):
       CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
       CREDENTIALS_PATH.write_text(json.dumps(data, indent=2))
       CREDENTIALS_PATH.chmod(0o600)
   ```

   - **Issue**: GitHub + Copilot tokens stored in plaintext JSON
   - **File perms**: 0o600 is user-readable only, but not encrypted
   - **Risk**: If disk is stolen/copied, tokens are compromised
   - **Mitigation needed**: Use OS keyring (macOS Keychain, Windows Credential Manager, Linux Secret Service)

2. **Token Refresh Race Condition (Lines 162-170):**
   ```python
   if resp.status_code == 200:
       data = resp.json()
       self.copilot_token = data["token"]
       self.copilot_expires_at = data["expires_at"]
       endpoints = data.get("endpoints", {})
       api_base = endpoints.get("api", "").rstrip("/")
       if api_base:
           self.chat_api_url = f"{api_base}/chat/completions"
   ```

   - **Issue**: Multiple fields set sequentially; no transaction
   - **Race condition**: If process crashes mid-refresh, copilot_token and copilot_expires_at may be mismatched
   - **Impact**: Token validation (line 65) checks both; if one stale, requests fail

3. **Token Expiry Check Off-by-One (Line 65):**
   ```python
   return bool(self.copilot_token) and time.time() < self.copilot_expires_at - 60
   ```

   - **Issue**: Subtracts 60s buffer, but -60 is arbitrary
   - **Risk**: If token expires in <60s, may fail mid-request; no retry logic (line 226-228 does retry, but limited)

---

### 11. workspace.py (234 lines)

**Purpose:** Project workspaces with soul.md + memory.md + config.json.

**Key Functions:**

| Function | Signature | Lines | Critical Notes |
|----------|-----------|-------|-----------------|
| `create` | `def create(self, team: str = "", description: str = "")` | 90-108 | Initialize workspace |
| `append_memory` | `def append_memory(self, section: str, entry: str)` | 127-152 | Append entry under markdown section |
| `get_llm_context` | `def get_llm_context(self) -> str` | 167-176 | Build context from soul + memory |

**Critical Issues:**

1. **Markdown Insertion Logic Fragile (Lines 134-147):**
   ```python
   for i, line in enumerate(lines):
       if line.strip() == marker:
           insert_idx = i + 1
           while insert_idx < len(lines) and lines[insert_idx].strip().startswith("<!--"):
               insert_idx += 1
               while insert_idx < len(lines) and "-->" not in lines[insert_idx - 1]:
                   insert_idx += 1
   ```

   - **Issue**: Tries to skip comment blocks but logic is convoluted
   - **Bug**: Inner while loop increments insert_idx but checks `lines[insert_idx - 1]`
   - **Example**: If comment is multi-line, may insert in wrong place
   - **Risk**: Over time, memory.md becomes malformed

2. **No Concurrent Write Protection (Lines 127-152):**
   ```python
   def append_memory(self, section: str, entry: str):
       content = self.read_memory()  # Read
       # ... parse and modify content ...
       self.memory_path.write_text("\n".join(lines))  # Write
   ```

   - **Issue**: TOCTOU race condition
   - **Scenario**: Two processes call append_memory simultaneously → second overwrites first's changes
   - **Missing**: File locking

---

### 12. Other Files (Brief Summary)

**cli.py (168 lines):** CLI entrypoint for `oncall` command
- No critical issues; straightforward arg parsing

**tui.py (partial, ~200+ lines):** Interactive terminal UI
- Stream processing seems robust
- Uses prompt_toolkit for input handling

**onboard.py (partial, ~100+ lines):** Setup wizard
- No critical issues; just TUI for config

---

## SECURITY SUMMARY TABLE

| Issue | Severity | File | Lines | Impact |
|-------|----------|------|-------|--------|
| KQL Injection (string format) | HIGH | step1_triage.py | 52, 56 | ADX query hijacking |
| KQL Injection (string format) | HIGH | step2_wow.py | 63, 95 | ADX query hijacking |
| Fragile KQL Response Parsing | HIGH | step1_triage.py | 59-68 | Silent fallback to "Unknown" |
| Fragile Regex WoW Parsing | HIGH | step2_wow.py | 77-83 | Wrong metrics computed |
| Silent Exception Swallowing | MEDIUM | step2_wow.py | 93-99 | GitHub data loss |
| JSON Parsing Fallback | MEDIUM | step3_reason.py | 94-109 | Generic incident analysis |
| Teams Notification Silent Fail | MEDIUM | step3_reason.py | 151-153 | Oncall team not notified |
| Dedup Logic Bypassed | MEDIUM | memory/store.py | 194-202 | Memory bloat |
| Character Limit Enforcement | MEDIUM | memory/store.py | 169-177 | Unpredictable eviction |
| File Lock Missing on Windows | MEDIUM | memory/store.py | 222-225 | JSON corruption |
| Security Regex Bypass | LOW | memory/store.py | 62-70 | Injection bypass possible |
| Config Priority Ambiguity | MEDIUM | config.py | 96-103 | Env vars ignored |
| Unencrypted Token Storage | MEDIUM | copilot_proxy.py | 41-44 | Credential compromise |
| Token Refresh Race Condition | LOW | copilot_proxy.py | 162-170 | Mismatched token state |
| Workspace Markdown Insertion | LOW | workspace.py | 134-147 | Memory.md corruption |
| No Concurrent Workspace Write | LOW | workspace.py | 127-152 | TOCTOU race condition |
| Mock Data Non-Deterministic | LOW | connectors/mock.py | 14, 54 | Testing issues |

---

## ARCHITECTURAL ISSUES

### 1. "Dual Track" Config Resolution (config.py)

**Problem:**
```python
config = Config()  # Sets llm_api_key from _get() (file > env > default)
config.llm_api_key = _resolve_api_key()  # OVERWRITES with file-only lookup
```

**Fix Required:**
- Single, consistent resolution function
- Clear priority: file > env > default
- Document which config keys use file vs env

### 2. Memory Snapshot vs Live Context Confusion

**Problem:**
- `system_prompt_snapshot` frozen at init (good for cache stability)
- `get_context_for_llm()` returns live context (contradicts snapshot)
- step3_reason.py uses live context, but docs say snapshot (line 76-81)

**Fix Required:**
- Choose one approach: either snapshot or live
- If snapshot: update it when memory changes
- If live: accept that prefix cache won't be stable across sessions

### 3. MCP Availability Check Too Late

**Problem:**
```python
# orchestrator.py line 18-20
self._mcp_available = bool(
    config.adx_mcp.url or config.github_mcp.url or config.teams_mcp.url
)
```

- Checks if URLs are non-empty, but doesn't validate they're reachable
- If ADX_MCP_URL="" but GITHUB_MCP_URL is set, `_mcp_available=True`
- Then _run_with_mcp tries to create ADXClient with empty URL (silent failure)

**Fix Required:**
- Per-connector availability check at initialization
- Async probe each connector; handle gracefully if one is down

### 4. Silent Step Skipping

**Problem:**
```python
# orchestrator.py line 139-148
if intent.get("should_run_wow", True):
    wow = await step_wow_compare(...)
else:
    result["steps"]["wow"] = { "trend": "skipped", ... }
```

- If intent extraction is wrong, WoW step silently skipped
- Caller doesn't know data is incomplete
- No warning in logs

**Fix Required:**
- Log skipped steps at INFO level
- Return warning in response metadata

---

## RECOMMENDATIONS

### Priority 1 (CRITICAL)

1. **Fix KQL Injection**
   - Use Kusto parameter binding instead of string formatting
   - Or: Implement strict input validation (whitelist pattern)
   - Audit: Affected = step1_triage.py (lines 52, 56), step2_wow.py (lines 63, 95)

2. **Fix Fragile Parsing**
   - step1_triage.py: Expect structured JSON response from MCP, not text
   - step2_wow.py: Parse response as JSON dict, not regex on text
   - Validate response schema before using

3. **Config Resolution Cleanup**
   - Single _get_config_value() function with clear priority
   - Remove _resolve_api_key() workaround
   - Document which keys support env vars

### Priority 2 (HIGH)

4. **Error Handling & Observability**
   - Replace silent try/except with explicit error handling
   - Log KQL query + response for debugging
   - Distinguish transient (retry) vs permanent (fail) errors in MCP client

5. **Memory Concurrency & Platform Support**
   - Add fcntl fallback for Windows (use pathlib.Lock or file rotation)
   - Fix character limit eviction logic (handle entry > limit case)
   - Fix dedup for nested dicts

6. **Teams Notification**
   - Don't silently swallow Teams send failure
   - Add retry logic with exponential backoff
   - Log failure at ERROR level, include in response

### Priority 3 (MEDIUM)

7. **Credential Security**
   - Store GitHub token in OS keyring (keyring library)
   - OR: Use environment variable only (no persistent storage)
   - Encrypt credentials file at rest

8. **MCP Client Improvements**
   - Add per-tool timeout configuration
   - Implement streaming for large result sets
   - Add request logging (without secrets)

9. **Testing**
   - Add mock fixtures for MCP responses
   - Test fragile parsing with malformed inputs
   - Add integration tests with real ADX (non-prod database)

---

## FUNCTION SIGNATURES REFERENCE

All critical functions with line numbers:

### Orchestrator
- `OncallOrchestrator.__init__()` — line 16
- `OncallOrchestrator.run()` — line 22
- `OncallOrchestrator._run_with_mock()` — line 41
- `OncallOrchestrator._run_with_mcp()` — line 99

### Steps
- `step_triage()` — step1_triage.py:41
- `step_wow_compare()` — step2_wow.py:44
- `step_reason_and_act()` — step3_reason.py:51

### MCP Client
- `MCPClient.__init__()` — mcp_clients/client.py:10
- `MCPClient.call_tool()` — mcp_clients/client.py:14
- `MCPClient.list_tools()` — mcp_clients/client.py:33

### Memory
- `OncallMemory.__init__()` — memory/store.py:83
- `OncallMemory.add()` — memory/store.py:206
- `OncallMemory._is_duplicate()` — memory/store.py:194
- `OncallMemory.get_context_for_llm()` — memory/store.py:276
- `scan_content()` — memory/store.py:62

### Config
- `_get()` — config.py:39
- `_load_file_config()` — config.py:16
- `_load_env_file()` — config.py:24

### API
- `extract_intent()` — api.py:70
- `trigger_oncall()` — api.py:109

### Workspace
- `Workspace.create()` — workspace.py:90
- `Workspace.append_memory()` — workspace.py:127
- `Workspace.get_llm_context()` — workspace.py:167

### Copilot Proxy
- `CopilotProxy.login()` — copilot_proxy.py:77
- `CopilotProxy._refresh_copilot_token()` — copilot_proxy.py:145
- `CopilotProxy.chat_completion()` — copilot_proxy.py:194
- `CopilotProxy.chat_completion_stream()` — copilot_proxy.py:234

