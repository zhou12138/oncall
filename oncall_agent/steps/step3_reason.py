"""Step 3: Reasoning + Summarize + Action.

Uses LLM to reason over triage + WoW data + memory context,
then optionally sends a summary to Teams via MCP.
"""

import json
from oncall_agent.memory.store import OncallMemory
from oncall_agent.config import config
from oncall_agent.copilot_proxy import get_proxy
from oncall_agent.logging_config import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are an OnCall analysis agent for a software engineering team.

Given incident triage data and week-over-week metrics, you must:
1. **Reason**: Analyze root cause hypotheses based on the data
2. **Summarize**: Produce a concise actionable summary
3. **Recommend**: Suggest specific next actions (investigate, escalate, monitor, close)

Consider the oncall memory context for patterns and prior incidents.

Output JSON:
{
  "reasoning": "your chain of thought",
  "summary": "2-3 sentence executive summary",  
  "severity": "critical|high|medium|low|info",
  "actions": ["action1", "action2"],
  "should_escalate": true/false,
  "pattern_detected": "description or null"
}
"""

USER_PROMPT_TEMPLATE = """## Triage Result (Step 1)
Signal: {signal_name}
Verdict: {verdict}
Details: {triage_details}

## Week-over-Week (Step 2)
Current week: {current_count}
Previous week: {previous_count}
Delta: {delta} ({change_percent}%)
Trend: {trend}
Recent code changes: {recent_changes}

## Memory Context
{memory_context}

Analyze this oncall signal and provide your assessment.
"""


async def step_reason_and_act(
    teams_client,  # MCPClient or None
    memory: OncallMemory,
    triage_result: dict,
    wow_result: dict,
    teams_channel: str = "",
    model: str = None,
    extra_context: str = "",
) -> dict:
    """LLM reasoning → summary → optional Teams notification."""
    # Build prompt
    memory_context = memory.get_context_for_llm()
    user_prompt = USER_PROMPT_TEMPLATE.format(
        signal_name=triage_result.get("signal_name", ""),
        verdict=triage_result.get("verdict", ""),
        triage_details=str(triage_result.get("details", "")),
        current_count=wow_result.get("current_count", 0),
        previous_count=wow_result.get("previous_count", 0),
        delta=wow_result.get("delta", 0),
        change_percent=wow_result.get("change_percent", 0),
        trend=wow_result.get("trend", ""),
        recent_changes=str(wow_result.get("recent_changes", [])),
        memory_context=memory_context,
    )

    if extra_context:
        user_prompt += f"\n{extra_context}"

    # Call LLM via copilot proxy
    proxy = get_proxy()
    if not await proxy.ensure_token():
        raise RuntimeError("Not authenticated. Run `oncall login` first.")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    use_model = model or config.llm_model

    llm_response = ""
    async for chunk in proxy.chat_completion_stream(messages, model=use_model):
        llm_response += chunk

    # Parse LLM output
    try:
        # Try to extract JSON from response (may have markdown wrapping)
        text = llm_response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        analysis = json.loads(text)
    except json.JSONDecodeError:
        analysis = {
            "reasoning": llm_response,
            "summary": llm_response[:200],
            "severity": "medium",
            "actions": ["Review manually"],
            "should_escalate": False,
            "pattern_detected": None,
        }

    # Save to memory
    memory.add("incidents", {
        "title": triage_result.get("signal_name", ""),
        "summary": analysis.get("summary", ""),
        "severity": analysis.get("severity", ""),
        "verdict": triage_result.get("verdict", ""),
        "wow_trend": wow_result.get("trend", ""),
    })

    if analysis.get("pattern_detected"):
        memory.add("patterns", {
            "pattern": analysis["pattern_detected"],
            "signal": triage_result.get("signal_name", ""),
        })

    memory.add("wow_comparisons", {
        "signal": triage_result.get("signal_name", ""),
        "summary": f"{wow_result.get('trend', '')} {wow_result.get('change_percent', 0)}%",
    })

    # Send to Teams (only if client and channel provided)
    teams_sent = False
    if teams_client and teams_channel:
        try:
            severity_emoji = {
                "critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢", "info": "ℹ️"
            }.get(analysis.get("severity", ""), "❓")

            teams_msg = (
                f"{severity_emoji} **OnCall Alert: {triage_result.get('signal_name', '')}**\n\n"
                f"**Verdict:** {triage_result.get('verdict', '')}\n"
                f"**Trend:** {wow_result.get('trend', '')} ({wow_result.get('change_percent', 0)}% WoW)\n\n"
                f"**Summary:** {analysis.get('summary', '')}\n\n"
                f"**Actions:**\n" + "\n".join(f"- {a}" for a in analysis.get("actions", []))
            )

            await teams_client.call_tool("send_message", {
                "channel": teams_channel,
                "message": teams_msg,
            })
            teams_sent = True
        except Exception as e:
            logger.warning(
                "teams.notification_failed",
                extra={
                    "event": "teams.notification_failed",
                    "channel": teams_channel,
                    "error": f"{type(e).__name__}: {e}",
                },
            )

    return {
        "reasoning": analysis.get("reasoning", ""),
        "summary": analysis.get("summary", ""),
        "severity": analysis.get("severity", ""),
        "actions": analysis.get("actions", []),
        "should_escalate": analysis.get("should_escalate", False),
        "teams_sent": teams_sent,
    }
