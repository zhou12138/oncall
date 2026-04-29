"""HTTP API — FastAPI entrypoint for OnCall Agent.

The /trigger endpoint accepts raw incident metadata as a natural language string.
The LLM reasons over it to extract signal, determine steps, and drive the pipeline.

In Phase 1 (M1.3), /trigger is asynchronous: it returns 202 Accepted with a
``run_id`` and the pipeline runs in the background. Clients poll
``GET /runs/{run_id}`` for the trace and final result.
"""

import asyncio
import json
import uuid
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from oncall_agent.orchestrator import OncallOrchestrator, get_incident, list_incidents
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
from oncall_agent.logging_config import configure_logging, get_logger, new_run_id
from oncall_agent.trace import RunTrace, STATUS_FAILED

configure_logging()
logger = get_logger(__name__)

app = FastAPI(title="OnCall Agent", version="0.1.0")
orchestrator = OncallOrchestrator()


# ── In-memory run store ──────────────────────────────────────────────────────
# Maps run_id → {"status", "trace": dict, "result": dict | None, "error": str | None}.
# Process-local; Phase 2 may swap this for a durable store.
_runs: dict[str, dict] = {}


def _record_run(run_id: str, **fields) -> None:
    entry = _runs.setdefault(run_id, {})
    entry.update(fields)


# ── Request / Response ───────────────────────────────────────────────────────

class TriggerRequest(BaseModel):
    """HTTP trigger payload — raw incident metadata as natural language."""
    query: str                    # raw ICM / incident metadata serialized as string
    workspace: str = ""           # workspace name (optional, uses active if empty)
    model: str = ""               # override LLM model


class TriggerAcceptedResponse(BaseModel):
    """Returned with HTTP 202 when the pipeline is dispatched asynchronously."""
    run_id: str
    status: str = "accepted"
    poll_url: str


class RunStatusResponse(BaseModel):
    run_id: str
    status: str                # accepted | running | completed | failed
    trace: dict = {}
    result: Optional[dict] = None
    error: Optional[str] = None



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


# ── Background pipeline runner ──────────────────────────────────────────────


async def _run_pipeline(run_id: str, req: TriggerRequest) -> None:
    """Drive a single pipeline run; populate _runs[run_id] as we go."""
    _record_run(run_id, status="running")
    logger.info(
        "pipeline.dispatched",
        extra={"event": "pipeline.dispatched", "run_id": run_id},
    )
    try:
        ws_name = req.workspace or WorkspaceManager.get_active() or ""
        ws_context = ""
        ws = None
        if ws_name:
            ws = WorkspaceManager.get(ws_name)
            if ws.exists:
                ws_context = ws.get_llm_context()

        model = req.model or None
        intent = await extract_intent(req.query, ws_context, model)

        result = await orchestrator.run(
            signal_name=intent.get("signal_name", "UnknownSignal"),
            repo=intent.get("repo", ""),
            teams_channel=intent.get("teams_channel", ""),
            model=model,
            intent=intent,
            raw_query=req.query,
            run_id=run_id,
        )

        # Workspace memory
        if ws and ws.exists:
            ws.append_memory(
                "Recent Incidents",
                f"**{intent.get('signal_name', '')}** — "
                f"{result.get('severity', 'unknown').upper()}\n"
                f"- Query: {req.query[:200]}\n"
                f"- Intent: {json.dumps(intent, default=str)[:300]}\n"
                f"- Summary: {result.get('summary', '')}\n"
                f"- Actions: {', '.join(result.get('actions', []))}",
            )

        envelope = {
            "query": req.query,
            "workspace": ws_name,
            "intent": intent,
            "severity": result.get("severity", "unknown"),
            "summary": result.get("summary", ""),
            "actions": result.get("actions", []),
            "steps": result.get("steps", {}),
            "raw_reasoning": result.get("steps", {})
                .get("analysis", {}).get("reasoning", ""),
            "run_id": run_id,
            "trace": result.get("trace", {}),
        }
        _record_run(
            run_id,
            status="completed",
            trace=result.get("trace", {}),
            result=envelope,
            error=None,
        )
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logger.error(
            "pipeline.error",
            extra={"event": "pipeline.error", "run_id": run_id, "error": err},
        )
        prev = _runs.get(run_id, {})
        trace = prev.get("trace") or {}
        _record_run(
            run_id,
            status="failed",
            trace=trace,
            error=err,
            result=None,
        )


# ── Endpoints ────────────────────────────────────────────────────────────────


@app.post("/trigger", status_code=202, response_model=TriggerAcceptedResponse)
async def trigger_oncall(req: TriggerRequest, response: Response):
    """Dispatch the oncall pipeline asynchronously.

    Returns ``202 Accepted`` with a ``run_id`` immediately. The pipeline runs
    in the background; poll ``GET /runs/{run_id}`` for status and result.
    """
    run_id = new_run_id()
    _record_run(run_id, status="accepted", trace={}, result=None, error=None)
    asyncio.create_task(_run_pipeline(run_id, req))
    response.headers["Location"] = f"/runs/{run_id}"
    return TriggerAcceptedResponse(
        run_id=run_id,
        status="accepted",
        poll_url=f"/runs/{run_id}",
    )


@app.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(run_id: str):
    """Return the current trace and result for a run."""
    entry = _runs.get(run_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown run_id: {run_id}")
    return RunStatusResponse(
        run_id=run_id,
        status=entry.get("status", "unknown"),
        trace=entry.get("trace") or {},
        result=entry.get("result"),
        error=entry.get("error"),
    )


@app.get("/health")
async def health():
    ws = WorkspaceManager.get_active() or "(none)"
    return {"status": "ok", "version": "0.1.0", "active_workspace": ws}


# ── Action callbacks (Adaptive Card buttons) ────────────────────────────────

_VALID_ACTIONS = {"ack", "escalate"}


@app.post("/actions/{run_id}/{action}")
async def post_action(run_id: str, action: str):
    """Record an Adaptive Card action (ack/escalate) against a run.

    For ``ack`` we drive the Incident state machine to ``acknowledged``;
    for ``escalate`` we drive it to ``escalated``. State transition errors
    surface as 409 Conflict so the caller can see the current status.
    """
    if action not in _VALID_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown action: {action} (allowed: {sorted(_VALID_ACTIONS)})",
        )
    entry = _runs.get(run_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown run_id: {run_id}")
    actions_log = entry.setdefault("actions_log", [])
    actions_log.append({"action": action})

    incident = get_incident(run_id)
    new_status = "acknowledged" if action == "ack" else "escalated"
    transition_error: Optional[str] = None
    if incident is not None:
        try:
            incident.transition(new_status, by=f"action:{action}")
        except ValueError as e:
            transition_error = str(e)

    if action == "ack":
        entry["acknowledged"] = True
    elif action == "escalate":
        entry["escalated"] = True

    if transition_error:
        raise HTTPException(status_code=409, detail=transition_error)
    return {
        "run_id": run_id,
        "action": action,
        "status": "recorded",
        "incident_status": incident.status if incident else None,
    }


@app.get("/incidents")
async def get_incidents():
    """List all incidents tracked by the orchestrator."""
    return {"incidents": [inc.to_dict() for inc in list_incidents()]}


@app.get("/incidents/{run_id}")
async def get_incident_endpoint(run_id: str):
    """Return a single incident by run_id."""
    inc = get_incident(run_id)
    if inc is None:
        raise HTTPException(status_code=404, detail=f"unknown run_id: {run_id}")
    return inc.to_dict()


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
