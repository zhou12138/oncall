"""DataProvider Protocol + concrete MCP/Mock implementations.

The orchestrator drives a single pipeline regardless of where the data
comes from. Providers expose three coroutines:

- ``triage(signal_name)`` — Step 1 result dict
- ``wow_compare(signal_name, repo)`` — Step 2 result dict
- ``reason_and_act(triage, wow, teams_channel, **kwargs)`` — Step 3 result

A provider also reports its ``mode`` ("mcp"/"mock") for logging and may
optionally provide ``enrich(signal_name)`` returning extra context to be
appended to the LLM prompt.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from oncall_agent.config import config
from oncall_agent.memory.store import OncallMemory


@runtime_checkable
class DataProvider(Protocol):
    """Protocol every pipeline data provider must satisfy."""

    mode: str

    async def triage(self, signal_name: str) -> dict: ...

    async def wow_compare(self, signal_name: str, repo: str) -> dict: ...

    async def reason_and_act(
        self,
        triage_result: dict,
        wow_result: dict,
        teams_channel: str = "",
        *,
        memory: OncallMemory,
        model: str | None = None,
        extra_context: str = "",
        run_id: str = "",
    ) -> dict: ...

    async def enrich(self, signal_name: str) -> str: ...


class MockProvider:
    """Mock data + LLM reasoning. Used when no MCP servers are configured."""

    mode = "mock"

    async def triage(self, signal_name: str) -> dict:
        from oncall_agent.connectors.mock import mock_triage
        return mock_triage(signal_name)

    async def wow_compare(self, signal_name: str, repo: str) -> dict:
        from oncall_agent.connectors.mock import mock_wow
        return mock_wow(signal_name, repo)

    async def reason_and_act(
        self,
        triage_result: dict,
        wow_result: dict,
        teams_channel: str = "",
        *,
        memory: OncallMemory,
        model: str | None = None,
        extra_context: str = "",
        run_id: str = "",
    ) -> dict:
        from oncall_agent.steps.step3_reason import step_reason_and_act
        return await step_reason_and_act(
            None, memory, triage_result, wow_result, teams_channel,
            model=model, extra_context=extra_context, run_id=run_id,
        )

    async def enrich(self, signal_name: str) -> str:
        return ""


class MCPProvider:
    """Real MCP-backed provider using the configured ADX/GitHub/Teams servers."""

    mode = "mcp"

    def __init__(self) -> None:
        from oncall_agent.mcp_clients.client import MCPClient
        self.adx = MCPClient("adx-kusto", config.adx_mcp.url)
        self.github = MCPClient("github", config.github_mcp.url)
        self.teams = MCPClient("teams", config.teams_mcp.url)

    async def triage(self, signal_name: str) -> dict:
        from oncall_agent.steps.step1_triage import step_triage
        return await step_triage(self.adx, signal_name)

    async def wow_compare(self, signal_name: str, repo: str) -> dict:
        from oncall_agent.steps.step2_wow import step_wow_compare
        return await step_wow_compare(self.adx, self.github, signal_name, repo)

    async def reason_and_act(
        self,
        triage_result: dict,
        wow_result: dict,
        teams_channel: str = "",
        *,
        memory: OncallMemory,
        model: str | None = None,
        extra_context: str = "",
        run_id: str = "",
    ) -> dict:
        from oncall_agent.steps.step3_reason import step_reason_and_act
        return await step_reason_and_act(
            self.teams, memory, triage_result, wow_result, teams_channel,
            model=model, extra_context=extra_context, run_id=run_id,
        )

    async def enrich(self, signal_name: str) -> str:
        from oncall_agent.ingestion.log_enricher import enrich_with_logs
        return await enrich_with_logs(self.adx, signal_name)


def select_provider() -> DataProvider:
    """Pick MCPProvider when any MCP URL is configured, otherwise MockProvider."""
    if config.adx_mcp.url or config.github_mcp.url or config.teams_mcp.url:
        return MCPProvider()
    return MockProvider()
