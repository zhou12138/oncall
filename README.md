# OnCall Agent 🚨

HTTP trigger → 3-step orchestration → MCP (ADX Kusto + GitHub + Teams)

## Architecture

```
HTTP POST /trigger
    │
    ▼
┌─────────────────────────────────┐
│         Orchestrator            │
│                                 │
│  Step 1: Triage                 │
│  ├─ ADX Kusto query             │
│  └─ Global first vs Windows?    │
│                                 │
│  Step 2: WoW 环比               │
│  ├─ ADX: current vs prev week   │
│  └─ GitHub: correlated PRs      │
│                                 │
│  Step 3: Reason + Act           │
│  ├─ LLM reasoning over data     │
│  ├─ Memory context injection    │
│  ├─ Summary + severity          │
│  └─ Teams notification          │
└─────────────────────────────────┘
    │
    ▼
  Memory (JSON) ← learns from each run
```

## Quick Start

```bash
cd ~/oncall
pip install -e .
python -m oncall_agent.api
```

## Trigger

```bash
curl -X POST http://localhost:8090/trigger \
  -H "Content-Type: application/json" \
  -d '{
    "signal_name": "HighCPUAlert",
    "repo": "microsoft/edge",
    "teams_channel": "oncall-alerts"
  }'
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_API_BASE` | `https://api.openai.com/v1` | LLM API endpoint |
| `LLM_API_KEY` | — | API key for LLM |
| `LLM_MODEL` | `gpt-4o` | Model name |
| `ADX_MCP_URL` | `http://localhost:8091/sse` | ADX Kusto MCP server |
| `GITHUB_MCP_URL` | `http://localhost:8092/sse` | GitHub MCP server |
| `TEAMS_MCP_URL` | `http://localhost:8093/sse` | Teams MCP server |
| `MEMORY_PATH` | `./memory/oncall_memory.json` | Memory store path |

## Endpoints

- `POST /trigger` — Run oncall pipeline
- `GET /health` — Health check
- `GET /memory` — View memory
- `DELETE /memory/{section}` — Clear memory section
