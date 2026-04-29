"""Configuration for OnCall Agent.

Priority: ~/.oncall/config.json > environment variables > defaults

Note: for the LLM API key, the file config stores the *name* of the env var
that holds the actual secret (under ``llm._token_env``). _get resolves that
indirection in a single pass so file > env > default ordering is consistent
with every other field.
"""

import json
import os
from pathlib import Path
from pydantic import BaseModel
from typing import Optional

CONFIG_PATH = Path.home() / ".oncall" / "config.json"
ENV_PATH = Path.home() / ".oncall" / ".env"


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def _load_file_config() -> dict:
    """Load config from ~/.oncall/config.json if exists."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def _load_env_file():
    """Load secrets from ~/.oncall/.env into os.environ."""
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# Load .env first so env vars are available
_load_env_file()
_file_cfg = _load_file_config()


def _get(file_path: list[str], env_key: str, default: str) -> str:
    """Resolve config value with strict file > env > default priority.

    The file value may be a literal, or — for indirection-style fields —
    a dict carrying ``_token_env`` whose value names an env var that holds
    the real secret. In that case we always resolve via env (file simply
    declares which env var to read).
    """
    val = _file_cfg
    for key in file_path:
        if isinstance(val, dict):
            val = val.get(key)
        else:
            val = None
            break

    # Indirection: file pointed at an env var name
    if isinstance(val, dict):
        token_env = val.get("_token_env")
        if isinstance(token_env, str) and token_env:
            return os.getenv(token_env, os.getenv(env_key, default))
        return os.getenv(env_key, default)

    if val is not None:
        return str(val)

    return os.getenv(env_key, default)


class MCPServerConfig(BaseModel):
    name: str
    url: str
    description: str = ""


class Config(BaseModel):
    # HTTP server
    host: str = _get(["server", "host"], "ONCALL_HOST", "0.0.0.0")
    port: int = int(_get(["server", "port"], "ONCALL_PORT", "8090"))

    # LLM — GitHub Copilot (OpenAI-compatible)
    llm_api_base: str = _get(["llm", "api_base"], "LLM_API_BASE", "https://api.githubcopilot.com")
    # llm_api_key: file may declare {"_token_env": "GITHUB_TOKEN"} or a literal;
    # _get handles both via the same file > env > default chain.
    llm_api_key: str = _get(["llm"], "GITHUB_TOKEN", "")
    llm_model: str = _get(["llm", "model"], "LLM_MODEL", "gpt-4o")
    llm_temperature: float = float(_get(["llm", "temperature"], "LLM_TEMPERATURE", "0.3"))

    # MCP servers
    adx_mcp: MCPServerConfig = MCPServerConfig(
        name="adx-kusto",
        url=_get(["mcp", "adx", "url"], "ADX_MCP_URL", ""),
        description="Azure Data Explorer Kusto queries",
    )
    github_mcp: MCPServerConfig = MCPServerConfig(
        name="github",
        url=_get(["mcp", "github", "url"], "GITHUB_MCP_URL", ""),
        description="GitHub metrics and operations",
    )
    teams_mcp: MCPServerConfig = MCPServerConfig(
        name="teams",
        url=_get(["mcp", "teams", "url"], "TEAMS_MCP_URL", ""),
        description="Microsoft Teams notifications",
    )

    # Memory
    memory_path: str = _get(["memory", "path"], "MEMORY_PATH", str(Path.home() / ".oncall" / "memory.json"))

    # Defaults
    default_teams_channel: str = _get(["teams", "default_channel"], "TEAMS_CHANNEL", "")
    default_repo: str = _get(["github", "default_repo"], "GITHUB_REPO", "")

    # Auth / ingestion
    api_key: str = _get(["server", "api_key"], "ONCALL_API_KEY", "")
    icm_webhook_secret: str = _get(["server", "icm_webhook_secret"], "ICM_WEBHOOK_SECRET", "")

    def validate(self) -> None:
        """Verify required fields are present. Raises ConfigError if not."""
        missing: list[str] = []
        if not self.llm_api_key:
            missing.append("llm_api_key")
        if missing:
            raise ConfigError(
                f"missing required config field(s): {', '.join(missing)}"
            )


config = Config()
