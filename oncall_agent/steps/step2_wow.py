"""Step 2: Week-over-Week (环比) comparison.

Queries ADX for current vs previous period metrics, computes delta.
"""

from oncall_agent.mcp_clients.client import MCPClient
from oncall_agent.utils.parsing import parse_mcp_text, parse_wow_metrics
from oncall_agent.utils.sanitize import sanitize_repo, sanitize_signal_name

WOW_QUERY = """
declare query_parameters(p_SignalName: string);
let CurrentStart = ago(7d);
let PreviousStart = ago(14d);
let PreviousEnd = ago(7d);
// Current week
let Current = toscalar(
    SignalTable
    | where SignalName == p_SignalName
    | where Timestamp between (CurrentStart .. now())
    | summarize Count = count()
);
// Previous week
let Previous = toscalar(
    SignalTable
    | where SignalName == p_SignalName
    | where Timestamp between (PreviousStart .. PreviousEnd)
    | summarize Count = count()
);
print CurrentWeek = Current, PreviousWeek = Previous,
      Delta = Current - Previous,
      ChangePercent = round(todouble(Current - Previous) / todouble(max_of(Previous, 1)) * 100, 2)
"""

GITHUB_RECENT_CHANGES_QUERY = """
declare query_parameters(p_SignalName: string, p_Repo: string);
GitHubMetrics
| where Timestamp > ago(14d)
| where Repository has p_Repo
| project Timestamp, Author, PRTitle, FilesChanged, Additions, Deletions
| order by Timestamp desc
| take 20
"""


async def step_wow_compare(
    adx_client: MCPClient,
    github_client: MCPClient,
    signal_name: str,
    repo: str = "",
) -> dict:
    """Run week-over-week comparison.
    
    Returns:
        {
            "current_count": int,
            "previous_count": int,
            "delta": int,
            "change_percent": float,
            "trend": "up" | "down" | "flat",
            "recent_changes": [...],  # github PRs if repo provided
        }
    """
    signal_name = sanitize_signal_name(signal_name)
    repo = sanitize_repo(repo)
    # ADX: WoW numbers (parameterized to prevent KQL injection)
    wow_result = await adx_client.call_tool("execute_query", {
        "query": WOW_QUERY,
        "parameters": {"p_SignalName": signal_name},
    })

    # Parse — raises ParseError on malformed responses (no silent defaults)
    text = parse_mcp_text(wow_result)
    metrics = parse_wow_metrics(text)
    current = metrics["current_count"]
    previous = metrics["previous_count"]
    delta = metrics["delta"]
    change_pct = metrics["change_percent"]

    trend = "flat"
    if change_pct > 5:
        trend = "up"
    elif change_pct < -5:
        trend = "down"

    # GitHub: recent PRs correlated
    recent_changes = []
    if repo:
        try:
            gh_result = await adx_client.call_tool("execute_query", {
                "query": GITHUB_RECENT_CHANGES_QUERY,
                "parameters": {"p_SignalName": signal_name, "p_Repo": repo},
            })
            recent_changes = gh_result.get("content", []) if isinstance(gh_result, dict) else []
        except Exception as e:
            # GitHub metrics correlation is optional — log and continue
            import logging
            logging.getLogger(__name__).warning(
                "github recent-changes correlation failed for repo=%s: %s", repo, e
            )
            recent_changes = []

    return {
        "current_count": current,
        "previous_count": previous,
        "delta": delta,
        "change_percent": change_pct,
        "trend": trend,
        "recent_changes": recent_changes,
    }
