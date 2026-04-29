"""Orchestrator — runs the 3-step oncall pipeline.

Two execution backends share a single ``_run_pipeline`` driver, selected at
construction by the presence of MCP server URLs:

  * :class:`MCPProvider` — full mode: ADX/GitHub/Teams MCP connectors.
  * :class:`MockProvider` — fallback: deterministic mock data + LLM reasoning.

Both implement the :class:`DataProvider` :class:`~typing.Protocol`, so adding
a new backend (e.g. a recorded-fixture provider for tests) is a matter of
matching that surface — no changes to ``_run_pipeline`` required.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from oncall_agent.memory.store import OncallMemory
from oncall_agent.models.incident import Incident
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


# ─── DataProvider protocol ──────────────────────────────────────────────────


class DataProvider(Protocol):
    """Backend abstraction the unified pipeline drives.

    A provider produces step results from a signal_name + optional repo.
    Implementations are async-capable; the orchestrator awaits everything.
    """

    mode: str

    async def triage(self, signal_name: str) -> dict: ...

    async def wow(self, signal_name: str, repo: str) -> dict: ...

    async def enrich_logs(self, signal_name: str) -> str: ...

    async def reason(
        self,
        memory: OncallMemory,
        triage_result: dict,
        wow_result: dict,
        teams_channel: str,
        *,
        model: Optional[str],
        extra_context: str,
        run_id: str,
    ) -> dict: ...


# ─── Concrete providers ─────────────────────────────────────────────────────


class MockProvider:
    """Deterministic mock connectors + LLM reasoning. No MCP required."""

    mode = "mock"

    async def triage(self, signal_name: str) -> dict:
        from oncall_agent.connectors.mock import mock_triage
        return mock_triage(signal_name)

    async def wow(self, signal_name: str, repo: str) -> dict:
        from oncall_agent.connectors.mock import mock_wow
        return mock_wow(signal_name, repo)

    async def enrich_logs(self, signal_name: str) -> str:
        # No log source in mock mode.
        return ""

    async def reason(
        self,
        memory: OncallMemory,
        triage_result: dict,
        wow_result: dict,
        teams_channel: str,
        *,
        model: Optional[str],
        extra_context: str,
        run_id: str,
    ) -> dict:
        from oncall_agent.steps.step3_reason import step_reason_and_act
        return await step_reason_and_act(
            None, memory, triage_result, wow_result, "",
            model=model, extra_context=extra_context, run_id=run_id,
        )


class MCPProvider:
    """Full-mode provider backed by ADX, GitHub, and Teams MCP clients."""

    mode = "mcp"

    def __init__(self) -> None:
        from oncall_agent.mcp_clients.client import MCPClient
        self.adx = MCPClient("adx-kusto", config.adx_mcp.url)
        self.github = MCPClient("github", config.github_mcp.url)
        self.teams = MCPClient("teams", config.teams_mcp.url)

    async def triage(self, signal_name: str) -> dict:
        from oncall_agent.steps.step1_triage import step_triage
        return await step_triage(self.adx, signal_name)

    async def wow(self, signal_name: str, repo: str) -> dict:
        from oncall_agent.steps.step2_wow import step_wow_compare
        return await step_wow_compare(self.adx, self.github, signal_name, repo)

    async def enrich_logs(self, signal_name: str) -> str:
        from oncall_agent.ingestion.log_enricher import enrich_with_logs
        return await enrich_with_logs(self.adx, signal_name)

    async def reason(
        self,
        memory: OncallMemory,
        triage_result: dict,
        wow_result: dict,
        teams_channel: str,
        *,
        model: Optional[str],
        extra_context: str,
        run_id: str,
    ) -> dict:
        from oncall_agent.steps.step3_reason import step_reason_and_act
        return await step_reason_and_act(
            self.teams, memory, triage_result, wow_result, teams_channel,
            model=model, extra_context=extra_context, run_id=run_id,
        )


# ─── Orchestrator ───────────────────────────────────────────────────────────


_SKIPPED_TRIAGE = {"verdict": "Skipped", "details": {}}
_SKIPPED_WOW = {
    "current_count": 0, "previous_count": 0, "delta": 0,
    "change_percent": 0, "trend": "skipped", "recent_changes": [],
}


class OncallOrchestrator:
    """Orchestrates the 3-step oncall analysis pipeline."""

    def __init__(self) -> None:
        self.memory = OncallMemory(config.memory_path)
        self._mcp_available = bool(
            config.adx_mcp.url or config.github_mcp.url or config.teams_mcp.url
        )

    def _make_provider(self) -> DataProvider:
        return MCPProvider() if self._mcp_available else MockProvider()

    async def run(
        self,
        signal_name: str,
        repo: str = "",
        teams_channel: str = "",
        model: Optional[str] = None,
        intent: Optional[dict] = None,
        raw_query: str = "",
        run_id: Optional[str] = None,
    ) -> dict:
        rid = run_id or new_run_id()
        trace = RunTrace(run_id=rid, signal_name=signal_name)
        provider = self._make_provider()
        with run_id_scope(rid):
            run_started = now_ms()
            logger.info(
                "run.started",
                extra={
                    "event": "run.started",
                    "run_id": rid,
                    "signal_name": signal_name,
                    "mode": provider.mode,
                },
            )
            try:
                result = await self._run_pipeline(
                    provider,
                    signal_name=signal_name,
                    repo=repo,
                    teams_channel=teams_channel,
                    model=model,
                    intent=intent or {},
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
    ) -> Any:
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
        model: Optional[str],
        intent: dict,
        raw_query: str,
        trace: RunTrace,
    ) -> dict:
        """Provider-agnostic 3-step pipeline driver.

        The branching that used to live in ``_run_with_mcp`` /
        ``_run_with_mock`` is gone; provider methods supply the data, this
        function handles tracing, logging, intent-driven skips, and result
        assembly identically for every backend.
        """
        result: dict = {"signal_name": signal_name, "steps": {}, "mode": provider.mode}

        extra_context = ""
        if raw_query:
            extra_context += f"\n\n## Original Incident Metadata\n{raw_query}"
        if intent.get("key_entities"):
            extra_context += f"\n\n## Key Entities\n{', '.join(intent['key_entities'])}"
        if intent.get("kusto_hints"):
            extra_context += f"\n\n## Kusto Hints\n{', '.join(intent['kusto_hints'])}"

        # ─── Step 1: triage ────────────────────────────────────────────────
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
            # Optional log enrichment after triage (provider-specific).
            try:
                log_ctx = await provider.enrich_logs(signal_name)
            except Exception as e:  # noqa: BLE001 — non-fatal
                logger.warning(
                    "log_enricher.error",
                    extra={"event": "log_enricher.error",
                           "signal_name": signal_name,
                           "error": f"{type(e).__name__}: {e}"},
                )
                log_ctx = ""
            if log_ctx:
                extra_context += log_ctx
        else:
            result["steps"]["triage"] = {**_SKIPPED_TRIAGE, "signal_name": signal_name}
            if trace is not None:
                trace.start_step("triage").mark_skipped("intent.should_run_triage=false")
            logger.info(
                "step.skipped",
                extra={"event": "step.skipped",
                       "step_name": "triage", "signal_name": signal_name},
            )

        # ─── Step 2: WoW ───────────────────────────────────────────────────
        if intent.get("should_run_wow", True):
            wow = await self._run_step(
                "wow", signal_name,
                lambda: provider.wow(signal_name, repo),
                WoWError,
                trace=trace,
                result_summary_fn=lambda r: f"{r.get('trend')} {r.get('change_percent')}%",
            )
            result["steps"]["wow"] = wow
            logger.info(
                "wow.detail",
                extra={"event": "wow.detail",
                       "signal_name": signal_name,
                       "trend": wow.get("trend"),
                       "change_percent": wow.get("change_percent")},
            )
        else:
            result["steps"]["wow"] = dict(_SKIPPED_WOW)
            if trace is not None:
                trace.start_step("wow").mark_skipped("intent.should_run_wow=false")
            logger.info(
                "step.skipped",
                extra={"event": "step.skipped",
                       "step_name": "wow", "signal_name": signal_name},
            )

        # ─── Step 3: reason + (optional) notify ────────────────────────────
        should_notify = intent.get("should_notify", bool(teams_channel))
        effective_channel = teams_channel if should_notify else ""
        analysis = await self._run_step(
            "reason", signal_name,
            lambda: provider.reason(
                self.memory,
                result["steps"]["triage"],
                result["steps"]["wow"],
                effective_channel,
                model=model,
                extra_context=extra_context,
                run_id=trace.run_id if trace is not None else "",
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

        result["severity"] = analysis.get("severity")
        result["summary"] = analysis.get("summary", "")
        result["actions"] = analysis.get("actions", [])

        # Mock provider used to record memory inline; do it here for parity.
        if provider.mode == "mock":
            self.memory.record(signal_name, result)

        return result
