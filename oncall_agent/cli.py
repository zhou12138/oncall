"""CLI entrypoint — oncall command."""

import sys


def main():
    if len(sys.argv) < 2:
        _help()
        return

    cmd = sys.argv[1]

    if cmd == "onboard":
        from oncall_agent.onboard import run_wizard
        run_wizard()
    elif cmd == "login":
        import asyncio
        from oncall_agent.copilot_proxy import get_proxy
        proxy = get_proxy()
        asyncio.run(proxy.login())
    elif cmd == "status":
        from oncall_agent.copilot_proxy import get_proxy
        proxy = get_proxy()
        if proxy.is_logged_in:
            valid = "✅ valid" if proxy.is_token_valid else "⚠️  expired (will auto-refresh)"
            print(f"GitHub: ✅ logged in")
            print(f"Copilot token: {valid}")
        else:
            print("GitHub: ❌ not logged in")
            print("Run: oncall login")
    elif cmd == "workspace" or cmd == "ws":
        _workspace_cmd()
    elif cmd == "serve":
        from oncall_agent.api import main as serve_main
        serve_main()
    elif cmd == "chat":
        from oncall_agent.tui import main as tui_main
        tui_main()
    elif cmd == "config":
        _show_config()
    elif cmd in ("-h", "--help", "help"):
        _help()
    elif cmd in ("-v", "--version", "version"):
        from importlib.metadata import version as get_version
        print(f"oncall-agent {get_version('oncall-agent')}")
    else:
        print(f"Unknown command: {cmd}")
        _help()
        sys.exit(1)


def _show_config():
    from oncall_agent.config import config, CONFIG_PATH
    import json
    print(f"Config file: {CONFIG_PATH}")
    print(json.dumps(config.model_dump(), indent=2, default=str))


def _workspace_cmd():
    from oncall_agent.workspace import WorkspaceManager
    args = sys.argv[2:]

    if not args or args[0] == "list":
        workspaces = WorkspaceManager.list_workspaces()
        active = WorkspaceManager.get_active()
        if not workspaces:
            print("No workspaces. Create one: oncall ws create <name>")
            return
        print("\nWorkspaces:")
        for ws_name in workspaces:
            marker = " → active" if ws_name == active else ""
            print(f"  {'●' if ws_name == active else '○'} {ws_name}{marker}")
        print()

    elif args[0] == "create":
        if len(args) < 2:
            print("Usage: oncall ws create <name> [--team TEAM] [--desc DESCRIPTION]")
            return
        name = args[1]
        team = ""
        desc = ""
        for i, a in enumerate(args[2:], 2):
            if a == "--team" and i + 1 < len(args):
                team = args[i + 1]
            elif a == "--desc" and i + 1 < len(args):
                desc = args[i + 1]
        ws = WorkspaceManager.create(name, team=team, description=desc)
        WorkspaceManager.set_active(name)
        print(f"✅ Created workspace: {name}")
        print(f"   soul.md:   {ws.soul_path}")
        print(f"   memory.md: {ws.memory_path}")
        print(f"   Active workspace set to: {name}")

    elif args[0] == "use":
        if len(args) < 2:
            print("Usage: oncall ws use <name>")
            return
        name = args[1]
        ws = WorkspaceManager.get(name)
        if not ws.exists:
            print(f"Workspace '{name}' not found. Create it: oncall ws create {name}")
            return
        WorkspaceManager.set_active(name)
        print(f"Active workspace: {name}")

    elif args[0] == "show":
        name = args[1] if len(args) > 1 else WorkspaceManager.get_active()
        if not name:
            print("No active workspace. Use: oncall ws use <name>")
            return
        ws = WorkspaceManager.get(name)
        if not ws.exists:
            print(f"Workspace '{name}' not found.")
            return
        print(f"\n═══ {name} ═══")
        print(f"\n--- soul.md ---\n{ws.read_soul()}")
        print(f"\n--- memory.md ---\n{ws.read_memory()}")

    elif args[0] == "delete":
        if len(args) < 2:
            print("Usage: oncall ws delete <name>")
            return
        name = args[1]
        confirm = input(f"Delete workspace '{name}'? [y/N] ")
        if confirm.lower() == "y":
            WorkspaceManager.delete(name)
            print(f"Deleted: {name}")

    else:
        print(f"Unknown workspace command: {args[0]}")
        print("Commands: list, create, use, show, delete")


def _help():
    print("""
🚨 OnCall Agent CLI

Usage: oncall <command>

Commands:
  login     GitHub Copilot device code login
  status    Show login & token status
  onboard   Interactive setup wizard (first-time config)
  workspace Manage project workspaces (alias: ws)
  serve     Start the HTTP API server
  chat      Interactive TUI chat (default mode)
  config    Show current configuration
  help      Show this message

Workspace:
  oncall ws               List workspaces
  oncall ws create <name> Create new workspace (soul.md + memory.md)
  oncall ws use <name>    Switch active workspace
  oncall ws show [name]   Show soul.md + memory.md
  oncall ws delete <name> Delete workspace

Examples:
  oncall onboard          # Run setup wizard
  oncall ws create myproj # Create workspace
  oncall ws use myproj    # Switch active workspace
  oncall chat             # Chat in active workspace context
  oncall serve            # Start on configured port
""")


if __name__ == "__main__":
    main()
