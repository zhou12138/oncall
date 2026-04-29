"""Generic MCP client — calls tools on MCP servers via HTTP JSON-RPC.

Phase 1 hardening (M1.4):

* Reused, pooled :class:`httpx.AsyncClient` per MCPClient instance
  (instead of spinning up a client per call).
* :meth:`initialize` performs the JSON-RPC ``initialize`` handshake
  before any tool call. ``call_tool`` lazily auto-initializes if
  callers skip it.
* Exponential backoff retry (3 attempts, 1s/2s/4s) for transient
  failures: timeouts, connection errors, and HTTP 5xx. Non-transient
  4xx and JSON-RPC application errors fail fast.
* Configurable per-call timeout (default 120s).
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any, Optional

import httpx

from oncall_agent.errors import MCPError
from oncall_agent.logging_config import get_logger

logger = get_logger(__name__)

# Backoff schedule in seconds — three retries on top of the initial attempt.
_RETRY_BACKOFF_S: tuple[float, ...] = (1.0, 2.0, 4.0)
_DEFAULT_TIMEOUT_S = 120.0
_MCP_PROTOCOL_VERSION = "2024-11-05"


class MCPClient:
    """Lightweight MCP tool-call client over HTTP JSON-RPC."""

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT_S,
        client_info: Optional[dict[str, str]] = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.client_info = client_info or {
            "name": "oncall-agent",
            "version": "0.1.0",
        }
        self._http: Optional[httpx.AsyncClient] = None
        self._init_lock = asyncio.Lock()
        self._initialized = False
        # Monotonically increasing JSON-RPC ids
        self._id_counter = itertools.count(1)

    # ── lifecycle ────────────────────────────────────────────────────────

    def _http_client(self) -> httpx.AsyncClient:
        """Lazily create the pooled httpx client."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                ),
            )
        return self._http

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None
        self._initialized = False

    async def __aenter__(self) -> "MCPClient":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # ── handshake ────────────────────────────────────────────────────────

    async def initialize(self) -> dict:
        """Perform the JSON-RPC ``initialize`` handshake (idempotent)."""
        if self._initialized:
            return {}
        async with self._init_lock:
            if self._initialized:
                return {}
            params = {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": self.client_info,
            }
            result = await self._rpc("initialize", params, _skip_init=True)
            self._initialized = True
            logger.info(
                "mcp.initialized",
                extra={
                    "event": "mcp.initialized",
                    "server": self.name,
                    "protocol_version": result.get("protocolVersion"),
                },
            )
            return result

    # ── tool API ─────────────────────────────────────────────────────────

    async def call_tool(
        self,
        tool_name: str,
        arguments: Optional[dict[str, Any]] = None,
    ) -> dict:
        """Call a tool on this MCP server."""
        return await self._rpc(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
        )

    async def list_tools(self) -> list[dict]:
        """List available tools on this MCP server."""
        result = await self._rpc("tools/list", {})
        return result.get("tools", []) if isinstance(result, dict) else []

    # ── transport ────────────────────────────────────────────────────────

    async def _rpc(
        self,
        method: str,
        params: dict,
        *,
        _skip_init: bool = False,
    ) -> dict:
        """Issue a JSON-RPC call with exponential backoff on transient errors."""
        if not _skip_init and not self._initialized:
            await self.initialize()

        payload = {
            "jsonrpc": "2.0",
            "id": next(self._id_counter),
            "method": method,
            "params": params,
        }
        url = f"{self.base_url}/message"
        client = self._http_client()

        last_exc: Optional[Exception] = None
        attempts = 1 + len(_RETRY_BACKOFF_S)
        for attempt in range(1, attempts + 1):
            try:
                resp = await client.post(url, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                if attempt < attempts:
                    delay = _RETRY_BACKOFF_S[attempt - 1]
                    logger.warning(
                        "mcp.transport_retry",
                        extra={
                            "event": "mcp.transport_retry",
                            "server": self.name,
                            "method": method,
                            "attempt": attempt,
                            "delay_s": delay,
                            "error": f"{type(e).__name__}: {e}",
                        },
                    )
                    await asyncio.sleep(delay)
                    continue
                raise MCPError(
                    f"{self.name}.{method}: transport failure after {attempts}"
                    f" attempts: {e}"
                ) from e

            # Retry on 5xx; fail fast on 4xx.
            if 500 <= resp.status_code < 600 and attempt < attempts:
                delay = _RETRY_BACKOFF_S[attempt - 1]
                logger.warning(
                    "mcp.http_retry",
                    extra={
                        "event": "mcp.http_retry",
                        "server": self.name,
                        "method": method,
                        "attempt": attempt,
                        "delay_s": delay,
                        "status_code": resp.status_code,
                    },
                )
                await asyncio.sleep(delay)
                continue

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise MCPError(
                    f"{self.name}.{method}: HTTP {resp.status_code}: {e}"
                ) from e

            try:
                envelope = resp.json()
            except ValueError as e:
                raise MCPError(
                    f"{self.name}.{method}: invalid JSON response: {e}"
                ) from e

            if "error" in envelope:
                # JSON-RPC application error — non-transient.
                raise MCPError(
                    f"{self.name}.{method}: {envelope['error']}"
                )
            return envelope.get("result", {})

        # Defensive — loop should always raise or return.
        raise MCPError(
            f"{self.name}.{method}: exhausted retries ({last_exc!r})"
        )
