"""Memory mechanism — persistent cross-session oncall context.

Inspired by Hermes Agent memory design:
- Frozen snapshot: system prompt injection is stable within a session
- Character limits: bounded memory prevents unbounded growth
- Security scanning: blocks prompt injection / exfiltration payloads
- Atomic writes: temp file + os.replace for crash-safe concurrency

Stores:
- Past incidents (issue, root cause, resolution)
- Known patterns (recurring alerts, flaky services)
- Runbooks and heuristics learned over time
"""

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import fcntl
except ImportError:
    fcntl = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Character limits per section (not tokens — model-independent)
# ---------------------------------------------------------------------------
DEFAULT_SECTION_LIMITS: Dict[str, int] = {
    "incidents": 4000,
    "patterns": 2000,
    "runbooks": 2000,
    "wow_comparisons": 1500,
}

TOTAL_CHAR_LIMIT = 10000  # hard cap for entire memory file

# ---------------------------------------------------------------------------
# Security: content scanning before write
# ---------------------------------------------------------------------------
_THREAT_PATTERNS = [
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'you\s+are\s+now\s+', "role_hijack"),
    (r'do\s+not\s+tell\s+the\s+user', "deception"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules)', "disregard_rules"),
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD)', "exfil_curl"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD)', "exfil_wget"),
]

_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}


def scan_content(text: str) -> Optional[str]:
    """Scan text for injection/exfil threats. Returns error msg if blocked."""
    for char in _INVISIBLE_CHARS:
        if char in text:
            return f"Blocked: invisible unicode U+{ord(char):04X}"
    for pattern, pid in _THREAT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return f"Blocked: threat pattern '{pid}'"
    return None


class OncallMemory:
    """JSON-backed memory with frozen snapshots, char limits, security & atomic IO.

    Frozen snapshot pattern:
      - On __init__ (load), a snapshot of get_context_for_llm() is captured.
      - system_prompt_snapshot stays stable for the session lifetime.
      - Live state (self.data) is mutated by add/record, written to disk.
      - Next session re-creates snapshot from fresh disk state.
    """

    def __init__(
        self,
        path: str = "./memory/oncall_memory.json",
        section_limits: Optional[Dict[str, int]] = None,
        total_limit: int = TOTAL_CHAR_LIMIT,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.section_limits = section_limits or DEFAULT_SECTION_LIMITS
        self.total_limit = total_limit
        self.data: Dict[str, List[Dict[str, Any]]] = {
            "incidents": [],
            "patterns": [],
            "runbooks": [],
            "wow_comparisons": [],
        }
        self._load()
        # Frozen snapshot — stable for entire session
        self._system_prompt_snapshot: str = self._build_context()

    # -- Frozen snapshot (Hermes-style) --

    @property
    def system_prompt_snapshot(self) -> str:
        """Return the frozen context captured at init. Never changes mid-session."""
        return self._system_prompt_snapshot

    # -- Atomic file I/O --

    def _load(self):
        """Load from disk. Atomic reads: os.replace guarantees complete files."""
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Memory load failed, starting fresh: %s", e)

    def _save(self):
        """Atomic write: tmpfile + fsync + os.replace (crash-safe)."""
        content = json.dumps(self.data, indent=2, default=str, ensure_ascii=False)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.path.parent), suffix=".tmp", prefix=".mem_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(self.path))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as e:
            logger.error("Memory save failed: %s", e)

    def _reload_under_lock(self):
        """Re-read from disk under lock to pick up other sessions' writes."""
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    def _locked_write(self):
        """Acquire file lock, reload, then caller mutates, then save."""
        # Used as context manager pattern — see add() and _evict_oldest()
        pass

    # -- Character limit enforcement --

    def _section_char_count(self, section: str) -> int:
        """Current char count for a section."""
        entries = self.data.get(section, [])
        if not entries:
            return 0
        return len(json.dumps(entries, default=str, ensure_ascii=False))

    def _total_char_count(self) -> int:
        return len(json.dumps(self.data, default=str, ensure_ascii=False))

    def _evict_oldest(self, section: str, needed_chars: int):
        """Remove oldest entries from section until there's room for needed_chars."""
        limit = self.section_limits.get(section, 3000)
        entries = self.data.get(section, [])
        while entries and self._section_char_count(section) + needed_chars > limit:
            removed = entries.pop(0)
            logger.info("Evicted oldest entry from '%s': %s", section,
                        str(removed)[:80])

    def _evict_global(self):
        """If total memory exceeds limit, evict oldest entries from largest section."""
        while self._total_char_count() > self.total_limit:
            # Find the largest section
            largest = max(
                self.data.keys(),
                key=lambda s: len(self.data.get(s, []))
            )
            entries = self.data.get(largest, [])
            if not entries:
                break
            removed = entries.pop(0)
            logger.info("Global eviction from '%s': %s", largest, str(removed)[:80])

    # -- Deduplication --

    def _is_duplicate(self, section: str, entry: Dict[str, Any]) -> bool:
        """Check if an essentially identical entry already exists."""
        # Compare on key fields, ignoring timestamp
        key_fields = {k: v for k, v in entry.items() if k != "timestamp"}
        for existing in self.data.get(section, []):
            existing_keys = {k: v for k, v in existing.items() if k != "timestamp"}
            if existing_keys == key_fields:
                return True
        return False

    # -- Public API (backward compatible) --

    def add(self, section: str, entry: Dict[str, Any]):
        """Add entry with security scan, dedup, char limits, and atomic write."""
        # Security scan
        entry_text = json.dumps(entry, default=str)
        threat = scan_content(entry_text)
        if threat:
            logger.warning("Memory write blocked: %s", threat)
            return

        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

        # File-locked read-modify-write
        lock_path = self.path.with_suffix(".json.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        lock_fd = None
        try:
            if fcntl:
                lock_fd = open(lock_path, "a+")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)

            # Reload latest state from disk
            self._reload_under_lock()

            # Dedup check
            if self._is_duplicate(section, entry):
                logger.debug("Duplicate entry skipped in '%s'", section)
                return

            # Evict if needed
            entry_chars = len(entry_text)
            self._evict_oldest(section, entry_chars)

            # Add
            self.data.setdefault(section, []).append(entry)

            # Global limit check
            self._evict_global()

            # Atomic save
            self._save()

        finally:
            if lock_fd:
                if fcntl:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()

    def search(self, section: str, keyword: str) -> List[Dict]:
        """Simple keyword search within a section."""
        results = []
        for entry in self.data.get(section, []):
            text = json.dumps(entry, default=str).lower()
            if keyword.lower() in text:
                results.append(entry)
        return results

    def get_recent(self, section: str, n: int = 10) -> List[Dict]:
        return self.data.get(section, [])[-n:]

    def record(self, signal_name: str, result: dict):
        """Record a completed oncall analysis to incidents."""
        self.add("incidents", {
            "signal": signal_name,
            "title": signal_name,
            "severity": result.get("severity", "unknown"),
            "summary": result.get("summary", "")[:500],
            "mode": result.get("mode", "mcp"),
        })

    def get_context_for_llm(self, limit: int = 5) -> str:
        """Build live memory context (for ad-hoc queries, NOT system prompt).

        For system prompt injection, use .system_prompt_snapshot instead
        to maintain stable prefix caching.
        """
        return self._build_context(limit)

    def _build_context(self, limit: int = 5) -> str:
        """Internal: render memory sections as text."""
        parts = []
        total = self._total_char_count()

        recent_incidents = self.get_recent("incidents", limit)
        if recent_incidents:
            parts.append("## Recent Incidents")
            for inc in recent_incidents:
                parts.append(
                    f"- [{inc.get('timestamp','')}] "
                    f"{inc.get('title','')}: {inc.get('summary','')}"
                )

        recent_patterns = self.get_recent("patterns", limit)
        if recent_patterns:
            parts.append("\n## Known Patterns")
            for p in recent_patterns:
                parts.append(f"- {p.get('pattern','')}")

        recent_wow = self.get_recent("wow_comparisons", 3)
        if recent_wow:
            parts.append("\n## Recent WoW Comparisons")
            for w in recent_wow:
                parts.append(
                    f"- [{w.get('timestamp','')}] {w.get('summary','')}"
                )

        if not parts:
            return "(No prior oncall memory)"

        header = f"[Memory: {total:,}/{self.total_limit:,} chars]"
        return header + "\n" + "\n".join(parts)

    # -- Stats --

    def stats(self) -> Dict[str, Any]:
        """Return memory usage stats per section."""
        result = {}
        for section in self.data:
            count = len(self.data[section])
            chars = self._section_char_count(section)
            limit = self.section_limits.get(section, 3000)
            result[section] = {
                "entries": count,
                "chars": chars,
                "limit": limit,
                "usage_pct": int(chars / limit * 100) if limit else 0,
            }
        result["_total"] = {
            "chars": self._total_char_count(),
            "limit": self.total_limit,
            "usage_pct": int(self._total_char_count() / self.total_limit * 100),
        }
        return result
