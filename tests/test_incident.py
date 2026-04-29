"""Tests for oncall_agent.models.incident state machine."""

import pytest

from oncall_agent.models.incident import VALID_TRANSITIONS, Incident


def make(status: str = "new") -> Incident:
    inc = Incident(run_id="r1", signal_name="EdgeCrash", severity="high")
    if status != "new":
        # Walk the state machine to reach the requested start state.
        path = {
            "triaged": ["triaged"],
            "acknowledged": ["triaged", "acknowledged"],
            "mitigated": ["triaged", "acknowledged", "mitigated"],
            "resolved": ["triaged", "acknowledged", "mitigated", "resolved"],
            "escalated": ["escalated"],
        }[status]
        for s in path:
            inc.transition(s, by="test")
    return inc


class TestValidTransitions:
    def test_table_has_expected_keys(self):
        for k in ("new", "triaged", "acknowledged", "mitigated", "*"):
            assert k in VALID_TRANSITIONS

    def test_happy_path(self):
        inc = Incident(run_id="r", signal_name="s")
        inc.transition("triaged")
        inc.transition("acknowledged")
        inc.transition("mitigated")
        inc.transition("resolved")
        assert inc.status == "resolved"
        assert len(inc.transitions) == 4

    def test_escalate_from_anywhere(self):
        for start in ["new", "triaged", "acknowledged", "mitigated"]:
            inc = make(start)
            inc.transition("escalated", by="user")
            assert inc.status == "escalated"

    def test_records_audit_log(self):
        inc = Incident(run_id="r", signal_name="s")
        inc.transition("triaged", by="orch")
        rec = inc.transitions[0]
        assert rec["from"] == "new"
        assert rec["to"] == "triaged"
        assert rec["by"] == "orch"
        assert "at" in rec


class TestInvalidTransitions:
    def test_skip_state(self):
        inc = Incident(run_id="r", signal_name="s")
        with pytest.raises(ValueError):
            inc.transition("acknowledged")

    def test_backwards(self):
        inc = make("acknowledged")
        with pytest.raises(ValueError):
            inc.transition("triaged")

    def test_self_loop(self):
        inc = make("triaged")
        with pytest.raises(ValueError):
            inc.transition("triaged")

    def test_unknown_state(self):
        inc = Incident(run_id="r", signal_name="s")
        with pytest.raises(ValueError):
            inc.transition("nonexistent")


class TestSerialization:
    def test_to_dict_roundtrip(self):
        inc = Incident(run_id="r1", signal_name="EdgeCrash", severity="high",
                       owner="edge-team")
        inc.transition("triaged")
        d = inc.to_dict()
        assert d["run_id"] == "r1"
        assert d["status"] == "triaged"
        assert d["owner"] == "edge-team"
        assert isinstance(d["transitions"], list)
        assert len(d["transitions"]) == 1
