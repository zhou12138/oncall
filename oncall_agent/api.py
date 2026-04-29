"""HTTP API — FastAPI entrypoint for OnCall Agent.

The /trigger endpoint accepts raw incident metadata as a natural language string.
The LLM reasons over it to extract signal, determine steps, and drive the pipeline.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import json
import httpx

from oncall_agent.orchestrator import OncallOrchestrator
from oncall_agent.copilot_proxy import get_proxy
from oncall_agent.memory.store import OncallMemory
from oncall_agent.workspace import WorkspaceManager
from oncall_agent.config import config
from oncall_agent.errors import (
    MCPError,
    OncallError,
    ReasoningError,
    TriageError,
    WoWError,
)
from oncall_agent.logging_config import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

app = FastAPI(title="OnCall Agent", version="0.1.0")
orchestrator = OncallOrchestrator()


# ── Request / Response ───────────────────────────────────────────────────────

class TriggerRequest(BaseModel):
    """HTTP trigger payload — raw incident metadata as natural language."""
    query: str                    # raw ICM / incident metadata serialized as string
    workspace: str = ""           # workspace name (optional, uses active if empty)
    model: str = ""               # override LLM model


class TriggerResponse(BaseModel):
    query: str
    workspace: str
    intent: dict                  # LLM-extracted intent
    severity: str
    summary: str
    actions: list[str]
    steps: dict
    raw_reasoning: str


# ── Intent Extraction ────────────────────────────────────────────────────────

INTENT_SYSTEM_PROMPT = """You are an OnCall triage router. Given raw incident metadata (ICM, alert, or free-text query), extract structured intent.

Output ONLY valid JSON:
{
  "signal_name": "the primary signal or alert name",
  "description": "one-line description of the issue",
  "repo": "owner/repo if mentioned, else empty string",
  "teams_channel": "teams channel if mentioned, else empty string",
  "severity_hint": "critical|high|medium|low|info|unknown",
  "should_run_triage": true,
  "should_run_wow": true,
  "should_notify": true,
  "kusto_hints": ["any specific table or query hints from the metadata"],
  "key_entities": ["service names", "component names", "regions", "etc"]
}

Extract as much as possible. If the metadata is vague, make reasonable inferences.
signal_name should be a concise identifier (e.g. "EdgeCrashRate", "HighCPU_WestUS2").
"""

WORKSPACE_CONTEXT_TEMPLATE = """## Active Workspace Context
{workspace_context}
"""


async def extract_intent(query: str, workspace_context: str = "", model: str = None) -> dict:
    """Use LLM to extract structured intent from raw incident metadata."""
    messages = [
        {"role": "system", "content": INTENT_SYSTEM_PROMPT},
    ]
    if workspace_context:
        messages.append({"role": "system", "content": WORKSPACE_CONTEXT_TEMPLATE.format(
            workspace_context=workspace_context[:2000]
        )})
    messages.append({"role": "user", "content": query})

    proxy = get_proxy()
    resp = await proxy.chat_completion(
        messages=messages,
        model=model or config.llm_model,
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    content = resp["choices"][0]["message"]["content"]

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {
            "signal_name": "UnparsedIncident",
            "description": query[:200],
            "repo": "",
            "teams_channel": "",
            "severity_hint": "unknown",
            "should_run_triage": True,
            "should_run_wow": True,
            "should_notify": True,
            "kusto_hints": [],
            "key_entities": [],
        }


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.post("/trigger", response_model=TriggerResponse)
async def trigger_oncall(req: TriggerRequest):
    """Trigger oncall pipeline from raw incident metadata.
    
    The query can be anything:
    - Raw ICM JSON serialized as string
    - Alert webhook payload
    - Free-text description
    - Kusto query results
    
    The LLM extracts intent and drives the pipeline.
    
    Examples:
        curl -X POST http://localhost:8090/trigger \\
          -H "Content-Type: application/json" \\
          -d '{"query": "ICM 12345678: Edge crash rate spiked 40% in WestUS2, component: BrowserCore, severity: 2, first seen 2024-03-15T08:00Z, impacting 50k users"}'
        
        curl -X POST http://localhost:8090/trigger \\
          -d '{"query": "HighMemoryAlert fired for edge-renderer process, P95 memory usage crossed 2GB threshold, detected by Geneva monitor EdgeRendererMemory"}'
    """
    try:
        # Resolve workspace
        ws_name = req.workspace or WorkspaceManager.get_active() or ""
        ws_context = ""
        ws = None
        if ws_name:
            ws = WorkspaceManager.get(ws_name)
            if ws.exists:
                ws_context = ws.get_llm_context()

        model = req.model or None

        # Step 0: LLM extracts intent from raw metadata
        intent = await extract_intent(req.query, ws_context, model)

        # Run pipeline based on intent
        result = await orchestrator.run(
            signal_name=intent.get("signal_name", "UnknownSignal"),
            repo=intent.get("repo", ""),
            teams_channel=intent.get("teams_channel", ""),
            model=model,
            # Pass intent extras for enriched pipeline
            intent=intent,
            raw_query=req.query,
        )

        # Write to workspace memory
        if ws and ws.exists:
            ws.append_memory("Recent Incidents",
                f"**{intent.get('signal_name', '')}** — "
                f"{result.get('severity', 'unknown').upper()}\n"
                f"- Query: {req.query[:200]}\n"
                f"- Intent: {json.dumps(intent, default=str)[:300]}\n"
                f"- Summary: {result.get('summary', '')}\n"
                f"- Actions: {', '.join(result.get('actions', []))}"
            )

        return TriggerResponse(
            query=req.query,
            workspace=ws_name,
            intent=intent,
            severity=result.get("severity", "unknown"),
            summary=result.get("summary", ""),
            actions=result.get("actions", []),
            steps=result.get("steps", {}),
            raw_reasoning=result.get("steps", {}).get("analysis", {}).get("reasoning", ""),
        )

    except ValueError as e:
        # Input validation (e.g. sanitize_signal_name) → Unprocessable Entity
        raise HTTPException(status_code=422, detail=str(e))
    except (TriageError, WoWError, MCPError) as e:
        # Upstream MCP / data dependency failure
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")
    except ReasoningError as e:
        raise HTTPException(status_code=502, detail=f"ReasoningError: {e}")
    except OncallError as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    ws = WorkspaceManager.get_active() or "(none)"
    return {"status": "ok", "version": "0.1.0", "active_workspace": ws}


@app.get("/memory")
async def get_memory(workspace: str = ""):
    """View memory — from workspace memory.md or JSON store."""
    if workspace:
        ws = WorkspaceManager.get(workspace)
        if ws.exists:
            return {"source": f"workspace:{workspace}", "memory_md": ws.read_memory()}
    memory = OncallMemory(config.memory_path)
    return {"source": "global", "data": memory.data}


@app.get("/workspaces")
async def list_workspaces():
    return {
        "workspaces": WorkspaceManager.list_workspaces(),
        "active": WorkspaceManager.get_active(),
    }


def main():
    import uvicorn
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
