"""Step 2: Week-over-Week (环比) comparison.

Queries ADX for current vs previous period metrics, computes delta.
"""

from oncall_agent.mcp_clients.client import MCPClient
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

    # Parse
    current = 0
    previous = 0
    delta = 0
    change_pct = 0.0

    if isinstance(wow_result, dict):
        content = wow_result.get("content", [{}])
        if content and isinstance(content, list):
            text = content[0].get("text", "")
            # Simple parse — real impl would parse structured output
            import re
            nums = re.findall(r'[\d.]+', text)
            if len(nums) >= 4:
                current = int(float(nums[0]))
                previous = int(float(nums[1]))
                delta = int(float(nums[2]))
                change_pct = float(nums[3])

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
            recent_changes = gh_result.get("content", [])
        except Exception:
            pass  # GitHub metrics optional

    return {
        "current_count": current,
        "previous_count": previous,
        "delta": delta,
        "change_percent": change_pct,
        "trend": trend,
        "recent_changes": recent_changes,
    }
