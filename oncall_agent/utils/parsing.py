"""Robust parsing helpers for MCP tool results.

MCP tools return loosely structured payloads. These helpers centralize
text extraction and metric parsing so steps don't silently coerce
malformed responses into default values.
"""

from __future__ import annotations

import json
import re
from typing import Any


class ParseError(Exception):
    """Raised when an MCP response cannot be parsed into the expected shape."""


def parse_mcp_text(result: Any) -> str:
    """Extract a text payload from an MCP tool result.

    Accepts dicts shaped like ``{"content": [{"type": "text", "text": ...}]}``.
    Returns the concatenated text of all text-typed content blocks.
    Raises :class:`ParseError` if no text content is found.
    """
    if not isinstance(result, dict):
        raise ParseError(f"expected dict result, got {type(result).__name__}")

    content = result.get("content")
    if content is None:
        raise ParseError("MCP result missing 'content' field")
    if not isinstance(content, list):
        raise ParseError(f"MCP 'content' is not a list: {type(content).__name__}")

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        # Accept blocks lacking explicit "type" but bearing a "text" string.
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)

    if not parts:
        raise ParseError("MCP result contains no text content blocks")

    return "\n".join(parts)


# Common metric keys we expect from a WoW Kusto query
_WOW_KEYS = ("CurrentWeek", "PreviousWeek", "Delta", "ChangePercent")


def parse_wow_metrics(text: str) -> dict:
    """Parse WoW metrics from an MCP text payload.

    Tries JSON parsing first (Kusto MCP often returns JSON tables); falls
    back to a labeled-key regex; finally falls back to positional numeric
    extraction. Raises :class:`ParseError` if no metrics can be recovered.

    Returns a dict with float-or-int values for keys:
    ``current_count``, ``previous_count``, ``delta``, ``change_percent``.
    """
    if not isinstance(text, str) or not text.strip():
        raise ParseError("empty text payload")

    # 1) Try strict JSON
    parsed: Any = None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None

    if parsed is not None:
        row = _first_row(parsed)
        if row is not None:
            try:
                return _coerce_wow(row)
            except ParseError:
                pass  # fall through to regex

    # 2) Labeled key regex (e.g. "CurrentWeek = 123")
    labeled: dict[str, float] = {}
    for key in _WOW_KEYS:
        m = re.search(rf"{key}\s*[=:]\s*(-?\d+(?:\.\d+)?)", text)
        if m:
            labeled[key] = float(m.group(1))
    if len(labeled) >= 3:
        return {
            "current_count": int(labeled.get("CurrentWeek", 0)),
            "previous_count": int(labeled.get("PreviousWeek", 0)),
            "delta": int(labeled.get("Delta",
                                     labeled.get("CurrentWeek", 0)
                                     - labeled.get("PreviousWeek", 0))),
            "change_percent": float(labeled.get("ChangePercent", 0.0)),
        }

    # 3) Positional fallback — at least 4 numerics required
    nums = re.findall(r"-?\d+(?:\.\d+)?", text)
    if len(nums) >= 4:
        return {
            "current_count": int(float(nums[0])),
            "previous_count": int(float(nums[1])),
            "delta": int(float(nums[2])),
            "change_percent": float(nums[3]),
        }

    raise ParseError(f"could not extract WoW metrics from text: {text[:200]!r}")


def _first_row(parsed: Any) -> dict | None:
    """Pull the first dict-row out of a variety of JSON shapes."""
    if isinstance(parsed, dict):
        # Kusto-ish: {"tables": [{"rows": [...], "columns": [...]}]}
        tables = parsed.get("tables")
        if isinstance(tables, list) and tables:
            t = tables[0]
            if isinstance(t, dict):
                rows = t.get("rows")
                cols = t.get("columns")
                if isinstance(rows, list) and rows and isinstance(cols, list):
                    names = [c.get("name") if isinstance(c, dict) else c for c in cols]
                    return dict(zip(names, rows[0]))
        # Direct row dict
        return parsed
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        return parsed[0]
    return None


def _coerce_wow(row: dict) -> dict:
    def _pick(*names: str) -> Any:
        for n in names:
            if n in row:
                return row[n]
        return None

    cur = _pick("CurrentWeek", "current_count", "Current")
    prev = _pick("PreviousWeek", "previous_count", "Previous")
    delta = _pick("Delta", "delta")
    pct = _pick("ChangePercent", "change_percent", "Change")

    if cur is None or prev is None:
        raise ParseError("row missing CurrentWeek/PreviousWeek")

    cur_i = int(float(cur))
    prev_i = int(float(prev))
    delta_i = int(float(delta)) if delta is not None else cur_i - prev_i
    pct_f = float(pct) if pct is not None else (
        (cur_i - prev_i) / max(prev_i, 1) * 100.0
    )

    return {
        "current_count": cur_i,
        "previous_count": prev_i,
        "delta": delta_i,
        "change_percent": pct_f,
    }
