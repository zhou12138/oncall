"""Error hierarchy for oncall_agent.

All recoverable / mappable errors derive from :class:`OncallError` so the
HTTP layer can map them to specific status codes without catching bare
``Exception``.

Hierarchy::

    OncallError
        ├── TriageError      (step 1 / ADX triage failure)
        ├── WoWError         (step 2 / week-over-week failure)
        ├── ReasoningError   (step 3 / LLM reasoning failure)
        └── MCPError         (transport-level MCP failure)
"""


class OncallError(Exception):
    """Base class for all oncall_agent domain errors."""


class TriageError(OncallError):
    """Step 1 (triage) failed — usually an ADX or parsing failure."""


class WoWError(OncallError):
    """Step 2 (week-over-week) failed."""


class ReasoningError(OncallError):
    """Step 3 (LLM reasoning / action) failed."""


class MCPError(OncallError):
    """Transport-level failure talking to an MCP server."""
