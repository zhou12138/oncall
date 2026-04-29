"""Tests for oncall_agent.memory.store recall + tokenization."""

import os
import tempfile

import pytest

from oncall_agent.memory.store import OncallMemory


@pytest.fixture
def memory():
    d = tempfile.mkdtemp()
    return OncallMemory(os.path.join(d, "mem.json"))


class TestTokenize:
    def test_camel_case(self):
        assert OncallMemory._tokenize("EdgeCrashRate") == {"edge", "crash", "rate"}

    def test_underscore_lower(self):
        assert OncallMemory._tokenize("high_cpu_westus2") == {"high", "cpu", "westus2"}

    def test_mixed(self):
        toks = OncallMemory._tokenize("HighCPU_WestUS2")
        # Tokenizer splits camel boundaries, so WestUS2 → west, us, 2
        assert "high" in toks
        assert "cpu" in toks
        assert "west" in toks
        assert "us" in toks

    def test_empty(self):
        assert OncallMemory._tokenize("") == set()


class TestRecall:
    def test_empty_memory_returns_empty(self, memory):
        assert memory.recall("EdgeCrashRate") == []

    def test_unrelated_returns_empty(self, memory):
        memory.add("incidents", {"signal": "NetworkLatency", "summary": "lat"})
        assert memory.recall("AuthFailure") == []

    def test_orders_by_similarity(self, memory):
        memory.add("incidents", {"signal": "EdgeCrashRate", "summary": "a"})
        memory.add("incidents", {"signal": "EdgeError", "summary": "b"})
        memory.add("incidents", {"signal": "NetworkLatency", "summary": "c"})

        results = memory.recall("EdgeCrash", top_k=3)
        # EdgeCrashRate (edge,crash,rate) ∩ (edge,crash) = 2/3 ≈ 0.667
        # EdgeError      (edge,error)      ∩ (edge,crash) = 1/3 ≈ 0.333
        # NetworkLatency: 0 (filtered)
        assert len(results) == 2
        assert results[0]["signal"] == "EdgeCrashRate"
        assert results[0]["_similarity"] > results[1]["_similarity"]
        assert results[1]["signal"] == "EdgeError"

    def test_top_k_limits(self, memory):
        for i in range(5):
            memory.add("incidents", {"signal": f"EdgeFail{i}", "summary": "x"})
        results = memory.recall("EdgeFail", top_k=2)
        assert len(results) == 2

    def test_falls_back_when_query_empty(self, memory):
        memory.add("incidents", {"signal": "EdgeCrash", "summary": "x"})
        assert memory.recall("") == []
