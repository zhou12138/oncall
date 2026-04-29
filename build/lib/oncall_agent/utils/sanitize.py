"""Input sanitization for KQL query parameters.

Defends against KQL injection by validating signal/repo identifiers against
strict allowlist regexes before they are interpolated into queries.
"""

import re

_SIGNAL_NAME_RE = re.compile(r"^[a-zA-Z0-9_.\-\s]+$")
_REPO_RE = re.compile(r"^[a-zA-Z0-9_.\-/]+$")


def sanitize_signal_name(name: str) -> str:
    """Validate a signal name. Returns the name unchanged if valid.

    Raises ValueError if the name contains characters outside
    [a-zA-Z0-9_.\\-\\s].
    """
    if not isinstance(name, str) or not name:
        raise ValueError("signal_name must be a non-empty string")
    if not _SIGNAL_NAME_RE.match(name):
        raise ValueError(f"invalid signal_name: {name!r}")
    return name


def sanitize_repo(repo: str) -> str:
    """Validate a repository identifier (e.g. ``owner/repo``).

    Raises ValueError if the value contains characters outside
    [a-zA-Z0-9_.\\-/]. An empty string is accepted (treated as "no repo").
    """
    if repo == "":
        return repo
    if not isinstance(repo, str):
        raise ValueError("repo must be a string")
    if not _REPO_RE.match(repo):
        raise ValueError(f"invalid repo: {repo!r}")
    return repo
