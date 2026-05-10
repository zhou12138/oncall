# OnCall-Agent: Comprehensive Codebase Analysis

**Version:** 0.2.4  
**Date:** May 10, 2026  
**Purpose:** HTTP trigger → 3-step orchestration → MCP (ADX Kusto + GitHub + Teams)

---

## 1. Project Overview

### Mission
OnCall Agent is an intelligent incident triage system that:
1. **Accepts HTTP triggers** with raw incident metadata (natural language)
2. **Orchestrates a 3-step pipeline** for analysis and reasoning
3. **Interfaces with MCP servers** for data retrieval and notifications
4. **Learns from history** via persistent memory with semantic search

### Key Features
- **Async FastAPI HTTP API** with `/trigger` endpoint (202 Accepted pattern)
- **3-step pipeline**: Triage → Week-over-Week comparison → LLM reasoning
- **Provider-agnostic architecture**: Pluggable MockProvider or MCPProvider
- **Persistent memory** with semantic recall (Jaccard similarity)
- **Incident state machine** (new → triaged → acknowledged → escalated)
- **Workspace isolation** with soul.md + memory.md per project
- **GitHub Copilot integration** via device code OAuth
- **Teams Adaptive Cards** for incident notifications
- **Structured JSON logging** with run_id correlation
- **Execution tracing** (RunTrace/StepTrace) for observability

---

## 2. Directory Structure

```
oncall/
├── oncall_agent/                 # Main package (v0.2.4)
│   ├── __init__.py
│   ├── api.py                    # FastAPI entrypoint (HTTP server)
│   ├── orchestrator.py           # 3-step pipeline orchestrator
│   ├── cli.py                    # CLI commands (login, serve, ws, chat)
│   ├── config.py                 # Configuration with file > env > default priority
│   ├── errors.py                 # Exception hierarchy (OncallError → 4 subtypes)
│   ├── routing.py                # Owner routing (signal name → team mapping)
│   ├── trace.py                  # RunTrace/StepTrace for execution visibility
│   ├── logging_config.py         # JSON logging + run_id correlation
│   ├── copilot_proxy.py          # GitHub Copilot auth + chat client
│   ├── workspace.py              # Project workspaces (soul.md + memory.md)
│   ├── tui.py                    # Interactive TUI chat mode
│   ├── onboard.py                # First-time setup wizard
│   │
│   ├── models/
│   │   ├── __init__.py
│   │   └── incident.py           # Incident dataclass + state machine
│   │
│   ├── providers.py              # DataProvider protocol + MCPProvider/MockProvider
│   │
│   ├── steps/
│   │   ├── __init__.py
│   │   ├── step1_triage.py       # ADX: Global-first vs Windows-first verdict
│   │   ├── step2_wow.py          # ADX: Week-over-week metrics + GitHub PRs
│   │   └── step3_reason.py       # LLM reasoning → summary → Teams notification
│   │
│   ├── mcp_clients/
│   │   ├── __init__.py
│   │   └── client.py             # Generic MCP tool-call client (JSON-RPC + retry)
│   │
│   ├── connectors/
│   │   ├── __init__.py
│   │   └── mock.py               # Mock ADX/GitHub data (for offline testing)
│   │
│   ├── memory/
│   │   ├── __init__.py
│   │   └── store.py              # OncallMemory (JSON, semantic recall, security scan)
│   │
│   ├── cards/
│   │   ├── __init__.py
│   │   └── adaptive.py           # Teams Adaptive Card v1.4 builder
│   │
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── icm_webhook.py        # ICM webhook parsing + HMAC-SHA256 verify
│   │   └── log_enricher.py       # Semantic log enrichment from ADX
│   │
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── parsing.py            # MCP response parsing (text + WoW metrics)
│   │   └── sanitize.py           # Sanitization (KQL injection prevention)
│   │
│   └── skills/
│       └── oncall.skills.md      # Skill definitions for Claude Code
│
├── pyproject.toml               # Project metadata + dependencies
├── README.md                    # Quick start guide
└── build/                       # Build artifacts
```

---

## 3. Architecture Patterns

### 3.1 Provider Pattern (Dependency Injection)
**Purpose:** Decouple pipeline logic from data source

```python
# Protocol
@runtime_checkable
class DataProvider(Protocol):
    mode: str  # "mcp" or "mock"
    async def triage(signal_name: str) -> dict
    async def wow_compare(signal_name: str, repo: str) -> dict
    async def reason_and_act(...) -> dict
    async def enrich(signal_name: str) -> str
```

**Implementations:**
- **MockProvider**: Fake data (for testing/demo)
- **MCPProvider**: Real MCP-backed (ADX, GitHub, Teams servers)

**Selection:** `select_provider()` → MCPProvider if any MCP URL configured, else MockProvider

### 3.2 Orchestrator Pattern
**OncallOrchestrator** drives a 3-step pipeline:

1. **Step 1: Triage** (ADX via MCP)
   - Query: Global first appearance vs Windows first?
   - Returns: Verdict + platform breakdown

2. **Step 2: WoW** (Week-over-Week comparison)
   - ADX: Current week vs previous week metrics
   - GitHub: Recent PRs correlated with signal
   - Returns: Delta, trend (up/down/flat), recent changes

3. **Step 3: Reason + Act** (LLM reasoning)
   - Input: Triage result + WoW metrics + memory context
   - LLM: Analyze root cause, recommend actions
   - Output: Summary + severity + actions
   - Action: Send Adaptive Card to Teams (optional)

**Error Handling:** Per-step error wrapping (TriageError, WoWError, ReasoningError)

**Incident Tracking:** Module-level registry `_incidents: dict[str, Incident]` for action callbacks

### 3.3 Memory Pattern (Hermes-style)
**OncallMemory** provides:

- **Frozen snapshot**: System prompt context captured at init (stable mid-session)
- **Character limits**: Per-section limits + global cap (bounded growth)
- **Security scanning**: Blocks prompt injection/exfil payloads
- **Atomic writes**: Tmpfile + fsync + os.replace (crash-safe)
- **Semantic recall**: Keyword Jaccard similarity over signal names
- **Automatic eviction**: Oldest entries removed when limits exceeded

**Sections:**
- `incidents` (4000 chars): Past issues + resolutions
- `patterns` (2000 chars): Recurring alerts
- `runbooks` (2000 chars): Procedures
- `wow_comparisons` (1500 chars): Historical trends

### 3.4 Workspace Pattern
**Per-project isolation** with:

- **soul.md**: Project identity, goals, escalation rules
- **memory.md**: Accumulated knowledge (auto-updated by agent)
- **config.json**: Workspace-specific overrides

**Active workspace**: Stored in `~/.oncall/active_workspace` file

### 3.5 State Machine (Incident)
```
new → triaged → acknowledged → mitigated → resolved
  ↘________________ escalated ________________↙
```

**Transitions:** Validated, logged with timestamp + actor

### 3.6 Async/Await Pattern
**Every I/O is async:**
- FastAPI endpoints
- MCP client calls (httpx.AsyncClient)
- LLM streaming (chat_completion_stream)
- Coroutines for each pipeline step

### 3.7 Execution Tracing Pattern
**RunTrace** captures:
- Per-step traces (StepTrace): name, duration, status, result_summary, error
- Run-level metadata: run_id, signal_name, start/end times
- Serializable to JSON (expose in /runs/{run_id} response)

---

## 4. Key Modules & Responsibilities

### 4.1 api.py (HTTP Server)
**FastAPI app on port 8090**

**Endpoints:**
- `POST /trigger` → Accepts raw incident metadata (202 Accepted)
- `GET /runs/{run_id}` → Poll for trace/result
- `POST /actions/{run_id}/{action}` → Incident state transitions (ack/escalate)
- `GET /incidents` → List all incidents
- `GET /incidents/{run_id}` → Single incident detail
- `POST /webhooks/icm` → ICM/Geneva webhook ingestion (HMAC verified)
- `GET /health` → Health check
- `GET /memory` → View global/workspace memory
- `GET /workspaces` → List workspaces

**Key Logic:**
- Intent extraction via LLM (structured JSON parsing)
- Background pipeline runner (`_run_pipeline`)
- In-memory run store (`_runs`)
- Auth middleware (X-API-Key header, optional)

### 4.2 orchestrator.py (Pipeline Orchestrator)
**OncallOrchestrator class**

**Methods:**
- `run(signal_name, repo, teams_channel, ...)` → Execute 3-step pipeline
- `_run_step(step_name, coro_factory, ...)` → Run step with logging + error wrapping
- `_run_pipeline(...)` → Step 1 → Step 2 → Step 3 → Memory persistence

**Responsibilities:**
- Memory initialization
- Provider selection
- Step execution coordination
- Error aggregation + logging
- Incident registry management
- Trace generation

### 4.3 steps/ (Step Implementations)

#### step1_triage.py
```python
async def step_triage(adx_client: MCPClient, signal_name: str) -> dict
```
- Parameterized KQL queries (injection-safe)
- ADX tables: SignalTable (signal name, timestamp, platform)
- Returns: Verdict (Global First / Windows First) + platform breakdown

#### step2_wow.py
```python
async def step_wow_compare(adx_client, github_client, signal_name, repo) -> dict
```
- Current week vs previous week: ADX count comparison
- Optional GitHub correlation (recent PRs)
- Returns: Metrics (current, previous, delta, trend) + recent_changes

#### step3_reason.py
```python
async def step_reason_and_act(...) -> dict
```
- Memory recall (semantic search for related incidents)
- LLM reasoning (system prompt + user data prompt)
- Adaptive Card generation + Teams send
- Memory persistence (incidents, patterns, wow_comparisons)

### 4.4 mcp_clients/client.py (MCP Transport)
**MCPClient class**

**Features:**
- Pooled httpx.AsyncClient (reused per client instance)
- Initialize handshake (JSON-RPC)
- Exponential backoff retry (3 attempts: 1s/2s/4s)
- Fail-fast on 4xx, retry on 5xx
- Timeout: 120s default

**Methods:**
- `initialize()` → JSON-RPC initialize
- `call_tool(tool_name, arguments)` → JSON-RPC tools/call
- `list_tools()` → JSON-RPC tools/list
- `_rpc(method, params)` → Core transport

### 4.5 memory/store.py (Persistent Memory)
**OncallMemory class**

**API:**
- `add(section, entry)` → Append + deduplicate + evict + save
- `search(section, keyword)` → Text search
- `recall(signal_name, top_k)` → Semantic recall (Jaccard)
- `record(signal_name, result)` → Convenience for incidents
- `get_context_for_llm()` → Live context (for ad-hoc queries)
- `system_prompt_snapshot` → Frozen context (for system prompt)
- `stats()` → Memory usage per section

**Security:**
- `scan_content()` → Blocks injection/exfil patterns
- Invisible unicode detection
- Threat pattern regex

**Concurrency:**
- File lock (fcntl on Unix, no-op on Windows)
- Read-modify-write under lock
- Atomic tmpfile + os.replace

### 4.6 config.py (Configuration)
**Config class (pydantic BaseModel)**

**Priority:** File > Environment > Defaults

**Sections:**
- Server (host, port, api_key, icm_webhook_secret)
- LLM (api_base, api_key indirection, model, temperature)
- MCP servers (ADX, GitHub, Teams URLs)
- Memory (path)
- Defaults (teams_channel, repo)

**Indirection:** Config file can declare `{"_token_env": "ENV_VAR_NAME"}` to point at env var

### 4.7 copilot_proxy.py (GitHub Copilot)
**CopilotProxy class**

**Auth Flow:**
- Device code OAuth (interactive)
- Keyring storage (fallback to file)
- Auto-refresh on expiry

**API:**
- `login()` → Device code flow
- `ensure_token()` → Auto-refresh if expired
- `chat_completion(messages, model, ...)` → Non-streaming
- `chat_completion_stream(...)` → Streaming chunks

### 4.8 workspace.py (Project Workspaces)
**Workspace + WorkspaceManager classes**

**Workspace Lifecycle:**
- `create(team, description)` → Init soul.md + memory.md + config.json
- `read_soul()` / `write_soul()`
- `read_memory()` / `append_memory(section, entry)`
- `get_llm_context()` → Merged soul + memory

**Manager:**
- `list_workspaces()`
- `get_active()` / `set_active(name)`
- `delete(name)`

### 4.9 trace.py (Execution Tracing)
**StepTrace + RunTrace dataclasses**

**StepTrace:**
- name, started_at, completed_at, duration_ms, status, result_summary, error

**RunTrace:**
- run_id, signal_name, steps: List[StepTrace], total duration

**Methods:**
- `mark_completed()` / `mark_failed()` / `mark_skipped()`
- `to_dict()` → JSON-serializable

### 4.10 logging_config.py (JSON Logging)
**JSONFormatter + _RunIdFilter**

**Features:**
- Every log record is a single JSON line
- `run_id` correlation via contextvars
- Extra fields from `logger.log(..., extra={...})`
- Exception stack traces included

**Helpers:**
- `run_id_scope(run_id)` → Context manager
- `log_step_event()` → Step event logging

---

## 5. Dependencies (pyproject.toml)

```toml
[project]
name = "oncall-agent"
version = "0.2.4"
requires-python = ">=3.10"

dependencies:
  - fastapi>=0.110
  - uvicorn[standard]>=0.29
  - httpx>=0.27
  - pydantic>=2.0
  - openai>=1.30
  - rich>=13.0
  - prompt-toolkit>=3.0

[dev]
  - pytest
  - pytest-asyncio
  - ruff
```

**Why these?**
- **fastapi/uvicorn**: Async HTTP framework
- **httpx**: Async HTTP client (MCP + Copilot)
- **pydantic**: Config validation + data models
- **openai**: OpenAI-compatible SDK (though Copilot is used instead)
- **rich**: Pretty terminal output
- **prompt-toolkit**: TUI chat mode (readline-like)

---

## 6. Request/Response Flow

### Typical Flow: POST /trigger

```
1. Client: POST /trigger with TriggerRequest
   {
     "query": "raw incident metadata",
     "workspace": "myproj",
     "model": "gpt-4o"  # optional
   }

2. API Handler: trigger_oncall()
   ├─ Generate run_id
   ├─ Record run as "accepted"
   ├─ Create background task: _run_pipeline()
   └─ Return 202 Accepted { run_id, poll_url }

3. Background: _run_pipeline(run_id, req)
   ├─ Extract intent via LLM
   │  └─ Structured JSON: signal_name, repo, teams_channel, severity_hint, ...
   ├─ Call orchestrator.run(signal_name, ...)
   │  ├─ Step 1: Triage (ADX via MCP)
   │  ├─ Step 2: WoW (ADX + GitHub via MCP)
   │  └─ Step 3: Reason (LLM via Copilot proxy)
   └─ Store result in _runs[run_id]

4. Client: Poll GET /runs/{run_id}
   ├─ Status: accepted | running | completed | failed
   ├─ Trace: detailed step tracing
   └─ Result: {intent, severity, summary, actions, steps, ...}
```

### Incident State Transitions

```
new (created by orchestrator)
  → triaged (orchestrator marks after step 1)
  → acknowledged (action callback: POST /actions/{run_id}/ack)
  → mitigated (manual state push)
  → resolved (manual state push)

Or from any state:
  → escalated (action callback: POST /actions/{run_id}/escalate)
```

---

## 7. Configuration Precedence

**Example**: `LLM_API_KEY`

```
1. ~/.oncall/config.json:
   { "llm": { "_token_env": "GITHUB_TOKEN" } }
   → Resolved via environment variable

2. Environment: GITHUB_TOKEN=...
   → Used directly

3. Default: (none specified)
   → Error on validate()
```

**File Paths:**
- `~/.oncall/config.json` → Config
- `~/.oncall/.env` → Secrets (loaded first)
- `~/.oncall/memory.json` → Global memory
- `~/.oncall/copilot_credentials.json` → Legacy (migrated to keyring)
- `~/.oncall/workspaces/{name}/` → Workspace directories
- `~/.oncall/active_workspace` → Active workspace name

---

## 8. Error Handling

**Exception Hierarchy:**
```
OncallError (base)
  ├── TriageError (Step 1 ADX/parsing failure)
  ├── WoWError (Step 2 failure)
  ├── ReasoningError (Step 3 LLM/Teams failure)
  └── MCPError (Transport-level MCP failure)
```

**HTTP Mapping:**
- 400: Bad request (invalid JSON, ICM signature)
- 401: Auth error (missing/invalid API key, ICM secret)
- 404: Not found (unknown run_id, incident)
- 409: Conflict (invalid state transition)
- 500: Internal server error (OncallError raised)

---

## 9. Security Considerations

### Memory Security
- **Injection scanning**: Blocks `ignore previous instructions`, `system prompt override`, etc.
- **Exfil prevention**: Blocks curl/wget with `${TOKEN}`, `${SECRET}`, etc.
- **Invisible unicode**: Detects zero-width characters, direction overrides

### Config Security
- **Indirection**: API keys stored as env var names in config file
- **Keyring integration**: Copilot credentials use OS keyring (or secured file)
- **File permissions**: Copilot credentials file → 0o600

### API Security
- **X-API-Key header**: Optional but can be enforced
- **ICM webhook HMAC**: SHA256 signature verification
- **KQL injection prevention**: Parameterized queries (p_SignalName, etc.)

### Logging
- **Structured JSON**: No free-form string interpolation
- **PII in logs**: Caller responsible (agent doesn't log incident content)

---

## 10. Key Classes & Responsibilities

| Class | Module | Key Methods | Purpose |
|-------|--------|-------------|---------|
| OncallOrchestrator | orchestrator | run(), _run_step(), _run_pipeline() | Pipeline orchestration |
| DataProvider | providers | triage(), wow_compare(), reason_and_act() | Protocol for data providers |
| MockProvider | providers | (implements DataProvider) | Fake data for testing |
| MCPProvider | providers | (implements DataProvider) | Real MCP-backed data |
| MCPClient | mcp_clients.client | call_tool(), initialize() | Generic MCP JSON-RPC client |
| OncallMemory | memory.store | add(), recall(), record() | Persistent cross-session context |
| Workspace | workspace | create(), read_soul(), get_llm_context() | Project isolation |
| WorkspaceManager | workspace | list_workspaces(), set_active() | Workspace CRUD |
| Incident | models.incident | transition(), to_dict() | Incident state machine |
| CopilotProxy | copilot_proxy | login(), chat_completion_stream() | GitHub Copilot client |
| RunTrace | trace | start_step(), to_dict() | Execution tracing |
| Config | config | (pydantic model) | Configuration management |

---

## 11. External Integrations

### MCP Servers (Expected)
- **ADX (Azure Data Explorer)** @ `ADX_MCP_URL`
  - Tools: `execute_query` (Kusto queries)
  - Used by: Step 1 (triage), Step 2 (WoW)

- **GitHub** @ `GITHUB_MCP_URL`
  - Tools: (listed dynamically)
  - Used by: Step 2 (recent PRs)

- **Teams** @ `TEAMS_MCP_URL`
  - Tools: `send_message` (Adaptive Card posting)
  - Used by: Step 3 (notifications)

### LLM (GitHub Copilot)
- Device code OAuth login
- Chat completion (streaming + non-streaming)
- Models: gpt-4o, gpt-4-turbo, etc.

### Webhooks (Inbound)
- **ICM/Geneva** @ `POST /webhooks/icm`
  - HMAC-SHA256 verification
  - Parses: incident_id, title, severity, owning_team, impacted_services

---

## 12. Testing Strategy

**Current:** pytest + pytest-asyncio (configured in pyproject.toml)

**What's Tested (inferred from code):**
- Configuration loading (file > env > defaults)
- Memory operations (add, recall, eviction)
- MCP client retry logic
- Incident state transitions
- Mock data generation
- Parsing (WoW metrics, MCP responses)

**Gaps (inferred):**
- End-to-end pipeline tests
- Async/await timing issues
- Concurrent memory access
- Large memory eviction scenarios

---

## 13. Known Limitations & TODOs

1. **Phase 1 (M1.4 / current)**
   - In-memory run store (not durable)
   - Mock MCP provider (for offline testing)
   - No database integration

2. **Phase 2 (M2.x, future)**
   - Durable run store (SQL/NoSQL)
   - More sophisticated memory recall (embeddings/vector DB)
   - Streaming LLM integration (not just Copilot)
   - Action templates (runbooks)

3. **Current Gaps**
   - No web UI (CLI + TUI only)
   - No multi-tenant support
   - Memory concurrency: file lock only (not distributed)
   - No audit logging (only structured logs)

---

## 14. Development Workflow

### Setup
```bash
cd ~/oncall
pip install -e .
oncall onboard  # First-time setup
```

### Run Server
```bash
oncall serve      # Starts on http://0.0.0.0:8090
# or
python -m oncall_agent.api
```

### Interactive Chat
```bash
oncall chat  # TUI chat in active workspace
```

### Workspace Management
```bash
oncall ws create myproj           # Create new workspace
oncall ws use myproj              # Switch active
oncall ws show myproj             # Display soul.md + memory.md
oncall ws delete myproj           # Remove
```

### Configuration
```bash
oncall config               # Show current config
oncall login                # GitHub Copilot device code login
oncall status               # Check login + token status
```

### Test Example
```bash
curl -X POST http://localhost:8090/trigger \
  -H "Content-Type: application/json" \
  -d '{
    "query": "HighCPUAlert on Windows Edge. Current: 2500 errors/min, previous: 800.",
    "workspace": "edge-oncall",
    "model": "gpt-4o"
  }'
# Response: 202 Accepted { "run_id": "...", "poll_url": "/runs/..." }
```

---

## 15. Summary

**OnCall Agent** is a well-architected incident triage system built on:

✅ **Clean patterns**: Provider injection, state machines, async/await  
✅ **Observability**: JSON logging, execution tracing, memory stats  
✅ **Security**: Input sanitization, memory content scanning, HMAC webhooks  
✅ **Extensibility**: Pluggable providers (MCP/mock), custom workspaces  
✅ **Persistence**: Cross-session memory with Hermes-style snapshots  
✅ **User experience**: CLI/TUI for developers, Adaptive Cards for stakeholders  

**Best for:**
- Teams using Azure Data Explorer (Kusto) + GitHub + Teams
- Running incident triage on-prem (no cloud-based incident management required)
- Integrating with existing oncall workflows via HTTP webhooks

**Architecture reads well** with clear separation of concerns, though **Phase 2 will need a durable run store and more sophisticated memory recall** for production scale.

