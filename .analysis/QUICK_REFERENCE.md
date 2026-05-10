# OnCall-Agent Quick Reference Guide

## Project Info
- **Name**: OnCall Agent
- **Version**: 0.2.4
- **Purpose**: HTTP-triggered incident triage with 3-step orchestration
- **Language**: Python 3.10+
- **Framework**: FastAPI + Async/Await

---

## Key Concepts at a Glance

| Concept | Definition | Location |
|---------|-----------|----------|
| **Provider** | Pluggable data source (Mock or MCP-backed) | `providers.py` |
| **Orchestrator** | 3-step pipeline: Triage → WoW → Reason | `orchestrator.py` |
| **MCP** | Model Context Protocol (for external data) | `mcp_clients/client.py` |
| **Memory** | Persistent JSON store with semantic recall | `memory/store.py` |
| **Workspace** | Project isolation (soul.md + memory.md) | `workspace.py` |
| **Incident** | State machine tracking alert lifecycle | `models/incident.py` |
| **Trace** | Execution visibility (runs + steps) | `trace.py` |

---

## File Organization

### Core Orchestration
```
orchestrator.py         → Pipeline coordination
providers.py            → DataProvider protocol + implementations
steps/
  ├─ step1_triage.py    → ADX query: global vs windows first
  ├─ step2_wow.py       → Week-over-week metrics + GitHub
  └─ step3_reason.py    → LLM reasoning + Teams notification
```

### HTTP API & CLI
```
api.py                  → FastAPI server (port 8090)
cli.py                  → Command-line interface
```

### Data & Storage
```
config.py               → Configuration (file > env > defaults)
memory/store.py         → Persistent cross-session memory
workspace.py            → Project workspaces + soul/memory files
models/incident.py      → Incident state machine
```

### External Integration
```
copilot_proxy.py        → GitHub Copilot auth + chat
mcp_clients/client.py   → Generic MCP JSON-RPC client
connectors/mock.py      → Mock data (for offline testing)
ingestion/
  ├─ icm_webhook.py     → ICM webhook parsing + HMAC
  └─ log_enricher.py    → Log semantic enrichment
```

### Utilities
```
logging_config.py       → JSON logging + run_id correlation
trace.py                → RunTrace/StepTrace for observability
routing.py              → Signal → Team owner mapping
errors.py               → Exception hierarchy
utils/
  ├─ parsing.py         → MCP response parsing
  └─ sanitize.py        → KQL/input sanitization
cards/adaptive.py       → Teams Adaptive Card v1.4 builder
```

---

## HTTP Endpoints

### Core Pipeline
| Method | Path | Status | Purpose |
|--------|------|--------|---------|
| POST | `/trigger` | 202 | Dispatch pipeline asynchronously |
| GET | `/runs/{run_id}` | 200/404 | Poll trace & result |

### Incident Management
| Method | Path | Status | Purpose |
|--------|------|--------|---------|
| POST | `/actions/{run_id}/{action}` | 200/409 | State transition (ack/escalate) |
| GET | `/incidents` | 200 | List all incidents |
| GET | `/incidents/{run_id}` | 200/404 | Single incident detail |

### Webhooks & Configuration
| Method | Path | Status | Purpose |
|--------|------|--------|---------|
| POST | `/webhooks/icm` | 202 | ICM webhook ingestion |
| GET | `/health` | 200 | Health check |
| GET | `/memory` | 200 | View memory (global or workspace) |
| GET | `/workspaces` | 200 | List workspaces |

---

## CLI Commands

```bash
# Authentication & Status
oncall login                    # GitHub Copilot device code login
oncall status                   # Check login + token status

# Configuration & Setup
oncall onboard                  # First-time interactive setup
oncall config                   # Show current configuration
oncall serve                    # Start HTTP API server (port 8090)

# Interactive Mode
oncall chat                     # TUI chat mode (active workspace context)

# Workspace Management
oncall ws                       # List workspaces
oncall ws create <name>         # Create new workspace
oncall ws use <name>            # Switch active workspace
oncall ws show [<name>]         # Display soul.md + memory.md
oncall ws delete <name>         # Delete workspace

# Help & Version
oncall help / -h / --help       # Show help
oncall -v / --version           # Show version
```

---

## Configuration Priority

```
1. ~/.oncall/config.json        (highest priority)
   {
     "llm": {
       "_token_env": "GITHUB_TOKEN"   ← Indirection pattern
     },
     "mcp": {
       "adx": {"url": "..."},
       ...
     }
   }

2. Environment variables
   LLM_API_KEY, ADX_MCP_URL, etc.

3. Hardcoded defaults            (lowest priority)
```

**Special**: `_token_env` allows config file to point at env var holding the actual secret.

---

## Important Paths

```
~/.oncall/config.json                  → Main configuration
~/.oncall/.env                         → Secrets (loaded first)
~/.oncall/memory.json                  → Global persistent memory
~/.oncall/copilot_credentials.json     → Copilot creds (legacy, now keyring)
~/.oncall/active_workspace             → Active workspace name
~/.oncall/workspaces/{name}/
  ├─ soul.md                          → Project identity + rules
  ├─ memory.md                        → Auto-updated knowledge
  └─ config.json                      → Workspace config overrides
```

---

## Key Classes & Methods

### OncallOrchestrator
```python
orchestrator = OncallOrchestrator()
result = await orchestrator.run(
    signal_name="HighCPU",
    repo="owner/repo",
    teams_channel="oncall",
    model="gpt-4o",
    intent={...},  # structured dict
    raw_query="...",
    run_id=None
)
# Returns: {signal_name, severity, summary, actions, steps, trace, incident, ...}
```

### OncallMemory
```python
memory = OncallMemory("~/.oncall/memory.json")

# Add entry (with dedup, eviction, security scan)
memory.add("incidents", {"title": "...", "summary": "..."})

# Semantic recall (keyword Jaccard)
similar = memory.recall("HighCPUAlert", top_k=3)

# Get context (for LLM prompt)
context = memory.get_context_for_llm()

# Get frozen snapshot (for system prompt, stable mid-session)
snapshot = memory.system_prompt_snapshot

# View stats
stats = memory.stats()
```

### Workspace
```python
ws = WorkspaceManager.create("myproj", team="edge-team", description="...")
ws.read_soul()                    # Returns soul.md content
ws.read_memory()                  # Returns memory.md content
ws.append_memory("Recent Incidents", "entry text")
ws.get_llm_context()              # Returns merged soul + memory
WorkspaceManager.set_active("myproj")
```

### Incident
```python
incident = Incident(run_id="...", signal_name="HighCPU", severity="high", owner="...")
incident.transition("triaged", by="orchestrator")
incident.transition("acknowledged", by="action:ack")
incident.to_dict()                # JSON-serializable
```

### MCPClient
```python
adx = MCPClient("adx-kusto", "http://localhost:8091/sse")
await adx.initialize()            # JSON-RPC initialize handshake
result = await adx.call_tool("execute_query", {
    "query": "...",
    "parameters": {"p_SignalName": "HighCPU"}
})
tools = await adx.list_tools()
```

### CopilotProxy
```python
proxy = get_proxy()               # Singleton
await proxy.login()               # Device code flow
await proxy.ensure_token()        # Auto-refresh if expired

resp = await proxy.chat_completion(
    [{"role": "user", "content": "..."}],
    model="gpt-4o",
    temperature=0.3
)

async for chunk in proxy.chat_completion_stream(...):
    print(chunk, end="")
```

---

## Error Handling

### Exception Hierarchy
```
OncallError
  ├─ TriageError          (Step 1: ADX query, parsing failure)
  ├─ WoWError             (Step 2: metrics, GitHub failure)
  ├─ ReasoningError       (Step 3: LLM, Teams send failure)
  └─ MCPError             (Transport: MCP server unreachable)
```

### Retry Strategy
- **MCP Client**: Exponential backoff (1s, 2s, 4s) × 3 attempts
- **Transient errors**: HTTP 5xx, timeout, connection refused
- **Fail-fast**: HTTP 4xx, JSON-RPC app errors

---

## Incident State Machine

```
     ┌─────────────────────┐
     │         new         │  (created by orchestrator)
     └─────────────────────┘
              │
              ▼
     ┌─────────────────────┐
     │      triaged        │  (marked after step 1)
     └─────────────────────┘
              │
              ▼
     ┌─────────────────────┐
     │   acknowledged      │  (action: ack)
     └─────────────────────┘
              │
              ▼
     ┌─────────────────────┐
     │     mitigated       │
     └─────────────────────┘
              │
              ▼
     ┌─────────────────────┐
     │      resolved       │
     └─────────────────────┘

From any state → escalated (action: escalate)
```

---

## Memory Storage Model

### Global Memory (~/.oncall/memory.json)
```json
{
  "incidents": [
    {"title": "...", "severity": "high", "summary": "...", "timestamp": "..."},
    ...
  ],
  "patterns": [
    {"pattern": "recurring issue", "signal": "HighCPU", ...},
    ...
  ],
  "runbooks": [...],
  "wow_comparisons": [...]
}
```

**Per-section limits:**
- incidents: 4000 chars
- patterns: 2000 chars
- runbooks: 2000 chars
- wow_comparisons: 1500 chars
- **Total**: 10,000 chars max

**Auto-eviction**: When limit exceeded, oldest entries removed first.

**Semantic recall**: Keyword Jaccard similarity (tokenize signal names, compute union/intersection).

---

## Logging

### Format
Every log line is a **single JSON object** (no multi-line):
```json
{
  "ts": "2026-05-10T12:34:56",
  "level": "INFO",
  "logger": "oncall_agent.orchestrator",
  "msg": "step.completed triage",
  "run_id": "abc-def-ghi",
  "event": "step.completed",
  "step_name": "triage",
  "signal_name": "HighCPU",
  "duration_ms": 234.5,
  "status": "ok"
}
```

### Key Features
- **run_id correlation**: Every record includes run_id from contextvars
- **Structured extras**: No free-form string interpolation
- **Minimal defaults**: Only timestamp, level, logger, msg; everything else is custom fields

### Usage
```python
logger.info("step.completed", extra={
    "event": "step.completed",
    "step_name": "triage",
    "duration_ms": 234.5,
})
```

---

## Testing

### Current Stack
- **Framework**: pytest + pytest-asyncio
- **Coverage**: Parsing, config, memory, state machine, mock data

### Run Tests
```bash
pytest                          # All tests
pytest tests/test_memory.py     # Specific file
pytest -v                       # Verbose
pytest -s                       # Print output
```

### Example Test
```python
import pytest
from oncall_agent.memory.store import OncallMemory

@pytest.mark.asyncio
async def test_memory_add_and_recall():
    mem = OncallMemory(":memory:")
    mem.add("incidents", {"title": "HighCPU", "severity": "high"})
    results = mem.recall("HighCPU", top_k=1)
    assert len(results) == 1
    assert results[0]["title"] == "HighCPU"
```

---

## Development Quick Start

```bash
# 1. Clone & install
cd ~/oncall
pip install -e .

# 2. Configure
oncall onboard

# 3. Create workspace
oncall ws create myproj
oncall ws use myproj

# 4. Start server
oncall serve

# 5. In another terminal, test
curl -X POST http://localhost:8090/trigger \
  -H "Content-Type: application/json" \
  -d '{"query": "HighCPUAlert...", "workspace": "myproj"}'

# Or use interactive chat
oncall chat
```

---

## Common Patterns

### Using Provider Abstraction
```python
# Automatic selection (mock or MCP)
provider = select_provider()

# Or explicit
provider = MockProvider()
result = await provider.triage("HighCPU")
```

### Custom MCP Query
```python
adx = MCPClient("adx", "http://localhost:8091/sse")
result = await adx.call_tool("execute_query", {
    "query": "MyTable | where X > Y",
    "parameters": {"p_Param": "value"}
})
text = parse_mcp_text(result)
```

### Memory Persistence
```python
# In step3_reason.py
memory.add("incidents", {...})
memory.add("patterns", {...})
memory.record(signal_name, result)
```

### Workspace Context
```python
ws = WorkspaceManager.get("myproj")
llm_context = ws.get_llm_context()
# Pass to LLM as system prompt addition
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Not authenticated" | Run `oncall login` for GitHub Copilot |
| Token expired | `oncall login` refreshes, or auto-refresh on next request |
| Config not found | Check `~/.oncall/config.json` or env vars |
| MCP server unreachable | Verify `ADX_MCP_URL`, `GITHUB_MCP_URL`, etc. |
| Memory full | Check stats, oldest entries auto-evict |
| Workspace not found | Run `oncall ws list` to see available |

---

## Phase 2 Roadmap (Future)

- [ ] Durable run store (SQL/NoSQL instead of in-memory)
- [ ] Vector embeddings for memory recall (beyond keyword Jaccard)
- [ ] Multi-streaming LLM integration
- [ ] Action templates / runbook execution
- [ ] Web UI dashboard
- [ ] Multi-tenant support

---

## Performance Notes

- **Async throughout**: No blocking I/O in main loop
- **Memory auto-evict**: Prevents unbounded growth
- **Timeout**: MCP calls timeout at 120s, retry 3× on transient errors
- **Pooled HTTP client**: Reused per MCPClient instance
- **Frozen snapshot**: System prompt context stable (no re-compute mid-session)

---

## Security Checklist

- ✅ Memory content scanning (blocks injection/exfil)
- ✅ KQL injection prevention (parameterized queries)
- ✅ Keyring integration (OS-backed credential storage)
- ✅ HMAC-SHA256 webhook verification (ICM)
- ✅ Optional API key enforcement (X-API-Key header)
- ✅ JSON logging (no PII by default)
- ✅ File permissions (credentials file 0o600)

