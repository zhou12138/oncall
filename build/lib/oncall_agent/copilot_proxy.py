"""GitHub Copilot Proxy — Device Code OAuth + auto-refresh session token.

Usage:
    proxy = CopilotProxy()
    await proxy.login()           # device code flow (interactive)
    await proxy.ensure_token()    # auto-refresh if expired
    resp = await proxy.chat_completion(messages, model="gpt-4o", stream=False)
    async for chunk in proxy.chat_completion_stream(messages, model="gpt-4o"):
        print(chunk, end="")
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import AsyncIterator

import httpx

# ── Constants ─────────────────────────────────────────────────────────────────

GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"  # VS Code Copilot client ID
COPILOT_CHAT_URL = "https://api.individual.githubcopilot.com/chat/completions"  # default, overridden by token response
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"

CREDENTIALS_PATH = Path.home() / ".oncall" / "copilot_credentials.json"


# ── Credential Persistence ────────────────────────────────────────────────────

def _load_credentials() -> dict:
    if CREDENTIALS_PATH.exists():
        try:
            return json.loads(CREDENTIALS_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_credentials(data: dict):
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_text(json.dumps(data, indent=2))
    CREDENTIALS_PATH.chmod(0o600)


# ── CopilotProxy ──────────────────────────────────────────────────────────────

class CopilotProxy:
    """GitHub Copilot API proxy with device code auth and token refresh."""

    def __init__(self):
        creds = _load_credentials()
        self.github_token: str = creds.get("github_token", "")
        self.copilot_token: str = creds.get("copilot_token", "")
        self.copilot_expires_at: int = creds.get("copilot_expires_at", 0)
        self.chat_api_url: str = creds.get("chat_api_url", COPILOT_CHAT_URL)

    @property
    def is_logged_in(self) -> bool:
        return bool(self.github_token)

    @property
    def is_token_valid(self) -> bool:
        return bool(self.copilot_token) and time.time() < self.copilot_expires_at - 60

    def _persist(self):
        _save_credentials({
            "github_token": self.github_token,
            "copilot_token": self.copilot_token,
            "copilot_expires_at": self.copilot_expires_at,
            "chat_api_url": self.chat_api_url,
        })

    # ── Device Code Login ─────────────────────────────────────────────────

    async def login(self) -> bool:
        """Interactive device code OAuth flow. Returns True on success."""
        async with httpx.AsyncClient(timeout=30) as client:
            # Step 1: Request device code
            resp = await client.post(
                "https://github.com/login/device/code",
                headers={"Accept": "application/json"},
                data={"client_id": GITHUB_CLIENT_ID, "scope": ""},
            )
            resp.raise_for_status()
            data = resp.json()

            user_code = data["user_code"]
            device_code = data["device_code"]
            verification_uri = data["verification_uri"]
            expires_in = data.get("expires_in", 900)
            interval = data.get("interval", 5)

            print(f"\n{'='*50}")
            print(f"  🔐 GitHub Copilot Authorization")
            print(f"{'='*50}")
            print(f"  1. Open: {verification_uri}")
            print(f"  2. Enter code: {user_code}")
            print(f"{'='*50}")
            print(f"  Waiting for authorization...\n")

            # Step 2: Poll for token
            deadline = time.time() + expires_in
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
                    print(f"  ❌ Auth error: {error} — {token_data.get('error_description', '')}")
                    return False
                else:
                    access_token = token_data.get("access_token")
                    if access_token:
                        self.github_token = access_token
                        self._persist()
                        print("  ✅ GitHub authorized successfully!")
                        # Immediately get copilot token
                        await self._refresh_copilot_token()
                        return True

            print("  ❌ Authorization timed out.")
            return False

    # ── Token Refresh ─────────────────────────────────────────────────────

    async def _refresh_copilot_token(self) -> bool:
        """Exchange GitHub OAuth token for a short-lived Copilot session token."""
        if not self.github_token:
            return False

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.get(
                    COPILOT_TOKEN_URL,
                    headers={
                        "Authorization": f"token {self.github_token}",
                        "Accept": "application/json",
                        "Editor-Version": "vscode/1.95.0",
                        "Editor-Plugin-Version": "copilot/1.250.0",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self.copilot_token = data["token"]
                    self.copilot_expires_at = data["expires_at"]
                    # Use enterprise or individual endpoint from token response
                    endpoints = data.get("endpoints", {})
                    api_base = endpoints.get("api", "").rstrip("/")
                    if api_base:
                        self.chat_api_url = f"{api_base}/chat/completions"
                    self._persist()
                    return True
                elif resp.status_code == 401:
                    print("  ⚠️  GitHub token expired. Run `oncall login` to re-authenticate.")
                    self.github_token = ""
                    self._persist()
                    return False
                else:
                    print(f"  ⚠️  Copilot token refresh failed: {resp.status_code}")
                    return False
            except Exception as e:
                print(f"  ⚠️  Copilot token refresh error: {e}")
                return False

    async def ensure_token(self) -> bool:
        """Ensure we have a valid Copilot token, refreshing if needed."""
        if self.is_token_valid:
            return True
        if not self.is_logged_in:
            return False
        return await self._refresh_copilot_token()

    # ── Chat Completion (non-streaming) ───────────────────────────────────

    async def chat_completion(
        self,
        messages: list[dict],
        model: str = "gpt-4o",
        temperature: float = 0.3,
        response_format: dict = None,
    ) -> dict:
        """OpenAI-compatible chat completion via Copilot API."""
        if not await self.ensure_token():
            raise RuntimeError("Not authenticated. Run `oncall login` first.")

        headers = {
            "Authorization": f"Bearer {self.copilot_token}",
            "Content-Type": "application/json",
            "Copilot-Integration-Id": "vscode-chat",
            "Editor-Version": "vscode/1.95.0",
            "Editor-Plugin-Version": "copilot/1.250.0",
        }

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if response_format:
            payload["response_format"] = response_format

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(self.chat_api_url, headers=headers, json=payload)
            if resp.status_code == 401:
                # Token expired mid-request, retry once
                if await self._refresh_copilot_token():
                    headers["Authorization"] = f"Bearer {self.copilot_token}"
                    resp = await client.post(self.chat_api_url, headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()

    # ── Chat Completion (streaming) ───────────────────────────────────────

    async def chat_completion_stream(
        self,
        messages: list[dict],
        model: str = "gpt-4o",
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        """Streaming chat completion. Yields content chunks."""
        if not await self.ensure_token():
            raise RuntimeError("Not authenticated. Run `oncall login` first.")

        headers = {
            "Authorization": f"Bearer {self.copilot_token}",
            "Content-Type": "application/json",
            "Copilot-Integration-Id": "vscode-chat",
            "Editor-Version": "vscode/1.95.0",
            "Editor-Plugin-Version": "copilot/1.250.0",
        }

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", self.chat_api_url, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    if resp.status_code == 401:
                        raise RuntimeError("Copilot token expired. Run `oncall login`.")
                    resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        content = chunk["choices"][0].get("delta", {}).get("content", "")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue


# ── Singleton ─────────────────────────────────────────────────────────────────

_proxy_instance: CopilotProxy | None = None


def get_proxy() -> CopilotProxy:
    """Get the singleton CopilotProxy instance."""
    global _proxy_instance
    if _proxy_instance is None:
        _proxy_instance = CopilotProxy()
    return _proxy_instance
