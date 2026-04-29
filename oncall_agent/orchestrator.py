"""Orchestrator — runs the 3-step oncall pipeline.

The pipeline is provider-agnostic: any object satisfying
``oncall_agent.providers.DataProvider`` (mock, MCP-backed, or future
providers) can drive triage / WoW / reasoning. ``select_provider``
picks MCP when any MCP server is configured, otherwise the mock
provider, so this orchestrator no longer needs separate code paths.
"""

from oncall_agent.memory.store import OncallMemory
from oncall_agent.models.incident import Incident
from oncall_agent.providers import DataProvider, select_provider
from oncall_agent.routing import route_incident
from oncall_agent.config import config
from oncall_agent.errors import (
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
from oncall_agent.trace import RunTrace

logger = get_logger(__name__)


# Module-level incident registry. run_id → Incident. The HTTP API reads this
# to expose /incidents and to drive state transitions from action callbacks.
_incidents: dict[str, Incident] = {}


def get_incident(run_id: str) -> Incident | None:
    return _incidents.get(run_id)


def list_incidents() -> list[Incident]:
    return list(_incidents.values())


class OncallOrchestrator:
    """Drives the 3-step oncall analysis pipeline through a DataProvider."""

    def __init__(self, provider: DataProvider | None = None):
        self.memory = OncallMemory(config.memory_path)
        self.provider: DataProvider = provider or select_provider()

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
        """Execute the pipeline using the configured provider."""
        rid = run_id or new_run_id()
        trace = RunTrace(run_id=rid, signal_name=signal_name)
        with run_id_scope(rid):
            run_started = now_ms()
            logger.info(
                "run.started",
                extra={
                    "event": "run.started",
                    "run_id": rid,
                    "signal_name": signal_name,
                    "mode": self.provider.mode,
                },
            )
            try:
                result = await self._run_pipeline(
                    self.provider,
                    signal_name=signal_name,
                    repo=repo,
                    teams_channel=teams_channel,
                    model=model,
                    intent=intent,
                    raw_query=raw_query,
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
        """Run a single step coroutine with logging + error wrapping."""
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

    async def _run_pipeline(
        self,
        provider: DataProvider,
        *,
        signal_name: str,
        repo: str,
        teams_channel: str,
        model: str | None,
        intent: dict | None,
        raw_query: str,
        trace: RunTrace | None,
    ) -> dict:
        """Single pipeline body. Provider supplies the data; this method
        owns logging, skip-handling, error wrapping and memory persistence.
        """
        result = {"signal_name": signal_name, "steps": {}, "mode": provider.mode}
        intent = intent or {}

        extra_context = ""
        if raw_query:
            extra_context += f"\n\n## Original Incident Metadata\n{raw_query}"
        if intent.get("key_entities"):
            extra_context += f"\n\n## Key Entities\n{', '.join(intent['key_entities'])}"
        if intent.get("kusto_hints"):
            extra_context += f"\n\n## Kusto Hints\n{', '.join(intent['kusto_hints'])}"

        # ── Step 1: Triage ───────────────────────────────────────────────
        if intent.get("should_run_triage", True):
            triage = await self._run_step(
                "triage", signal_name,
                lambda: provider.triage(signal_name),
                TriageError,
                trace=trace,
                result_summary_fn=lambda r: f"verdict={r.get('verdict')}",
            )
            result["steps"]["triage"] = triage
            logger.info(
                "triage.detail",
                extra={"event": "triage.detail",
                       "signal_name": signal_name,
                       "verdict": triage.get("verdict")},
            )
            try:
                log_ctx = await provider.enrich(signal_name)
            except Exception as e:  # noqa: BLE001 — enrichment is best-effort
                logger.warning(
                    "enrich.failed",
                    extra={"event": "enrich.failed",
                           "signal_name": signal_name,
                           "error": f"{type(e).__name__}: {e}"},
                )
                log_ctx = ""
            if log_ctx:
                extra_context += log_ctx
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

        # ── Step 2: WoW ─────────────────────────────────────────────────
        if intent.get("should_run_wow", True):
            wow = await self._run_step(
                "wow", signal_name,
                lambda: provider.wow_compare(signal_name, repo),
                WoWError,
                trace=trace,
                result_summary_fn=lambda r: f"{r.get('trend')} {r.get('change_percent')}%",
            )
            result["steps"]["wow"] = wow
            logger.info(
                "wow.detail",
                extra={"event": "wow.detail",
                       "signal_name": signal_name,
                       "current_count": wow.get("current_count"),
                       "previous_count": wow.get("previous_count"),
                       "trend": wow.get("trend"),
                       "change_percent": wow.get("change_percent")},
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

        # ── Step 3: Reason + (optional) notify ──────────────────────────
        should_notify = intent.get("should_notify", bool(teams_channel))
        rid = trace.run_id if trace is not None else ""
        analysis = await self._run_step(
            "reason", signal_name,
            lambda: provider.reason_and_act(
                result["steps"]["triage"], result["steps"]["wow"],
                teams_channel if should_notify else "",
                memory=self.memory, model=model,
                extra_context=extra_context, run_id=rid,
            ),
            ReasoningError,
            trace=trace,
            result_summary_fn=lambda r: (
                f"severity={r.get('severity')} "
                f"teams_sent={r.get('teams_sent', False)}"
            ),
        )
        result["steps"]["analysis"] = analysis
        logger.info(
            "reason.detail",
            extra={"event": "reason.detail",
                   "signal_name": signal_name,
                   "severity": analysis.get("severity"),
                   "teams_sent": analysis.get("teams_sent", False)},
        )

        result["severity"] = analysis.get("severity", "unknown")
        result["summary"] = analysis.get("summary", "")
        result["actions"] = analysis.get("actions", [])

        # Compact final record (mock path didn't get one inside step3).
        if provider.mode == "mock":
            self.memory.record(signal_name, result)

        return result
