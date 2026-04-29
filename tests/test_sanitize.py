"""Tests for oncall_agent.utils.sanitize.

Verifies that valid identifiers pass through and that classic injection
payloads are rejected before they can reach KQL interpolation.
"""

import pytest

from oncall_agent.utils.sanitize import sanitize_signal_name, sanitize_repo


# ─── happy path ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "name",
    [
        "EdgeCrashRate",
        "HighCPU_WestUS2",
        "service.api.errors",
        "alert-2026-04",
        "Some Signal With Spaces",
        "abc123",
    ],
)
def test_sanitize_signal_name_accepts_valid(name):
    assert sanitize_signal_name(name) == name


@pytest.mark.parametrize(
    "repo",
    ["", "owner/repo", "Microsoft/Edge", "a.b/c-d_e", "user/repo.subdir"],
)
def test_sanitize_repo_accepts_valid(repo):
    assert sanitize_repo(repo) == repo


# ─── injection rejection ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "payload",
    [
        "'; DROP TABLE Users--",
        '" OR 1=1',
        "abc\x00null",
        "name; rm -rf /",
        "`backtick`",
        "name | take 1",
        "a$(b)",
    ],
)
def test_sanitize_signal_name_rejects_injection(payload):
    with pytest.raises(ValueError):
        sanitize_signal_name(payload)


@pytest.mark.parametrize("bad", ["", None])
def test_sanitize_signal_name_rejects_empty(bad):
    with pytest.raises(ValueError):
        sanitize_signal_name(bad)


@pytest.mark.parametrize(
    "bad_repo",
    ["owner/repo;rm", "owner repo", "owner/$repo", "owner\nrepo"],
)
def test_sanitize_repo_rejects_injection(bad_repo):
    with pytest.raises(ValueError):
        sanitize_repo(bad_repo)
