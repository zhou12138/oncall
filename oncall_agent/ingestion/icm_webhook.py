"""ICM / Geneva webhook ingestion.

Provides:
- ``parse_icm_payload`` — normalize an ICM JSON envelope into the
  pipeline's standard intent shape.
- ``verify_icm_signature`` — HMAC-SHA256 verification of the raw request
  body against the configured shared secret.

ICM payloads vary by tenant. We accept several common field names
(``IncidentId`` vs ``id``, ``Severity`` vs ``severity``, …) and fall back
to safe defaults so a malformed payload still drives a pipeline run that
the LLM can reason about.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict


def _first(d: dict, *names: str, default: Any = "") -> Any:
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return default


_SEVERITY_MAP = {
    "0": "critical", "1": "critical",
    "2": "high",
    "3": "medium",
    "4": "low",
    "sev0": "critical", "sev1": "critical",
    "sev2": "high",
    "sev3": "medium",
    "sev4": "low",
}


def _normalize_severity(raw: Any) -> str:
    if raw is None:
        return "unknown"
    s = str(raw).strip().lower()
    if not s:
        return "unknown"
    if s in _SEVERITY_MAP:
        return _SEVERITY_MAP[s]
    if s in {"critical", "high", "medium", "low", "info"}:
        return s
    return "unknown"


def parse_icm_payload(payload: dict) -> Dict[str, Any]:
    """Normalize an ICM webhook envelope to a standard intent dict.

    Returns a dict with: ``incident_id``, ``title``, ``description``,
    ``severity``, ``owning_team``, ``impacted_services`` (list[str]),
    ``signal_name`` (derived from title), ``raw`` (original payload).
    """
    if not isinstance(payload, dict):
        raise ValueError("ICM payload must be a JSON object")

    incident_id = str(_first(payload, "IncidentId", "incidentId", "id", default=""))
    title = str(_first(payload, "Title", "title", "Summary", "summary", default=""))
    description = str(
        _first(payload, "Description", "description", "Details", "details", default="")
    )
    severity = _normalize_severity(
        _first(payload, "Severity", "severity", default=None)
    )
    owning_team = str(
        _first(payload, "OwningTeamId", "owningTeam", "owning_team", "Team", default="")
    )

    services_raw = _first(
        payload, "ImpactedServices", "impactedServices", "impacted_services",
        "Services", "services", default=[],
    )
    if isinstance(services_raw, str):
        impacted_services = [s.strip() for s in services_raw.split(",") if s.strip()]
    elif isinstance(services_raw, list):
        impacted_services = [str(s) for s in services_raw if s]
    else:
        impacted_services = []

    # signal_name: prefer explicit, otherwise derive from title.
    signal_name = str(
        _first(payload, "SignalName", "signal_name", default="")
    )
    if not signal_name:
        # Strip non-identifier punctuation, keep alnum + _ . - space.
        signal_name = "".join(
            c for c in title if c.isalnum() or c in ("_", ".", "-", " ")
        ).strip()
        if not signal_name:
            signal_name = f"ICMIncident_{incident_id}" if incident_id else "ICMIncident"

    return {
        "incident_id": incident_id,
        "title": title,
        "description": description,
        "severity": severity,
        "owning_team": owning_team,
        "impacted_services": impacted_services,
        "signal_name": signal_name,
        "raw": payload,
    }


def verify_hmac(payload_bytes: bytes, signature: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 verification.

    ``signature`` may be in the form ``sha256=<hex>`` (GitHub-style) or a
    bare hex digest. Returns False on any mismatch or missing inputs.
    """
    if not secret or not signature or payload_bytes is None:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), payload_bytes, hashlib.sha256
    ).hexdigest()
    sig = signature.strip()
    if "=" in sig:
        sig = sig.split("=", 1)[1].strip()
    return hmac.compare_digest(expected, sig)


# Back-compat alias.
verify_icm_signature = verify_hmac
