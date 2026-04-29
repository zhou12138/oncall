"""Mock data connector — simulates ADX/GitHub/Teams when MCP is unavailable.

Returns realistic mock data so the 3-step pipeline can run end-to-end.
Replace with real connectors (Kusto SDK, GitHub API, Teams webhook) later.
"""

import random
from datetime import datetime, timedelta, timezone


def mock_triage(signal_name: str) -> dict:
    """Simulate Step 1 triage: Global First vs Windows First."""
    now = datetime.now(timezone.utc)

    # Simulate platform breakdown
    platforms = ["Windows", "macOS", "Linux", "iOS", "Android", "Web"]
    platform_counts = {p: random.randint(0, 500) for p in platforms}
    total = sum(platform_counts.values())
    windows_count = platform_counts["Windows"]
    windows_pct = round(windows_count / max(total, 1) * 100, 1)

    # Determine verdict based on distribution
    if windows_pct > 60:
        verdict = "Windows First"
        global_first_seen = now - timedelta(hours=random.randint(1, 6))
        windows_first_seen = now - timedelta(hours=random.randint(7, 24))
    elif windows_pct < 30:
        verdict = "Global First"
        global_first_seen = now - timedelta(hours=random.randint(7, 24))
        windows_first_seen = now - timedelta(hours=random.randint(1, 6))
    else:
        verdict = "Global First"
        global_first_seen = now - timedelta(hours=random.randint(4, 12))
        windows_first_seen = now - timedelta(hours=random.randint(2, 10))

    return {
        "verdict": verdict,
        "details": {
            "source": "mock",
            "signal_name": signal_name,
            "total_count": total,
            "platform_breakdown": platform_counts,
            "windows_percentage": windows_pct,
            "global_first_seen": global_first_seen.isoformat(),
            "windows_first_seen": windows_first_seen.isoformat(),
            "affected_platforms": [p for p, c in platform_counts.items() if c > 50],
        },
        "signal_name": signal_name,
    }


def mock_wow(signal_name: str, repo: str = "") -> dict:
    """Simulate Step 2 week-over-week comparison."""
    previous_count = random.randint(100, 2000)

    # Simulate different scenarios
    scenario = random.choice(["spike", "gradual_up", "stable", "improving"])
    if scenario == "spike":
        current_count = int(previous_count * random.uniform(1.5, 3.0))
    elif scenario == "gradual_up":
        current_count = int(previous_count * random.uniform(1.1, 1.4))
    elif scenario == "stable":
        current_count = int(previous_count * random.uniform(0.95, 1.05))
    else:  # improving
        current_count = int(previous_count * random.uniform(0.5, 0.85))

    delta = current_count - previous_count
    change_pct = round(delta / max(previous_count, 1) * 100, 1)

    if change_pct > 5:
        trend = "up"
    elif change_pct < -5:
        trend = "down"
    else:
        trend = "flat"

    # Mock recent code changes
    recent_changes = []
    if repo:
        authors = ["alice", "bob", "charlie", "diana"]
        for i in range(random.randint(2, 5)):
            recent_changes.append({
                "author": random.choice(authors),
                "pr_title": random.choice([
                    f"Fix retry logic in {signal_name.split()[0].lower()} handler",
                    "Bump dependency versions",
                    "Refactor error handling middleware",
                    "Add circuit breaker for downstream calls",
                    "Update rate limiting config",
                    "Migrate to new auth provider",
                ]),
                "files_changed": random.randint(1, 20),
                "timestamp": (datetime.now(timezone.utc) - timedelta(days=random.randint(0, 7))).isoformat(),
            })

    return {
        "current_count": current_count,
        "previous_count": previous_count,
        "delta": delta,
        "change_percent": change_pct,
        "trend": trend,
        "recent_changes": recent_changes,
        "source": "mock",
        "scenario": scenario,
    }
