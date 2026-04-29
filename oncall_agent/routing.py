"""Owner routing — keyword-based mapping from signal name to oncall team.

Used by the orchestrator after a run completes to assign an Incident's
``owner``. First substring match (case-insensitive) wins; otherwise the
default oncall is used.
"""

from typing import Dict

# Order matters: more specific keywords should appear first if duplicates.
DEFAULT_ROUTES: Dict[str, str] = {
    "CPU": "infra-team",
    "Memory": "infra-team",
    "Disk": "infra-team",
    "Network": "infra-team",
    "Latency": "infra-team",
    "Crash": "client-team",
    "Edge": "client-team",
    "Auth": "identity-team",
    "Login": "identity-team",
    "Build": "devx-team",
    "CI": "devx-team",
}

DEFAULT_OWNER = "oncall-primary"


def route_incident(signal_name: str) -> str:
    """Return the owner team for a signal based on keyword match."""
    if not signal_name:
        return DEFAULT_OWNER
    needle = signal_name.lower()
    for keyword, owner in DEFAULT_ROUTES.items():
        if keyword.lower() in needle:
            return owner
    return DEFAULT_OWNER
