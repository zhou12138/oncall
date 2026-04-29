"""Structured (JSON) logging for oncall_agent.

Uses only the stdlib ``logging`` module. Adds:

* JSON formatter — every record is a single JSON object.
* ``run_id`` correlation — module-level :data:`run_id_var` (a
  :class:`contextvars.ContextVar`) is attached to every record via a
  :class:`logging.Filter`.  Set/clear it with :func:`set_run_id` /
  :func:`clear_run_id` (or use the context manager :func:`run_id_scope`).
* ``log_step_event(...)`` helper — emit ``step.started`` / ``step.completed``
  / ``step.failed`` events with the standard fields the orchestrator and
  steps share (signal_name, duration_ms, step_name, status).

Call :func:`configure_logging` once at process start (api.py / cli.py).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
import time
import uuid
from typing import Any, Iterator, Optional

# Correlation ID for the active pipeline run. Set by the orchestrator (or
# manually via run_id_scope) and propagated to every log record.
run_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "oncall_run_id", default=None
)


# ── Run-id helpers ───────────────────────────────────────────────────────────


def new_run_id() -> str:
    """Generate a fresh run id (UUID4 string)."""
    return str(uuid.uuid4())


def set_run_id(run_id: str) -> contextvars.Token:
    """Set the active run id; returns a token to reset() with."""
    return run_id_var.set(run_id)


def clear_run_id(token: contextvars.Token) -> None:
    """Reset the run id contextvar using a previously returned token."""
    run_id_var.reset(token)


@contextlib.contextmanager
def run_id_scope(run_id: Optional[str] = None) -> Iterator[str]:
    """Context manager — sets a run_id for the duration of the block."""
    rid = run_id or new_run_id()
    token = set_run_id(rid)
    try:
        yield rid
    finally:
        clear_run_id(token)


# ── Filter & Formatter ───────────────────────────────────────────────────────


class _RunIdFilter(logging.Filter):
    """Attach the current run_id (if any) to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        return True


# Standard library LogRecord attributes; anything *else* on the record is
# treated as structured payload by the JSON formatter.
_STD_RECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class JSONFormatter(logging.Formatter):
    """Render every record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        run_id = getattr(record, "run_id", None)
        if run_id:
            payload["run_id"] = run_id

        # Any extra={…} kwargs become top-level fields
        for k, v in record.__dict__.items():
            if k in _STD_RECORD_KEYS or k == "run_id":
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except TypeError:
                payload[k] = repr(v)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


# ── Public API ───────────────────────────────────────────────────────────────


_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    """Install JSON formatter + run_id filter on the root logger.

    Idempotent — safe to call multiple times.
    """
    global _CONFIGURED
    root = logging.getLogger()
    root.setLevel(level)

    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(_RunIdFilter())

    # Replace any pre-existing handlers — we want one canonical stream.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)

    _CONFIGURED = True


def setup_logging(level: str | int = "INFO") -> None:
    """Spec-friendly alias for :func:`configure_logging`.

    Accepts either a string level name (``"INFO"``) or an int.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    configure_logging(level=level)


def get_logger(name: str) -> logging.Logger:
    """Convenience accessor — ensures setup_logging() ran first."""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)


# ── Step event helper ────────────────────────────────────────────────────────


def log_step_event(
    logger: logging.Logger,
    event: str,
    *,
    step_name: str,
    signal_name: str = "",
    duration_ms: Optional[float] = None,
    status: Optional[str] = None,
    error: Optional[str] = None,
    **extra: Any,
) -> None:
    """Emit a structured ``step.<event>`` log line.

    Conventions:
      * ``event`` is one of ``"started" | "completed" | "failed"``.
      * On ``failed``, pass ``error=<str>``; level is ERROR.
      * ``duration_ms`` should be present on completed/failed.
    """
    level = logging.ERROR if event == "failed" else logging.INFO
    fields: dict[str, Any] = {
        "event": f"step.{event}",
        "step_name": step_name,
        "signal_name": signal_name,
    }
    if duration_ms is not None:
        fields["duration_ms"] = round(duration_ms, 3)
    if status is not None:
        fields["status"] = status
    if error is not None:
        fields["error"] = error
    fields.update(extra)
    logger.log(level, f"step.{event} {step_name}", extra=fields)


def now_ms() -> float:
    """Monotonic millisecond timestamp for step duration measurement."""
    return time.monotonic() * 1000.0
