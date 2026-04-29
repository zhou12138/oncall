"""ADX log + stacktrace enrichment.

Queries the ADX MCP for recent exceptions matching a signal, then formats
the top results as a compact text blob suitable for injection into the
LLM prompt's ``extra_context``.

Failures are non-fatal: if ADX is unavailable, parsing fails, or no rows
come back, an empty string (or a short placeholder) is returned so the
pipeline can still complete.
"""

from __future__ import annotations

import logging

from oncall_agent.utils.parsing import ParseError, parse_mcp_text
from oncall_agent.utils.sanitize import sanitize_signal_name

logger = logging.getLogger(__name__)


_LOG_QUERY_TEMPLATE = """
let SignalName = '{signal_name}';
let Window = {time_window};
ExceptionTable
| where Timestamp > ago(Window)
| where Message has SignalName or StackTrace has SignalName
| top 3 by Timestamp desc
| project Timestamp, Message, StackTrace
"""


def _summarize_text(text: str, max_len: int = 1500) -> str:
    """Compact a multi-line MCP text payload to a bounded snippet."""
    text = text.strip()
    if len(text) <= max_len:
        return text
    head = text[: max_len - 80]
    return head + f"\n... [truncated, total {len(text)} chars]"


async def enrich_with_logs(
    adx_client,
    signal_name: str,
    time_window: str = "1h",
) -> str:
    """Return a markdown-formatted log/stacktrace context block, or ''.

    ``time_window`` is a Kusto duration literal (e.g. ``1h``, ``30m``).
    Only digits + lowercase d/h/m/s are accepted to keep the value out of
    the SQL/Kusto injection surface.
    """
    if adx_client is None or not signal_name:
        return ""

    safe_signal = sanitize_signal_name(signal_name)
    safe_window = "".join(
        c for c in str(time_window) if c.isdigit() or c in ("d", "h", "m", "s")
    )
    if not safe_window:
        safe_window = "1h"

    query = _LOG_QUERY_TEMPLATE.format(
        signal_name=safe_signal, time_window=safe_window
    )

    try:
        result = await adx_client.call_tool("execute_query", {"query": query})
    except Exception as e:  # noqa: BLE001 — non-fatal enrichment
        logger.warning(
            "log_enricher.query_failed",
            extra={"event": "log_enricher.query_failed",
                   "signal_name": safe_signal,
                   "error": f"{type(e).__name__}: {e}"},
        )
        return ""

    try:
        text = parse_mcp_text(result)
    except ParseError as e:
        logger.warning(
            "log_enricher.parse_failed",
            extra={"event": "log_enricher.parse_failed",
                   "signal_name": safe_signal,
                   "error": str(e)},
        )
        return ""

    text = text.strip()
    if not text:
        return ""

    return (
        f"\n\n## Recent Exceptions / Stack Traces (last {safe_window})\n"
        f"{_summarize_text(text)}"
    )
