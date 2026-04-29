"""Generic MCP client — calls tools on MCP servers via SSE/HTTP."""

import httpx
from typing import Any


class MCPClient:
    """Lightweight MCP tool-call client over HTTP."""

    def __init__(self, name: str, base_url: str):
        self.name = name
        self.base_url = base_url.rstrip("/")

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] = None) -> dict:
        """Call a tool on this MCP server."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments or {},
            },
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{self.base_url}/message", json=payload)
            resp.raise_for_status()
            result = resp.json()
            if "error" in result:
                raise RuntimeError(f"MCP tool error: {result['error']}")
            return result.get("result", {})

    async def list_tools(self) -> list[dict]:
        """List available tools on this MCP server."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{self.base_url}/message", json=payload)
            resp.raise_for_status()
            result = resp.json()
            return result.get("result", {}).get("tools", [])
