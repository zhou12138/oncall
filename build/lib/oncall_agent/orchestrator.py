"""Orchestrator — runs the 3-step oncall pipeline.

Supports two modes:
  1. Full MCP: connectors available → query ADX, GitHub, Teams
  2. Mock + LLM: No MCP → mock data for triage/WoW, LLM for reasoning
"""

from oncall_agent.memory.store import OncallMemory
from oncall_agent.config import config
from oncall_agent.copilot_proxy import get_proxy
from oncall_agent.errors import (
    MCPError,
    OncallError,
    ReasoningError,
    TriageError,
    WoWError,
)


class OncallOrchestrator:
    """Orchestrates the 3-step oncall analysis pipeline."""

    def __init__(self):
        self.memory = OncallMemory(config.memory_path)
        self._mcp_available = bool(
            config.adx_mcp.url or config.github_mcp.url or config.teams_mcp.url
        )

    async def run(
        self,
        signal_name: str,
        repo: str = "",
        teams_channel: str = "",
        model: str = None,
        intent: dict = None,
        raw_query: str = "",
    ) -> dict:
        """Execute the pipeline. Uses mock data if MCP unavailable."""
        if self._mcp_available:
            return await self._run_with_mcp(
                signal_name, repo, teams_channel, model, intent, raw_query
            )
        else:
            return await self._run_with_mock(
                signal_name, repo, teams_channel, model, intent, raw_query
            )

    async def _run_with_mock(
        self,
        signal_name: str,
        repo: str = "",
        teams_channel: str = "",
        model: str = None,
        intent: dict = None,
        raw_query: str = "",
    ) -> dict:
        """3-step pipeline with mock data + LLM reasoning."""
        from oncall_agent.connectors.mock import mock_triage, mock_wow
        from oncall_agent.steps.step3_reason import step_reason_and_act

        result = {"signal_name": signal_name, "steps": {}, "mode": "mock"}
        intent = intent or {}

        extra_context = ""
        if raw_query:
            extra_context += f"\n\n## Original Incident Metadata\n{raw_query}"

        # Step 1: Triage (mock)
        print(f"[Step 1] Triage: {signal_name}")
        triage = mock_triage(signal_name)
        result["steps"]["triage"] = triage
        details = triage["details"]
        print(f"  → Verdict: {triage['verdict']}")
        print(f"  → Platforms: {', '.join(f'{k}={v}' for k, v in details.get('platform_breakdown', {}).items() if v > 50)}")
        print(f"  → Windows: {details.get('windows_percentage', 0)}%")

        # Step 2: WoW (mock)
        print(f"[Step 2] WoW comparison")
        wow = mock_wow(signal_name, repo)
        result["steps"]["wow"] = wow
        print(f"  → This week: {wow['current_count']}, Last week: {wow['previous_count']}")
        print(f"  → Trend: {wow['trend']} ({wow['change_percent']}%)")

        # Step 3: LLM reasoning
        print(f"[Step 3] LLM reasoning & action")
        try:
            analysis = await step_reason_and_act(
                None,  # no teams client
                self.memory,
                triage, wow,
                "",  # no teams channel in mock mode
                model=model,
                extra_context=extra_context,
            )
        except (OncallError, ValueError):
            raise
        except Exception as e:
            raise ReasoningError(f"step_reason_and_act failed: {e}") from e
        result["steps"]["analysis"] = analysis
        print(f"  → Severity: {analysis['severity']}")

        result["severity"] = analysis["severity"]
        result["summary"] = analysis["summary"]
        result["actions"] = analysis["actions"]

        # Save to memory
        self.memory.record(signal_name, result)

        return result

    async def _run_with_mcp(
        self,
        signal_name: str,
        repo: str = "",
        teams_channel: str = "",
        model: str = None,
        intent: dict = None,
        raw_query: str = "",
    ) -> dict:
        """Full pipeline with MCP connectors."""
        from oncall_agent.mcp_clients.client import MCPClient
        from oncall_agent.steps.step1_triage import step_triage
        from oncall_agent.steps.step2_wow import step_wow_compare
        from oncall_agent.steps.step3_reason import step_reason_and_act

        adx_client = MCPClient("adx-kusto", config.adx_mcp.url)
        github_client = MCPClient("github", config.github_mcp.url)
        teams_client = MCPClient("teams", config.teams_mcp.url)

        result = {"signal_name": signal_name, "steps": {}, "mode": "mcp"}
        intent = intent or {}

        extra_context = ""
        if raw_query:
            extra_context += f"\n\n## Original Incident Metadata\n{raw_query}"
        if intent.get("key_entities"):
            extra_context += f"\n\n## Key Entities\n{', '.join(intent['key_entities'])}"
        if intent.get("kusto_hints"):
            extra_context += f"\n\n## Kusto Hints\n{', '.join(intent['kusto_hints'])}"

        # Step 1
        if intent.get("should_run_triage", True):
            print(f"[Step 1] Triage: {signal_name}")
            try:
                triage = await step_triage(adx_client, signal_name)
            except (OncallError, ValueError):
                raise
            except Exception as e:
                raise TriageError(f"step_triage failed: {e}") from e
            result["steps"]["triage"] = triage
            print(f"  → Verdict: {triage['verdict']}")
        else:
            result["steps"]["triage"] = {"verdict": "Skipped", "details": {}, "signal_name": signal_name}

        # Step 2
        if intent.get("should_run_wow", True):
            print(f"[Step 2] WoW comparison")
            try:
                wow = await step_wow_compare(adx_client, github_client, signal_name, repo)
            except (OncallError, ValueError):
                raise
            except Exception as e:
                raise WoWError(f"step_wow_compare failed: {e}") from e
            result["steps"]["wow"] = wow
            print(f"  → Trend: {wow['trend']} ({wow['change_percent']}%)")
        else:
            result["steps"]["wow"] = {
                "current_count": 0, "previous_count": 0, "delta": 0,
                "change_percent": 0, "trend": "skipped", "recent_changes": [],
            }

        # Step 3
        print(f"[Step 3] Reasoning & action")
        should_notify = intent.get("should_notify", bool(teams_channel))
        try:
            analysis = await step_reason_and_act(
                teams_client, self.memory,
                result["steps"]["triage"], result["steps"]["wow"],
                teams_channel if should_notify else "",
                model=model, extra_context=extra_context,
            )
        except (OncallError, ValueError):
            raise
        except Exception as e:
            raise ReasoningError(f"step_reason_and_act failed: {e}") from e
        result["steps"]["analysis"] = analysis
        print(f"  → Severity: {analysis['severity']}")
        print(f"  → Teams sent: {analysis['teams_sent']}")

        result["severity"] = analysis["severity"]
        result["summary"] = analysis["summary"]
        result["actions"] = analysis["actions"]

        return result
