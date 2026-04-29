"""Orchestrator — runs the 3-step oncall pipeline.

Supports two modes:
  1. Full MCP: connectors available → query ADX, GitHub, Teams
  2. Mock + LLM: No MCP → mock data for triage/WoW, LLM for reasoning
"""

from oncall_agent.memory.store import OncallMemory
from oncall_agent.models.incident import Incident
from oncall_agent.routing import route_incident
from oncall_agent.config import config
from oncall_agent.copilot_proxy import get_proxy
from oncall_agent.errors import (
    MCPError,
    OncallError,
    ReasoningError,
    TriageError,
    WoWError,
)
from oncall_agent.logging_config import (
    get_logger,
    log_step_event,
    new_run_id,
    now_ms,
    run_id_scope,
)
from oncall_agent.trace import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    RunTrace,
)

logger = get_logger(__name__)


# Module-level incident registry. run_id → Incident. The HTTP API reads this
# to expose /incidents and to drive state transitions from action callbacks.
_incidents: dict[str, Incident] = {}


def get_incident(run_id: str) -> Incident | None:
    return _incidents.get(run_id)


def list_incidents() -> list[Incident]:
    return list(_incidents.values())


class OncallOrchestrator:
    """Orchestrates the 3-step oncall analysis pipeline."""

    def __init__(self):
        self.memory = OncallMemory(config.memory_path)
        self._mcp_available = bool(
            config.adx_mcp.url or config.github_mcp.url or config.teams_mcp.url
        )

    async def run(
        self,
        signal_name: str,
        repo: str = "",
        teams_channel: str = "",
        model: str = None,
        intent: dict = None,
        raw_query: str = "",
        run_id: str | None = None,
    ) -> dict:
        """Execute the pipeline. Uses mock data if MCP unavailable."""
        rid = run_id or new_run_id()
        trace = RunTrace(run_id=rid, signal_name=signal_name)
        with run_id_scope(rid):
            run_started = now_ms()
            mode = "mcp" if self._mcp_available else "mock"
            logger.info(
                "run.started",
                extra={
                    "event": "run.started",
                    "run_id": rid,
                    "signal_name": signal_name,
                    "mode": mode,
                },
            )
            try:
                if self._mcp_available:
                    result = await self._run_with_mcp(
                        signal_name, repo, teams_channel, model, intent, raw_query,
                        trace=trace,
                    )
                else:
                    result = await self._run_with_mock(
                        signal_name, repo, teams_channel, model, intent, raw_query,
                        trace=trace,
                    )
            except Exception as e:
                trace.mark_failed(f"{type(e).__name__}: {e}")
                logger.error(
                    "run.failed",
                    extra={
                        "event": "run.failed",
                        "run_id": rid,
                        "signal_name": signal_name,
                        "duration_ms": round(now_ms() - run_started, 3),
                        "error": f"{type(e).__name__}: {e}",
                    },
                )
                raise
            trace.mark_completed()
            result["run_id"] = rid
            result["trace"] = trace.to_dict()
            # Register the incident so the API can list/transition it.
            owner = route_incident(signal_name)
            incident = Incident(
                run_id=rid,
                signal_name=signal_name,
                severity=str(result.get("severity", "unknown")),
                owner=owner,
            )
            try:
                incident.transition("triaged", by="orchestrator")
            except ValueError:
                pass
            _incidents[rid] = incident
            result["owner"] = owner
            result["incident"] = incident.to_dict()
            logger.info(
                "run.completed",
                extra={
                    "event": "run.completed",
                    "run_id": rid,
                    "signal_name": signal_name,
                    "duration_ms": round(now_ms() - run_started, 3),
                    "severity": result.get("severity"),
                },
            )
            return result

    async def _run_step(
        self,
        step_name: str,
        signal_name: str,
        coro_factory,
        wrap_error: type[OncallError],
        trace: RunTrace | None = None,
        result_summary_fn=None,
    ):
        """Run a single step coroutine with logging + error wrapping.

        ``coro_factory`` is a zero-arg callable returning an awaitable, so we
        avoid creating the coroutine before logging step.started.
        ``result_summary_fn`` (optional) takes the step result and returns a
        short string written to the StepTrace.result_summary field.
        """
        step = trace.start_step(step_name) if trace is not None else None
        log_step_event(logger, "started", step_name=step_name, signal_name=signal_name)
        t0 = now_ms()
        try:
            result = await coro_factory()
        except (OncallError, ValueError) as e:
            err = f"{type(e).__name__}: {e}"
            if step is not None:
                step.mark_failed(err)
            log_step_event(
                logger, "failed",
                step_name=step_name, signal_name=signal_name,
                duration_ms=now_ms() - t0, error=err,
            )
            raise
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if step is not None:
                step.mark_failed(err)
            log_step_event(
                logger, "failed",
                step_name=step_name, signal_name=signal_name,
                duration_ms=now_ms() - t0, error=err,
            )
            raise wrap_error(f"{step_name} failed: {e}") from e
        summary = None
        if result_summary_fn is not None:
            try:
                summary = result_summary_fn(result)
            except Exception:
                summary = None
        if step is not None:
            step.mark_completed(result_summary=summary)
        log_step_event(
            logger, "completed",
            step_name=step_name, signal_name=signal_name,
            duration_ms=now_ms() - t0, status="ok",
        )
        return result

    async def _run_with_mock(
        self,
        signal_name: str,
        repo: str = "",
        teams_channel: str = "",
        model: str = None,
        intent: dict = None,
        raw_query: str = "",
        trace: RunTrace | None = None,
    ) -> dict:
        """3-step pipeline with mock data + LLM reasoning."""
        from oncall_agent.connectors.mock import mock_triage, mock_wow
        from oncall_agent.steps.step3_reason import step_reason_and_act

        result = {"signal_name": signal_name, "steps": {}, "mode": "mock"}
        intent = intent or {}

        extra_context = ""
        if raw_query:
            extra_context += f"\n\n## Original Incident Metadata\n{raw_query}"

        # Step 1: Triage (mock) — synchronous, but log the event the same way.
        async def _triage():
            return mock_triage(signal_name)

        triage = await self._run_step(
            "triage", signal_name, _triage, TriageError,
            trace=trace,
            result_summary_fn=lambda r: f"verdict={r.get('verdict')}",
        )
        result["steps"]["triage"] = triage
        details = triage["details"]
        logger.info(
            "triage.detail",
            extra={
                "event": "triage.detail",
                "signal_name": signal_name,
                "verdict": triage["verdict"],
                "windows_percentage": details.get("windows_percentage", 0),
                "platform_breakdown": details.get("platform_breakdown", {}),
            },
        )

        async def _wow():
            return mock_wow(signal_name, repo)

        wow = await self._run_step(
            "wow", signal_name, _wow, WoWError,
            trace=trace,
            result_summary_fn=lambda r: f"{r.get('trend')} {r.get('change_percent')}%",
        )
        result["steps"]["wow"] = wow
        logger.info(
            "wow.detail",
            extra={
                "event": "wow.detail",
                "signal_name": signal_name,
                "current_count": wow["current_count"],
                "previous_count": wow["previous_count"],
                "trend": wow["trend"],
                "change_percent": wow["change_percent"],
            },
        )

        async def _reason():
            return await step_reason_and_act(
                None, self.memory, triage, wow, "",
                model=model, extra_context=extra_context,
                run_id=trace.run_id if trace is not None else "",
            )

        analysis = await self._run_step(
            "reason", signal_name, _reason, ReasoningError,
            trace=trace,
            result_summary_fn=lambda r: f"severity={r.get('severity')}",
        )
        result["steps"]["analysis"] = analysis
        logger.info(
            "reason.detail",
            extra={
                "event": "reason.detail",
                "signal_name": signal_name,
                "severity": analysis["severity"],
            },
        )

        result["severity"] = analysis["severity"]
        result["summary"] = analysis["summary"]
        result["actions"] = analysis["actions"]

        # Save to memory
        self.memory.record(signal_name, result)

        return result

    async def _run_with_mcp(
        self,
        signal_name: str,
        repo: str = "",
        teams_channel: str = "",
        model: str = None,
        intent: dict = None,
        raw_query: str = "",
        trace: RunTrace | None = None,
    ) -> dict:
        """Full pipeline with MCP connectors."""
        from oncall_agent.mcp_clients.client import MCPClient
        from oncall_agent.steps.step1_triage import step_triage
        from oncall_agent.steps.step2_wow import step_wow_compare
        from oncall_agent.steps.step3_reason import step_reason_and_act

        adx_client = MCPClient("adx-kusto", config.adx_mcp.url)
        github_client = MCPClient("github", config.github_mcp.url)
        teams_client = MCPClient("teams", config.teams_mcp.url)

        result = {"signal_name": signal_name, "steps": {}, "mode": "mcp"}
        intent = intent or {}

        extra_context = ""
        if raw_query:
            extra_context += f"\n\n## Original Incident Metadata\n{raw_query}"
        if intent.get("key_entities"):
            extra_context += f"\n\n## Key Entities\n{', '.join(intent['key_entities'])}"
        if intent.get("kusto_hints"):
            extra_context += f"\n\n## Kusto Hints\n{', '.join(intent['kusto_hints'])}"

        # Step 1
        if intent.get("should_run_triage", True):
            triage = await self._run_step(
                "triage", signal_name,
                lambda: step_triage(adx_client, signal_name),
                TriageError,
                trace=trace,
                result_summary_fn=lambda r: f"verdict={r.get('verdict')}",
            )
            result["steps"]["triage"] = triage
            logger.info(
                "triage.detail",
                extra={"event": "triage.detail",
                       "signal_name": signal_name,
                       "verdict": triage["verdict"]},
            )
        else:
            result["steps"]["triage"] = {
                "verdict": "Skipped", "details": {}, "signal_name": signal_name,
            }
            if trace is not None:
                trace.start_step("triage").mark_skipped("intent.should_run_triage=false")
            logger.info(
                "step.skipped",
                extra={"event": "step.skipped",
                       "step_name": "triage", "signal_name": signal_name},
            )

        # Step 2
        if intent.get("should_run_wow", True):
            wow = await self._run_step(
                "wow", signal_name,
                lambda: step_wow_compare(adx_client, github_client, signal_name, repo),
                WoWError,
                trace=trace,
                result_summary_fn=lambda r: f"{r.get('trend')} {r.get('change_percent')}%",
            )
            result["steps"]["wow"] = wow
            logger.info(
                "wow.detail",
                extra={"event": "wow.detail",
                       "signal_name": signal_name,
                       "trend": wow["trend"],
                       "change_percent": wow["change_percent"]},
            )
        else:
            result["steps"]["wow"] = {
                "current_count": 0, "previous_count": 0, "delta": 0,
                "change_percent": 0, "trend": "skipped", "recent_changes": [],
            }
            if trace is not None:
                trace.start_step("wow").mark_skipped("intent.should_run_wow=false")
            logger.info(
                "step.skipped",
                extra={"event": "step.skipped",
                       "step_name": "wow", "signal_name": signal_name},
            )

        # Step 3
        should_notify = intent.get("should_notify", bool(teams_channel))
        analysis = await self._run_step(
            "reason", signal_name,
            lambda: step_reason_and_act(
                teams_client, self.memory,
                result["steps"]["triage"], result["steps"]["wow"],
                teams_channel if should_notify else "",
                model=model, extra_context=extra_context,
                run_id=trace.run_id if trace is not None else "",
            ),
            ReasoningError,
            trace=trace,
            result_summary_fn=lambda r: f"severity={r.get('severity')} teams_sent={r.get('teams_sent', False)}",
        )
        result["steps"]["analysis"] = analysis
        logger.info(
            "reason.detail",
            extra={"event": "reason.detail",
                   "signal_name": signal_name,
                   "severity": analysis["severity"],
                   "teams_sent": analysis.get("teams_sent", False)},
        )

        result["severity"] = analysis["severity"]
        result["summary"] = analysis["summary"]
        result["actions"] = analysis["actions"]

        return result
