"""Workspace management — project isolation with soul.md + memory.md.

Each workspace is a directory under ~/.oncall/workspaces/<name>/
containing:
  - soul.md     — project identity, goals, oncall context
  - memory.md   — accumulated knowledge, incidents, patterns
  - config.json — workspace-specific overrides (connectors, model, etc.)
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ONCALL_HOME = Path.home() / ".oncall"
WORKSPACES_DIR = ONCALL_HOME / "workspaces"

# ── Templates ────────────────────────────────────────────────────────────────

SOUL_TEMPLATE = """# {name}

## Identity
- **Project:** {name}
- **Team:** {team}
- **Created:** {created}

## Oncall Context
{description}

## Signal Patterns
<!-- Known alert patterns and their typical root causes -->

## Escalation Rules
<!-- When and how to escalate -->
- Critical: Immediately page on-call lead
- High: Notify Teams channel, create ICM
- Medium: Log and monitor
- Low: Batch for weekly review

## Runbooks
<!-- Links or inline procedures for common incidents -->

## Stakeholders
<!-- Key contacts -->
| Role | Name | Contact |
|------|------|---------|
| On-call Lead | | |
| Service Owner | | |
| Escalation | | |
"""

MEMORY_TEMPLATE = """# {name} — Memory

> Auto-updated by OnCall Agent. Manual edits welcome.

## Recent Incidents
<!-- Newest first. Agent appends here after each oncall: run -->

## Patterns
<!-- Recurring issues the agent has detected -->

## Week-over-Week Trends
<!-- Historical WoW comparisons -->

## Learned Resolutions
<!-- What worked for past incidents -->

## Notes
<!-- Free-form team notes, context, gotchas -->
"""


# ── Workspace Class ──────────────────────────────────────────────────────────

class Workspace:
    """A project workspace with soul.md and memory.md."""

    def __init__(self, name: str):
        self.name = name
        self.path = WORKSPACES_DIR / name
        self.soul_path = self.path / "soul.md"
        self.memory_path = self.path / "memory.md"
        self.config_path = self.path / "config.json"

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def create(self, team: str = "", description: str = ""):
        """Initialize a new workspace."""
        self.path.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if not self.soul_path.exists():
            self.soul_path.write_text(SOUL_TEMPLATE.format(
                name=self.name,
                team=team or "TBD",
                description=description or "<!-- Describe the project's oncall scope -->",
                created=now,
            ))

        if not self.memory_path.exists():
            self.memory_path.write_text(MEMORY_TEMPLATE.format(name=self.name))

        if not self.config_path.exists():
            self.config_path.write_text(json.dumps({"workspace": self.name}, indent=2))

    # ── Soul ─────────────────────────────────────────────────────────────

    def read_soul(self) -> str:
        if self.soul_path.exists():
            return self.soul_path.read_text()
        return ""

    def write_soul(self, content: str):
        self.soul_path.write_text(content)

    # ── Memory ───────────────────────────────────────────────────────────

    def read_memory(self) -> str:
        if self.memory_path.exists():
            return self.memory_path.read_text()
        return ""

    def append_memory(self, section: str, entry: str):
        """Append an entry under a markdown section in memory.md."""
        content = self.read_memory()
        marker = f"## {section}"
        if marker in content:
            # Insert after the section header (and any comment line)
            lines = content.split("\n")
            insert_idx = None
            for i, line in enumerate(lines):
                if line.strip() == marker:
                    insert_idx = i + 1
                    # Skip comment lines
                    while insert_idx < len(lines) and lines[insert_idx].strip().startswith("<!--"):
                        insert_idx += 1
                        while insert_idx < len(lines) and "-->" not in lines[insert_idx - 1]:
                            insert_idx += 1
                    break
            if insert_idx is not None:
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                lines.insert(insert_idx, f"\n### [{timestamp}]\n{entry}\n")
                self.memory_path.write_text("\n".join(lines))
                return
        # Section not found — append at end
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(self.memory_path, "a") as f:
            f.write(f"\n## {section}\n\n### [{timestamp}]\n{entry}\n")

    # ── Config overrides ─────────────────────────────────────────────────

    def read_config(self) -> dict:
        if self.config_path.exists():
            with open(self.config_path) as f:
                return json.load(f)
        return {}

    def write_config(self, cfg: dict):
        cfg["workspace"] = self.name
        with open(self.config_path, "w") as f:
            json.dump(cfg, f, indent=2)

    def get_llm_context(self) -> str:
        """Build full context for LLM: soul + memory."""
        parts = []
        soul = self.read_soul()
        if soul:
            parts.append(f"# Project Soul\n\n{soul}")
        memory = self.read_memory()
        if memory:
            parts.append(f"# Project Memory\n\n{memory}")
        return "\n\n---\n\n".join(parts) if parts else ""


# ── Workspace Manager ────────────────────────────────────────────────────────

class WorkspaceManager:
    """Manage multiple project workspaces."""

    @staticmethod
    def list_workspaces() -> list[str]:
        if not WORKSPACES_DIR.exists():
            return []
        return sorted([
            d.name for d in WORKSPACES_DIR.iterdir()
            if d.is_dir() and (d / "soul.md").exists()
        ])

    @staticmethod
    def get(name: str) -> Workspace:
        return Workspace(name)

    @staticmethod
    def create(name: str, team: str = "", description: str = "") -> Workspace:
        ws = Workspace(name)
        ws.create(team=team, description=description)
        return ws

    @staticmethod
    def delete(name: str):
        import shutil
        ws_path = WORKSPACES_DIR / name
        if ws_path.exists():
            shutil.rmtree(ws_path)

    @staticmethod
    def get_active() -> Optional[str]:
        """Get the currently active workspace from ~/.oncall/active_workspace."""
        active_file = ONCALL_HOME / "active_workspace"
        if active_file.exists():
            return active_file.read_text().strip()
        return None

    @staticmethod
    def set_active(name: str):
        """Set the active workspace."""
        active_file = ONCALL_HOME / "active_workspace"
        ONCALL_HOME.mkdir(parents=True, exist_ok=True)
        active_file.write_text(name)

    @staticmethod
    def get_active_workspace() -> Optional[Workspace]:
        """Get the active workspace object."""
        name = WorkspaceManager.get_active()
        if name:
            ws = Workspace(name)
            if ws.exists:
                return ws
        return None
