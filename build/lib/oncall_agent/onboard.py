"""OnCall Agent onboard wizard — TUI with rich + prompt_toolkit.

Steps:
  1. Model selection (GitHub Copilot device code flow built-in)
  2. Connector config (ICM, Metrics, Kusto, GitHub)
  3. Memory scheme
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.rule import Rule
from rich.columns import Columns
from rich.syntax import Syntax

console = Console()

CONFIG_DIR = Path.home() / ".oncall"
CONFIG_PATH = CONFIG_DIR / "config.json"
ENV_PATH = CONFIG_DIR / ".env"

# ═══════════════════════════════════════════════════════════════════════════════
# GitHub Copilot Device Code Flow
# ═══════════════════════════════════════════════════════════════════════════════

GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"  # GitHub Copilot CLI client ID

async def github_device_code_flow() -> str | None:
    """OAuth device code flow for GitHub Copilot. Returns access token."""

    console.print()
    console.print("[bold cyan]🔐 GitHub Copilot — Device Code Authorization[/bold cyan]")
    console.print()

    async with httpx.AsyncClient(timeout=30) as client:
        # Step 1: Request device code
        try:
            resp = await client.post(
                "https://github.com/login/device/code",
                headers={"Accept": "application/json"},
                data={"client_id": GITHUB_CLIENT_ID, "scope": "read:user"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            console.print(f"[red]Failed to request device code: {e}[/red]")
            return None

        user_code = data["user_code"]
        device_code = data["device_code"]
        verification_uri = data["verification_uri"]
        expires_in = data.get("expires_in", 900)
        interval = data.get("interval", 5)

        # Step 2: Show code to user
        console.print(Panel(
            f"[bold yellow]1.[/bold yellow] Open in browser: [bold underline cyan]{verification_uri}[/bold underline cyan]\n"
            f"[bold yellow]2.[/bold yellow] Enter code: [bold white on dark_red]  {user_code}  [/bold white on dark_red]\n\n"
            f"[dim]Waiting for authorization (expires in {expires_in // 60} min)...[/dim]",
            title="[bold]Authorize GitHub Copilot[/bold]",
            border_style="green",
        ))

        # Step 3: Poll for token
        deadline = time.time() + expires_in
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Waiting for browser authorization...", total=None)

            while time.time() < deadline:
                await asyncio.sleep(interval)
                try:
                    token_resp = await client.post(
                        "https://github.com/login/oauth/access_token",
                        headers={"Accept": "application/json"},
                        data={
                            "client_id": GITHUB_CLIENT_ID,
                            "device_code": device_code,
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        },
                    )
                    token_data = token_resp.json()
                except Exception:
                    continue

                error = token_data.get("error")
                if error == "authorization_pending":
                    continue
                elif error == "slow_down":
                    interval += 5
                    continue
                elif error:
                    console.print(f"[red]Auth error: {error} — {token_data.get('error_description', '')}[/red]")
                    return None
                else:
                    access_token = token_data.get("access_token")
                    if access_token:
                        progress.update(task, description="[green]✓ Authorized![/green]")
                        console.print("[bold green]✅ GitHub Copilot authorized successfully![/bold green]")
                        return access_token

        console.print("[red]Authorization timed out.[/red]")
        return None


async def get_copilot_token(github_token: str) -> str | None:
    """Exchange GitHub OAuth token for a Copilot API token."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(
                "https://api.github.com/copilot_internal/v2/token",
                headers={
                    "Authorization": f"token {github_token}",
                    "Accept": "application/json",
                },
            )
            if resp.status_code == 200:
                return resp.json().get("token")
        except Exception:
            pass
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Step 1: Model Selection
# ═══════════════════════════════════════════════════════════════════════════════

MODEL_PRESETS = {
    "1": {"name": "GitHub Copilot (gpt-4o)", "api_base": "https://api.githubcopilot.com", "model": "gpt-4o", "auth": "copilot"},
    "2": {"name": "GitHub Copilot (claude-3.5-sonnet)", "api_base": "https://api.githubcopilot.com", "model": "claude-3.5-sonnet", "auth": "copilot"},
    "3": {"name": "GitHub Copilot (o3-mini)", "api_base": "https://api.githubcopilot.com", "model": "o3-mini", "auth": "copilot"},
    "4": {"name": "Azure OpenAI", "api_base": "", "model": "gpt-4o", "auth": "api_key"},
    "5": {"name": "OpenAI Direct", "api_base": "https://api.openai.com/v1", "model": "gpt-4o", "auth": "api_key"},
    "6": {"name": "Local (Ollama / vLLM)", "api_base": "http://localhost:11434/v1", "model": "llama3", "auth": "none"},
    "7": {"name": "Custom endpoint", "api_base": "", "model": "", "auth": "api_key"},
}


async def step_model(cfg: dict) -> dict:
    console.print(Rule("[bold yellow]Step 1 / 3 — LLM Model[/bold yellow]", style="yellow"))
    console.print()

    # Show options
    table = Table(show_header=False, border_style="dim", pad_edge=False, box=None)
    table.add_column("Key", style="bold cyan", width=4)
    table.add_column("Provider", width=45)
    for k, v in MODEL_PRESETS.items():
        table.add_row(f" {k})", v["name"])
    console.print(table)
    console.print()

    choice = Prompt.ask("Select provider", choices=list(MODEL_PRESETS.keys()), default="1")
    preset = MODEL_PRESETS[choice]

    llm_cfg = {
        "api_base": preset["api_base"],
        "model": preset["model"],
        "temperature": 0.3,
        "auth_type": preset["auth"],
    }

    # Auth flow
    if preset["auth"] == "copilot":
        console.print()
        auth_method = Prompt.ask(
            "GitHub auth method",
            choices=["device", "token"],
            default="device",
        )
        if auth_method == "device":
            token = await github_device_code_flow()
            if token:
                _save_env("GITHUB_TOKEN", token)
                llm_cfg["token_env"] = "GITHUB_TOKEN"
            else:
                console.print("[yellow]Skipped — set GITHUB_TOKEN manually later[/yellow]")
                llm_cfg["token_env"] = "GITHUB_TOKEN"
        else:
            token = Prompt.ask("GitHub Token", password=True)
            if token:
                _save_env("GITHUB_TOKEN", token)
            llm_cfg["token_env"] = "GITHUB_TOKEN"

    elif preset["auth"] == "api_key":
        if not preset["api_base"]:
            llm_cfg["api_base"] = Prompt.ask("API base URL")
        key = Prompt.ask("API Key", password=True, default="")
        if key:
            _save_env("LLM_API_KEY", key)
        llm_cfg["token_env"] = "LLM_API_KEY"

    # Model customization
    console.print()
    custom_model = Prompt.ask("Model name", default=llm_cfg["model"])
    llm_cfg["model"] = custom_model

    temp = Prompt.ask("Temperature (0.0-1.0)", default=str(llm_cfg["temperature"]))
    llm_cfg["temperature"] = float(temp)

    cfg["llm"] = llm_cfg

    console.print()
    console.print(f"[green]✓ LLM: {llm_cfg['api_base']}  model={llm_cfg['model']}[/green]")
    return cfg


# ═══════════════════════════════════════════════════════════════════════════════
# Step 2: Connectors
# ═══════════════════════════════════════════════════════════════════════════════

CONNECTORS = [
    {
        "key": "icm",
        "name": "ICM (Incident Management)",
        "description": "Receive and manage ICM incidents",
        "types": ["mcp_sse", "mcp_stdio", "webhook", "skip"],
        "default_url": "http://localhost:8091/sse",
    },
    {
        "key": "kusto",
        "name": "Kusto / ADX",
        "description": "Azure Data Explorer queries for metrics & logs",
        "types": ["mcp_sse", "mcp_stdio", "skip"],
        "default_url": "http://localhost:8092/sse",
    },
    {
        "key": "github",
        "name": "GitHub",
        "description": "GitHub metrics, PRs, issues",
        "types": ["mcp_sse", "mcp_stdio", "skip"],
        "default_url": "http://localhost:8093/sse",
    },
    {
        "key": "metrics",
        "name": "Metrics (Geneva / MDM)",
        "description": "Time-series metrics source",
        "types": ["mcp_sse", "mcp_stdio", "skip"],
        "default_url": "http://localhost:8094/sse",
    },
    {
        "key": "teams",
        "name": "Teams Notification",
        "description": "Send alerts and summaries to Teams channels",
        "types": ["mcp_sse", "webhook", "skip"],
        "default_url": "http://localhost:8095/sse",
    },
]


def step_connectors(cfg: dict) -> dict:
    console.print()
    console.print(Rule("[bold yellow]Step 2 / 3 — Connectors[/bold yellow]", style="yellow"))
    console.print()
    console.print("[dim]Configure data sources and notification channels.[/dim]")
    console.print("[dim]Press Enter to skip any connector — you can configure later.[/dim]")

    connectors = {}

    for conn in CONNECTORS:
        console.print()
        console.print(f"[bold cyan]📌 {conn['name']}[/bold cyan]  [dim]{conn['description']}[/dim]")

        type_choices = {
            "mcp_sse": "MCP Server (SSE)",
            "mcp_stdio": "MCP Server (stdio command)",
            "webhook": "Webhook URL",
            "skip": "Skip (configure later)",
        }
        available = {k: v for k, v in type_choices.items() if k in conn["types"]}

        # Show type options inline
        opts = " / ".join(f"[cyan]{k}[/cyan]" for k in available.keys())
        console.print(f"  Type: {opts}")

        conn_type = Prompt.ask("  Connection type", choices=list(available.keys()), default="skip")

        connector_cfg = {"type": conn_type, "enabled": conn_type != "skip"}

        if conn_type == "mcp_sse":
            url = Prompt.ask("  MCP SSE URL", default=conn["default_url"])
            connector_cfg["url"] = url
        elif conn_type == "mcp_stdio":
            cmd = Prompt.ask("  stdio command (e.g. npx @mcp/kusto-server)")
            connector_cfg["command"] = cmd
        elif conn_type == "webhook":
            url = Prompt.ask("  Webhook URL")
            connector_cfg["url"] = url
            secret = Prompt.ask("  Webhook secret (optional)", password=True, default="")
            if secret:
                env_key = f"{conn['key'].upper()}_WEBHOOK_SECRET"
                _save_env(env_key, secret)
                connector_cfg["secret_env"] = env_key

        # Connector-specific extras
        if conn["key"] == "teams" and conn_type != "skip":
            connector_cfg["default_channel"] = Prompt.ask("  Default Teams channel", default="")
        elif conn["key"] == "github" and conn_type != "skip":
            connector_cfg["default_repo"] = Prompt.ask("  Default repo (owner/repo)", default="")
        elif conn["key"] == "kusto" and conn_type != "skip":
            connector_cfg["default_cluster"] = Prompt.ask("  Default cluster URL", default="")
            connector_cfg["default_database"] = Prompt.ask("  Default database", default="")

        connectors[conn["key"]] = connector_cfg

        status = "[green]✓ Configured[/green]" if connector_cfg["enabled"] else "[dim]⊘ Skipped[/dim]"
        console.print(f"  {status}")

    cfg["connectors"] = connectors
    return cfg


# ═══════════════════════════════════════════════════════════════════════════════
# Step 3: Memory Scheme
# ═══════════════════════════════════════════════════════════════════════════════

MEMORY_SCHEMES = {
    "1": {
        "name": "JSON File (simple)",
        "description": "Local JSON file — zero dependencies, good for single-node",
        "type": "json",
    },
    "2": {
        "name": "SQLite + Vector (semantic search)",
        "description": "SQLite DB with embedding vectors for semantic incident recall",
        "type": "sqlite_vector",
    },
    "3": {
        "name": "Redis (shared / multi-agent)",
        "description": "Redis backend for multi-agent sharing and fast lookup",
        "type": "redis",
    },
}


def step_memory(cfg: dict) -> dict:
    console.print()
    console.print(Rule("[bold yellow]Step 3 / 3 — Memory Scheme[/bold yellow]", style="yellow"))
    console.print()

    table = Table(show_header=False, border_style="dim", box=None)
    table.add_column("Key", style="bold cyan", width=4)
    table.add_column("Scheme", width=40)
    table.add_column("Description", style="dim")
    for k, v in MEMORY_SCHEMES.items():
        table.add_row(f" {k})", v["name"], v["description"])
    console.print(table)
    console.print()

    choice = Prompt.ask("Select memory scheme", choices=list(MEMORY_SCHEMES.keys()), default="1")
    scheme = MEMORY_SCHEMES[choice]

    memory_cfg = {"type": scheme["type"]}

    if scheme["type"] == "json":
        path = Prompt.ask("Memory file path", default=str(CONFIG_DIR / "memory.json"))
        memory_cfg["path"] = path
    elif scheme["type"] == "sqlite_vector":
        path = Prompt.ask("SQLite DB path", default=str(CONFIG_DIR / "memory.db"))
        memory_cfg["path"] = path
        memory_cfg["embedding_model"] = Prompt.ask("Embedding model", default="text-embedding-3-small")
    elif scheme["type"] == "redis":
        memory_cfg["url"] = Prompt.ask("Redis URL", default="redis://localhost:6379/0")
        memory_cfg["prefix"] = Prompt.ask("Key prefix", default="oncall:")

    # Memory sections
    console.print()
    console.print("[bold]Memory sections:[/bold]")
    sections = ["incidents", "patterns", "runbooks", "wow_comparisons"]
    console.print(f"  [cyan]{', '.join(sections)}[/cyan]")
    extra = Prompt.ask("Add custom sections (comma-separated, or Enter to skip)", default="")
    if extra:
        sections.extend([s.strip() for s in extra.split(",") if s.strip()])
    memory_cfg["sections"] = sections

    # Retention
    console.print()
    retention = Prompt.ask("Retention days (0 = forever)", default="90")
    memory_cfg["retention_days"] = int(retention)

    cfg["memory"] = memory_cfg

    console.print()
    console.print(f"[green]✓ Memory: {scheme['name']} — retention {retention}d[/green]")
    return cfg


# ═══════════════════════════════════════════════════════════════════════════════
# Summary & Save
# ═══════════════════════════════════════════════════════════════════════════════

def show_summary(cfg: dict):
    console.print()
    console.print(Rule("[bold cyan]Configuration Summary[/bold cyan]", style="cyan"))

    # LLM
    llm = cfg.get("llm", {})
    console.print(Panel(
        f"[bold]Provider:[/bold]  {llm.get('api_base', '')}\n"
        f"[bold]Model:[/bold]     {llm.get('model', '')}\n"
        f"[bold]Temp:[/bold]      {llm.get('temperature', 0.3)}\n"
        f"[bold]Auth:[/bold]      {llm.get('auth_type', '')} → ${llm.get('token_env', 'N/A')}",
        title="LLM", border_style="green",
    ))

    # Connectors
    conn_table = Table(border_style="cyan", title="Connectors")
    conn_table.add_column("Connector", style="bold")
    conn_table.add_column("Type")
    conn_table.add_column("Endpoint")
    conn_table.add_column("Status")
    for key, c in cfg.get("connectors", {}).items():
        status = "[green]✓[/green]" if c.get("enabled") else "[dim]⊘[/dim]"
        endpoint = c.get("url", c.get("command", "—"))
        conn_table.add_row(key, c.get("type", ""), str(endpoint)[:50], status)
    console.print(conn_table)

    # Memory
    mem = cfg.get("memory", {})
    console.print(Panel(
        f"[bold]Scheme:[/bold]     {mem.get('type', '')}\n"
        f"[bold]Path:[/bold]       {mem.get('path', mem.get('url', ''))}\n"
        f"[bold]Sections:[/bold]   {', '.join(mem.get('sections', []))}\n"
        f"[bold]Retention:[/bold]  {mem.get('retention_days', 0)} days",
        title="Memory", border_style="magenta",
    ))

    # Config as JSON preview
    console.print()
    console.print(f"[dim]Config file: {CONFIG_PATH}[/dim]")
    if ENV_PATH.exists():
        console.print(f"[dim]Secrets:     {ENV_PATH} (chmod 600)[/dim]")


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    CONFIG_PATH.chmod(0o600)


# ═══════════════════════════════════════════════════════════════════════════════
# Env file helper
# ═══════════════════════════════════════════════════════════════════════════════

def _save_env(key: str, value: str):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    found = False
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    ENV_PATH.chmod(0o600)


# ═══════════════════════════════════════════════════════════════════════════════
# Main Wizard
# ═══════════════════════════════════════════════════════════════════════════════

async def run_wizard_async():
    console.print()
    console.print(Panel(
        "[bold cyan]🚨 OnCall Agent — Onboard Wizard[/bold cyan]\n\n"
        "[dim]3 steps to get your oncall agent running[/dim]\n"
        "  [yellow]1.[/yellow] LLM Model & Auth\n"
        "  [yellow]2.[/yellow] Connectors (ICM, Kusto, GitHub, Metrics, Teams)\n"
        "  [yellow]3.[/yellow] Memory Scheme",
        border_style="cyan",
    ))

    # Load existing or start fresh
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                existing = json.load(f)
            console.print(f"\n[yellow]Found existing config: {CONFIG_PATH}[/yellow]")
            if Confirm.ask("Reconfigure from scratch?", default=False):
                cfg = {}
            else:
                cfg = existing
                console.print("[dim]Will update existing config[/dim]")
        except Exception:
            cfg = {}

    # Step 1
    cfg = await step_model(cfg)

    # Step 2
    cfg = step_connectors(cfg)

    # Step 3
    cfg = step_memory(cfg)

    # Summary
    show_summary(cfg)

    # Save
    console.print()
    if Confirm.ask("[bold]Save configuration?[/bold]", default=True):
        save_config(cfg)
        console.print()
        console.print("[bold green]✅ Configuration saved![/bold green]")
        console.print()
        console.print("  Next steps:")
        console.print("  [bold cyan]oncall chat[/bold cyan]     → Start interactive TUI")
        console.print("  [bold cyan]oncall serve[/bold cyan]    → Start HTTP API server")
        console.print("  [bold cyan]oncall config[/bold cyan]   → View configuration")
        console.print()
    else:
        console.print("[dim]Discarded.[/dim]")


def run_wizard():
    asyncio.run(run_wizard_async())


if __name__ == "__main__":
    run_wizard()
