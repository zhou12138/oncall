"""Configuration for OnCall Agent.

Priority: ~/.oncall/config.json > environment variables > defaults
"""

import json
import os
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Optional

CONFIG_PATH = Path.home() / ".oncall" / "config.json"
ENV_PATH = Path.home() / ".oncall" / ".env"


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
    """Resolve config value: file > env > default."""
    # Walk file config dict
    val = _file_cfg
    for key in file_path:
        if isinstance(val, dict):
            val = val.get(key)
        else:
            val = None
            break
    if val is not None and not isinstance(val, dict):
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
    llm_api_key: str = _get(["llm", "_token_env"], "GITHUB_TOKEN", "")  # resolved from .env
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


# Fix: resolve api_key from the env var name stored in config
def _resolve_api_key() -> str:
    token_env = _file_cfg.get("llm", {}).get("_token_env", "GITHUB_TOKEN")
    return os.getenv(token_env, "")


config = Config()
config.llm_api_key = _resolve_api_key()
