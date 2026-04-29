"""Incident dataclass + state machine.

Allowed transitions form a small DAG with an "escalated" sink reachable from
any state. ``transition`` validates the move and appends a record to
``transitions`` so the audit log is replayable.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List


# State graph. Special key "*" means "from any state". transition() merges
# wildcards with concrete from-state edges.
VALID_TRANSITIONS: Dict[str, List[str]] = {
    "new": ["triaged"],
    "triaged": ["acknowledged"],
    "acknowledged": ["mitigated"],
    "mitigated": ["resolved"],
    "*": ["escalated"],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _allowed_from(status: str) -> List[str]:
    return list(VALID_TRANSITIONS.get(status, [])) + list(VALID_TRANSITIONS.get("*", []))


@dataclass
class Incident:
    run_id: str
    signal_name: str
    severity: str = "unknown"
    status: str = "new"
    owner: str = ""
    created_at: str = field(default_factory=_now_iso)
    transitions: List[Dict[str, Any]] = field(default_factory=list)

    def transition(self, new_status: str, by: str = "system") -> None:
        """Move to ``new_status`` if allowed; raise ValueError otherwise."""
        allowed = _allowed_from(self.status)
        if new_status not in allowed:
            raise ValueError(
                f"invalid transition: {self.status} -> {new_status} "
                f"(allowed: {allowed})"
            )
        record = {
            "from": self.status,
            "to": new_status,
            "by": by,
            "at": _now_iso(),
        }
        self.transitions.append(record)
        self.status = new_status

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "signal_name": self.signal_name,
            "severity": self.severity,
            "status": self.status,
            "owner": self.owner,
            "created_at": self.created_at,
            "transitions": list(self.transitions),
        }
