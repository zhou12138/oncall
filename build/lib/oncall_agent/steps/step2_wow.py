"""Step 2: Week-over-Week (环比) comparison.

Queries ADX for current vs previous period metrics, computes delta.
"""

from oncall_agent.mcp_clients.client import MCPClient
from oncall_agent.utils.parsing import parse_mcp_text, parse_wow_metrics
from oncall_agent.utils.sanitize import sanitize_repo, sanitize_signal_name

WOW_QUERY = """
let SignalName = '{signal_name}';
let CurrentStart = ago(7d);
let PreviousStart = ago(14d);
let PreviousEnd = ago(7d);
// Current week
let Current = toscalar(
    SignalTable
    | where SignalName == SignalName
    | where Timestamp between (CurrentStart .. now())
    | summarize Count = count()
);
// Previous week
let Previous = toscalar(
    SignalTable
    | where SignalName == SignalName
    | where Timestamp between (PreviousStart .. PreviousEnd)
    | summarize Count = count()
);
print CurrentWeek = Current, PreviousWeek = Previous,
      Delta = Current - Previous,
      ChangePercent = round(todouble(Current - Previous) / todouble(max_of(Previous, 1)) * 100, 2)
"""

GITHUB_RECENT_CHANGES_QUERY = """
// Correlate with recent code changes from GitHub metrics
let SignalName = '{signal_name}';
GitHubMetrics
| where Timestamp > ago(14d)
| where Repository has '{repo}'
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
    # ADX: WoW numbers
    query = WOW_QUERY.format(signal_name=signal_name)
    wow_result = await adx_client.call_tool("execute_query", {"query": query})

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
            gh_query = GITHUB_RECENT_CHANGES_QUERY.format(signal_name=signal_name, repo=repo)
            gh_result = await adx_client.call_tool("execute_query", {"query": gh_query})
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
