"""Tests for oncall_agent.utils.parsing."""

import json

import pytest

from oncall_agent.utils.parsing import ParseError, parse_mcp_text, parse_wow_metrics


class TestParseMcpText:
    def test_extracts_text_block(self):
        result = {"content": [{"type": "text", "text": "hello world"}]}
        assert parse_mcp_text(result) == "hello world"

    def test_concatenates_multiple_blocks(self):
        result = {"content": [
            {"type": "text", "text": "line1"},
            {"type": "text", "text": "line2"},
        ]}
        assert parse_mcp_text(result) == "line1\nline2"

    def test_non_dict_raises(self):
        with pytest.raises(ParseError):
            parse_mcp_text("not a dict")

    def test_missing_content_raises(self):
        with pytest.raises(ParseError):
            parse_mcp_text({"foo": "bar"})

    def test_content_not_list_raises(self):
        with pytest.raises(ParseError):
            parse_mcp_text({"content": "not a list"})

    def test_no_text_blocks_raises(self):
        with pytest.raises(ParseError):
            parse_mcp_text({"content": [{"type": "image", "url": "x"}]})


class TestParseWowMetrics:
    def test_kusto_table_shape(self):
        text = json.dumps({
            "tables": [{
                "columns": [
                    {"name": "CurrentWeek"},
                    {"name": "PreviousWeek"},
                    {"name": "Delta"},
                    {"name": "ChangePercent"},
                ],
                "rows": [[150, 100, 50, 50.0]],
            }]
        })
        m = parse_wow_metrics(text)
        assert m["current_count"] == 150
        assert m["previous_count"] == 100
        assert m["delta"] == 50
        assert m["change_percent"] == 50.0

    def test_labeled_keys(self):
        text = "CurrentWeek = 200\nPreviousWeek = 100\nDelta = 100\nChangePercent = 100.0"
        m = parse_wow_metrics(text)
        assert m["current_count"] == 200
        assert m["change_percent"] == 100.0

    def test_positional_fallback(self):
        text = "300 200 100 50.0"
        m = parse_wow_metrics(text)
        assert m["current_count"] == 300
        assert m["delta"] == 100

    def test_empty_raises(self):
        with pytest.raises(ParseError):
            parse_wow_metrics("")

    def test_garbage_raises(self):
        with pytest.raises(ParseError):
            parse_wow_metrics("totally unparseable text without numbers")
