"""TUI Chat — interactive terminal UI for OnCall Agent.

Normal input → LLM chat (with oncall memory context)
oncall: <icm info> → triggers the 3-step oncall workflow
"""

import asyncio
import json
import sys
from datetime import datetime, timezone

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from oncall_agent.config import config
from oncall_agent.copilot_proxy import get_proxy
from oncall_agent.memory.store import OncallMemory
from oncall_agent.orchestrator import OncallOrchestrator
from oncall_agent.workspace import WorkspaceManager, Workspace

# ── Style ────────────────────────────────────────────────────────────────────

console = Console()

PROMPT_STYLE = Style.from_dict({
    "prompt": "#00d7ff bold",
    "pound": "#ffaf00",
})

SYSTEM_PROMPT = """You are an OnCall assistant for a software engineering team.
You help with incident triage, monitoring analysis, and oncall workflows.
You have access to oncall memory with past incidents and patterns.
Be concise and actionable. Use markdown formatting.

{workspace_context}

{memory_context}
"""

COMMANDS = {
    "help":    "Show available commands",
    "oncall":  "Trigger oncall workflow — oncall: <signal_name> [repo=owner/repo] [channel=teams-channel]",
    "memory":  "Show oncall memory",
    "clear":   "Clear screen",
    "history": "Show recent incidents from memory",
    "ws":      "Switch workspace — ws <name> or ws to list",
    "soul":    "Show/edit current workspace soul.md",
    "exit":    "Quit",
}

# ── Chat History ─────────────────────────────────────────────────────────────

class ChatSession:
    def __init__(self):
        self.messages: list[dict] = []
        self.memory = OncallMemory(config.memory_path)
        self.orchestrator = OncallOrchestrator()
        self.workspace: Workspace | None = WorkspaceManager.get_active_workspace()

    def system_prompt(self) -> str:
        ws_context = ""
        if self.workspace:
            ws_context = self.workspace.get_llm_context()
        return SYSTEM_PROMPT.format(
            workspace_context=ws_context,
            memory_context=self.memory.system_prompt_snapshot,
        )

    def add_user(self, text: str):
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, text: str):
        self.messages.append({"role": "assistant", "content": text})

    def build_messages(self) -> list[dict]:
        # Keep last 20 turns + system
        msgs = [{"role": "system", "content": self.system_prompt()}]
        msgs.extend(self.messages[-40:])
        return msgs


# ── LLM Streaming ────────────────────────────────────────────────────────────

async def stream_chat(session: ChatSession, user_input: str, model: str = None):
    """Stream LLM response via Copilot Proxy."""
    session.add_user(user_input)

    proxy = get_proxy()
    if not proxy.is_logged_in:
        console.print("[yellow]Not logged in. Run [bold]oncall login[/bold] first.[/yellow]")
        return

    full_response = ""
    console.print()

    try:
        async for chunk in proxy.chat_completion_stream(
            messages=session.build_messages(),
            model=model or config.llm_model,
            temperature=config.llm_temperature,
        ):
            full_response += chunk
            print(chunk, end="", flush=True)

        print()  # newline after stream
        session.add_assistant(full_response)

    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
    except httpx.HTTPStatusError as e:
        console.print(f"[red]LLM Error: {e.response.status_code} {e.response.text[:200]}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


# ── Oncall Workflow ──────────────────────────────────────────────────────────

async def run_oncall_workflow(session: ChatSession, args: str):
    """Parse oncall command and run 3-step pipeline."""
    # Parse: oncall: SignalName [repo=xxx] [channel=xxx] [model=xxx]
    parts = args.strip().split()
    if not parts:
        console.print("[red]Usage: oncall: <signal_name> [repo=owner/repo] [channel=teams-channel] [model=gpt-4o][/red]")
        return

    signal_name_parts = []
    repo = config.default_repo
    channel = config.default_teams_channel
    model = None

    for p in parts:
        if p.startswith("repo="):
            repo = p.split("=", 1)[1]
        elif p.startswith("channel="):
            channel = p.split("=", 1)[1]
        elif p.startswith("model="):
            model = p.split("=", 1)[1]
        else:
            signal_name_parts.append(p)

    signal_name = " ".join(signal_name_parts) if signal_name_parts else ""
    if not signal_name:
        console.print("[red]Usage: oncall: <signal description> [repo=owner/repo] [channel=teams-channel] [model=gpt-4o][/red]")
        return

    # Show what we're doing
    table = Table(title="🚨 OnCall Workflow", show_header=False, border_style="red")
    table.add_row("Signal", signal_name)
    if repo:
        table.add_row("Repo", repo)
    if channel:
        table.add_row("Teams Channel", channel)
    table.add_row("Model", model or config.llm_model)
    console.print(table)
    console.print()

    # Run pipeline with live spinner
    try:
        mode = "mock" if not session.orchestrator._mcp_available else "mcp"
        mode_label = "mock data" if mode == "mock" else "MCP connectors"
        console.print(f"[bold cyan]🔍 Running 3-step pipeline ({mode_label})...[/bold cyan]\n")

        result = await session.orchestrator.run(
            signal_name=signal_name,
            repo=repo,
            teams_channel=channel,
            model=model,
        )
        _display_oncall_result(result)

        # Add to chat context
        summary = (
            f"OnCall analysis for **{signal_name}**:\n"
            f"- Severity: {result['severity']}\n"
            f"- Summary: {result['summary'][:500]}\n"
        )
        session.add_assistant(summary)

    except Exception as e:
        console.print(f"[red]Pipeline error: {e}[/red]")
        # Still allow follow-up conversation about the error
        session.add_assistant(f"OnCall workflow failed for {signal_name}: {e}")


def _display_oncall_result(result: dict):
    """Pretty-print the 3-step pipeline result."""
    triage = result["steps"]["triage"]
    wow = result["steps"]["wow"]
    analysis = result["steps"]["analysis"]

    severity_colors = {
        "critical": "red", "high": "dark_orange", "medium": "yellow",
        "low": "green", "info": "blue",
    }
    sev = analysis.get("severity", "medium")
    sev_color = severity_colors.get(sev, "white")

    # Step 1
    details = triage.get("details", {})
    triage_text = f"[bold]Verdict:[/bold] {triage['verdict']}"
    if details.get("platform_breakdown"):
        platforms = ", ".join(f"{k}: {v}" for k, v in details["platform_breakdown"].items() if v > 0)
        triage_text += f"\n[bold]Platforms:[/bold] {platforms}"
        if details.get("windows_percentage") is not None:
            triage_text += f"\n[bold]Windows:[/bold] {details['windows_percentage']}%"
    if details.get("source") == "mock":
        triage_text += "\n[dim](mock data)[/dim]"
    console.print(Panel(triage_text, title="Step 1: Triage", border_style="cyan"))

    # Step 2
    trend_icon = {"up": "📈", "down": "📉", "flat": "➡️"}.get(wow["trend"], "❓")
    wow_text = (
        f"{trend_icon} Current: {wow['current_count']}  |  Previous: {wow['previous_count']}\n"
        f"   Delta: {wow['delta']}  ({wow['change_percent']}% WoW)  Trend: {wow['trend']}"
    )
    if wow.get("source") == "mock":
        wow_text += "\n[dim](mock data)[/dim]"
    console.print(Panel(wow_text, title="Step 2: Week-over-Week 环比", border_style="cyan"))

    # Step 3
    actions_text = "\n".join(f"  • {a}" for a in analysis.get("actions", []))
    console.print(Panel(
        f"[bold {sev_color}]Severity: {sev.upper()}[/bold {sev_color}]\n\n"
        f"[bold]Summary:[/bold]\n  {analysis.get('summary', '')}\n\n"
        f"[bold]Reasoning:[/bold]\n  {analysis.get('reasoning', '')[:500]}\n\n"
        f"[bold]Actions:[/bold]\n{actions_text}\n\n"
        f"Teams notified: {'✅' if analysis.get('teams_sent') else '❌'}",
        title="Step 3: Analysis & Action", border_style=sev_color,
    ))


# ── Built-in Commands ────────────────────────────────────────────────────────

def cmd_help():
    table = Table(title="Commands", border_style="cyan")
    table.add_column("Command", style="bold cyan")
    table.add_column("Description")
    for cmd, desc in COMMANDS.items():
        prefix = "" if cmd in ("help", "clear", "exit", "history", "memory") else "oncall: "
        table.add_row(prefix + cmd if prefix else cmd, desc)
    console.print(table)


def cmd_memory(session: ChatSession):
    data = session.memory.data
    for section, entries in data.items():
        if entries:
            console.print(f"\n[bold cyan]{section}[/bold cyan] ({len(entries)} entries)")
            for e in entries[-5:]:
                console.print(f"  • {json.dumps(e, default=str)[:120]}")
    if not any(data.values()):
        console.print("[dim]Memory is empty[/dim]")


def cmd_history(session: ChatSession):
    incidents = session.memory.get_recent("incidents", 10)
    if not incidents:
        console.print("[dim]No incident history[/dim]")
        return
    table = Table(title="Recent Incidents", border_style="yellow")
    table.add_column("Time", style="dim")
    table.add_column("Signal")
    table.add_column("Severity")
    table.add_column("Verdict")
    table.add_column("Summary", max_width=50)
    for inc in reversed(incidents):
        table.add_row(
            inc.get("timestamp", "")[:19],
            inc.get("title", ""),
            inc.get("severity", ""),
            inc.get("verdict", ""),
            inc.get("summary", "")[:50],
        )
    console.print(table)


def _cmd_ws(session: ChatSession, args: str):
    """Switch or list workspaces."""
    if not args:
        # List
        workspaces = WorkspaceManager.list_workspaces()
        active = WorkspaceManager.get_active()
        if not workspaces:
            console.print("[dim]No workspaces. Create one: oncall ws create <name>[/dim]")
            return
        console.print("\n[bold]Workspaces:[/bold]")
        for name in workspaces:
            if name == active:
                console.print(f"  [green]● {name}[/green] [dim]← active[/dim]")
            else:
                console.print(f"  [dim]○ {name}[/dim]")
        console.print()
    else:
        # Switch
        name = args.strip()
        ws = WorkspaceManager.get(name)
        if not ws.exists:
            console.print(f"[yellow]Workspace '{name}' not found. Creating...[/yellow]")
            ws = WorkspaceManager.create(name)
        WorkspaceManager.set_active(name)
        session.workspace = ws
        console.print(f"[green]Switched to workspace: {name}[/green]")


def _cmd_soul(session: ChatSession):
    """Display current workspace soul.md."""
    if not session.workspace:
        console.print("[dim]No active workspace. Use: ws <name>[/dim]")
        return
    soul = session.workspace.read_soul()
    console.print(Panel(
        Markdown(soul),
        title=f"soul.md — {session.workspace.name}",
        border_style="magenta",
    ))


# ── Main Loop ────────────────────────────────────────────────────────────────

async def tui_main():
    console.print(Panel(
        "[bold cyan]🚨 OnCall Agent[/bold cyan]\n\n"
        "Chat naturally or use [bold yellow]oncall: <signal>[/bold yellow] to trigger workflow\n"
        "Type [bold]help[/bold] for commands",
        border_style="cyan",
    ))

    session = ChatSession()
    ws_name = session.workspace.name if session.workspace else "no-workspace"
    history_path = str(config.memory_path).replace("memory.json", ".chat_history")

    prompt_session = PromptSession(
        history=FileHistory(history_path),
        style=PROMPT_STYLE,
    )

    while True:
        try:
            ws_label = session.workspace.name if session.workspace else "oncall"
            user_input = await prompt_session.prompt_async(
                HTML(f"<prompt>{ws_label}</prompt> <pound>❯</pound> ")
            )
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Bye 👋[/dim]")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        # Built-in commands
        if user_input.lower() == "exit":
            console.print("[dim]Bye 👋[/dim]")
            break
        elif user_input.lower() == "clear":
            console.clear()
            continue
        elif user_input.lower() == "help":
            cmd_help()
            continue
        elif user_input.lower() == "memory":
            cmd_memory(session)
            continue
        elif user_input.lower() == "history":
            cmd_history(session)
            continue

        # Oncall workflow trigger
        if user_input.lower().startswith("oncall:"):
            args = user_input[7:].strip()
            await run_oncall_workflow(session, args)
            continue

        # Workspace commands
        if user_input.lower().startswith("ws"):
            _cmd_ws(session, user_input[2:].strip())
            continue
        if user_input.lower() == "soul":
            _cmd_soul(session)
            continue

        # Normal chat → LLM
        await stream_chat(session, user_input)


def main():
    asyncio.run(tui_main())


if __name__ == "__main__":
    main()
