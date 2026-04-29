"""Run / step trace model.

A :class:`RunTrace` captures the lifecycle of one pipeline run: when it
started, the per-step :class:`StepTrace` records, the terminal status, and
total duration. The orchestrator builds a RunTrace as it executes and the
HTTP layer surfaces it in the response (and via /runs/{run_id}).

These dataclasses are JSON-serializable through :meth:`RunTrace.to_dict`.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ── Status constants ─────────────────────────────────────────────────────────

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


def _utc_iso() -> str:
    """Current time as ISO-8601 UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _now_ms() -> float:
    """Monotonic wall clock in ms."""
    return time.monotonic() * 1000.0


# ── StepTrace ────────────────────────────────────────────────────────────────


@dataclass
class StepTrace:
    """Trace for a single pipeline step."""

    name: str
    started_at: str = field(default_factory=_utc_iso)
    completed_at: Optional[str] = None
    duration_ms: Optional[float] = None
    status: str = STATUS_RUNNING
    result_summary: Optional[str] = None
    error: Optional[str] = None

    # Internal monotonic anchor — not serialized.
    _t0: float = field(default_factory=_now_ms, repr=False, compare=False)

    def mark_completed(
        self, *, result_summary: Optional[str] = None,
        status: str = STATUS_COMPLETED,
    ) -> None:
        self.completed_at = _utc_iso()
        self.duration_ms = round(_now_ms() - self._t0, 3)
        self.status = status
        if result_summary is not None:
            self.result_summary = result_summary

    def mark_failed(self, error: str) -> None:
        self.completed_at = _utc_iso()
        self.duration_ms = round(_now_ms() - self._t0, 3)
        self.status = STATUS_FAILED
        self.error = error

    def mark_skipped(self, reason: str = "") -> None:
        self.completed_at = _utc_iso()
        self.duration_ms = round(_now_ms() - self._t0, 3)
        self.status = STATUS_SKIPPED
        if reason:
            self.result_summary = reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "result_summary": self.result_summary,
            "error": self.error,
        }


# ── RunTrace ─────────────────────────────────────────────────────────────────


@dataclass
class RunTrace:
    """Trace for an entire pipeline run."""

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    signal_name: str = ""
    started_at: str = field(default_factory=_utc_iso)
    completed_at: Optional[str] = None
    duration_ms: Optional[float] = None
    status: str = STATUS_RUNNING
    steps: list[StepTrace] = field(default_factory=list)
    error: Optional[str] = None

    _t0: float = field(default_factory=_now_ms, repr=False, compare=False)

    def start_step(self, name: str) -> StepTrace:
        """Append a new running StepTrace and return it."""
        step = StepTrace(name=name)
        self.steps.append(step)
        return step

    def mark_completed(self, status: str = STATUS_COMPLETED) -> None:
        self.completed_at = _utc_iso()
        self.duration_ms = round(_now_ms() - self._t0, 3)
        self.status = status

    def mark_failed(self, error: str) -> None:
        self.completed_at = _utc_iso()
        self.duration_ms = round(_now_ms() - self._t0, 3)
        self.status = STATUS_FAILED
        self.error = error

    @property
    def total_duration_ms(self) -> Optional[float]:
        """Alias for duration_ms — matches Phase 1 spec naming."""
        return self.duration_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "signal_name": self.signal_name,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "total_duration_ms": self.duration_ms,
            "status": self.status,
            "error": self.error,
            "steps": [s.to_dict() for s in self.steps],
        }
