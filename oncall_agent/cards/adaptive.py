"""Teams Adaptive Card builder for OnCall analyses.

Produces a v1.4 Adaptive Card (https://adaptivecards.io/) JSON dict containing:
- severity badge (color tied to severity)
- signal name + LLM summary
- triage verdict + WoW trend
- Acknowledge / Escalate action buttons that hit the local action API.
"""

from typing import Any, Dict

# Severity → Adaptive Card text color keyword.
_SEVERITY_COLOR = {
    "critical": "attention",
    "high": "warning",
    "medium": "default",
    "low": "good",
    "info": "accent",
}


def _color_for(severity: str) -> str:
    return _SEVERITY_COLOR.get((severity or "").lower(), "default")


def build_adaptive_card(analysis: dict, run_id: str = "") -> Dict[str, Any]:
    """Build a v1.4 AdaptiveCard payload from a step3 analysis dict.

    ``analysis`` is the merged result the orchestrator gathers — it should
    contain ``signal_name``, ``severity``, ``summary``, plus nested
    ``steps.triage.verdict`` and ``steps.wow.trend`` / ``change_percent``.
    Falls back gracefully when fields are missing.
    """
    severity = (analysis.get("severity") or "unknown").lower()
    signal_name = analysis.get("signal_name") or analysis.get("signal") or "(unknown signal)"
    summary = analysis.get("summary") or ""

    steps = analysis.get("steps") or {}
    triage = steps.get("triage") or {}
    wow = steps.get("wow") or {}
    verdict = triage.get("verdict") or "n/a"
    trend = wow.get("trend") or "n/a"
    change_pct = wow.get("change_percent", 0)

    base_url = f"/actions/{run_id}" if run_id else "/actions"

    body = [
        {
            "type": "TextBlock",
            "text": f"Severity: {severity.upper()}",
            "weight": "Bolder",
            "size": "Medium",
            "color": _color_for(severity),
        },
        {
            "type": "TextBlock",
            "text": f"Signal: {signal_name}",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": summary or "(no summary available)",
            "wrap": True,
            "spacing": "Small",
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Triage verdict", "value": str(verdict)},
                {"title": "WoW trend", "value": f"{trend} ({change_pct}%)"},
                {"title": "Run ID", "value": run_id or "(unset)"},
            ],
        },
    ]

    actions = [
        {
            "type": "Action.OpenUrl",
            "title": "Acknowledge",
            "url": f"{base_url}/ack",
        },
        {
            "type": "Action.OpenUrl",
            "title": "Escalate",
            "url": f"{base_url}/escalate",
        },
    ]

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
        "actions": actions,
    }
