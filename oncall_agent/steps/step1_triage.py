"""Step 1: Triage — Global first appearance vs Windows first?

Queries ADX via MCP to determine if the issue is global-first or Windows-first.
"""

from oncall_agent.mcp_clients.client import MCPClient

# Kusto queries
GLOBAL_FIRST_QUERY = """
// Check if this signal appeared globally before Windows
let SignalName = '{signal_name}';
let LookbackDays = 14d;
let GlobalFirst = toscalar(
    SignalTable
    | where SignalName == SignalName
    | where Timestamp > ago(LookbackDays)
    | summarize FirstSeen = min(Timestamp)
);
let WindowsFirst = toscalar(
    SignalTable
    | where SignalName == SignalName
    | where Platform == 'Windows'
    | where Timestamp > ago(LookbackDays)
    | summarize FirstSeen = min(Timestamp)
);
print GlobalFirstAppearance = GlobalFirst, WindowsFirstAppearance = WindowsFirst,
      Verdict = iff(GlobalFirst < WindowsFirst, "Global First", "Windows First")
"""

SIGNAL_DETAILS_QUERY = """
let SignalName = '{signal_name}';
SignalTable
| where SignalName == SignalName
| where Timestamp > ago(14d)
| summarize Count = count(), Platforms = make_set(Platform), 
            FirstSeen = min(Timestamp), LastSeen = max(Timestamp)
    by SignalName
"""


async def step_triage(adx_client: MCPClient, signal_name: str) -> dict:
    """Run triage: query ADX to check global vs Windows first appearance.
    
    Returns:
        {
            "verdict": "Global First" | "Windows First",
            "details": { ... raw query results },
            "signal_name": str,
        }
    """
    # Query 1: Global vs Windows first
    query = GLOBAL_FIRST_QUERY.format(signal_name=signal_name)
    verdict_result = await adx_client.call_tool("execute_query", {"query": query})

    # Query 2: Signal overview
    details_query = SIGNAL_DETAILS_QUERY.format(signal_name=signal_name)
    details_result = await adx_client.call_tool("execute_query", {"query": details_query})

    # Parse verdict from result
    verdict = "Unknown"
    if isinstance(verdict_result, dict):
        content = verdict_result.get("content", [{}])
        if content and isinstance(content, list):
            text = content[0].get("text", "")
            if "Global First" in text:
                verdict = "Global First"
            elif "Windows First" in text:
                verdict = "Windows First"

    return {
        "verdict": verdict,
        "details": details_result,
        "signal_name": signal_name,
    }
