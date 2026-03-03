"""Delegated user token authentication for Microsoft Graph API."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

import httpx
from loguru import logger


class TeamsAuthError(Exception):
    """Raised when token acquisition fails permanently."""


@dataclass
class AuthToken:
    """Cached delegated token with expiry tracking."""

    access_token: str
    refresh_token: str | None
    expires_at: float  # time.time()-based epoch seconds


class DelegatedTokenProvider(Protocol):
    """Protocol for acquiring delegated Graph tokens."""

    async def acquire_token(self, force_refresh: bool = False) -> AuthToken: ...


class PasswordGrantProvider:
    """Username/password (ROPC) token acquisition for non-MFA service accounts."""

    TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        username: str,
        password: str,
        scopes: list[str],
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._username = username
        self._password = password
        self._scopes = scopes
        self._refresh_token: str | None = None

    async def acquire_token(self, force_refresh: bool = False) -> AuthToken:
        """Acquire token via refresh grant (if available) or password grant."""
        if not force_refresh and self._refresh_token:
            try:
                token = await self._refresh_grant(self._refresh_token)
                self._refresh_token = token.refresh_token or self._refresh_token
                return token
            except Exception:
                logger.warning("Teams: refresh grant failed, falling back to password grant")

        token = await self._password_grant()
        self._refresh_token = token.refresh_token
        return token

    async def _password_grant(self) -> AuthToken:
        """Execute password grant request."""
        url = self.TOKEN_URL.format(tenant_id=self._tenant_id)
        data = {
            "grant_type": "password",
            "client_id": self._client_id,
            "username": self._username,
            "password": self._password,
            "scope": " ".join(self._scopes),
        }
        at_idx = self._username.find("@")
        if at_idx >= 0:
            masked = self._username[:2] + "***" + self._username[at_idx:]
        else:
            masked = self._username[:2] + "***"
        logger.debug("Teams: acquiring token via password grant for {}", masked)

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data=data)

        if resp.status_code != 200:
            body = resp.json() if resp.content else {}
            error = body.get("error", "unknown")
            desc = body.get("error_description", "")
            raise TeamsAuthError(f"Password grant failed: {error} — {desc}")

        return self._parse_token_response(resp.json())

    async def _refresh_grant(self, refresh_token: str) -> AuthToken:
        """Execute refresh_token grant request."""
        url = self.TOKEN_URL.format(tenant_id=self._tenant_id)
        data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "refresh_token": refresh_token,
            "scope": " ".join(self._scopes),
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data=data)
            resp.raise_for_status()

        return self._parse_token_response(resp.json())

    @staticmethod
    def _parse_token_response(data: dict) -> AuthToken:
        """Parse token endpoint JSON into AuthToken."""
        return AuthToken(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=time.time() + data.get("expires_in", 3600),
        )


class TeamsAuthManager:
    """Provider-agnostic token cache/refresh facade used by GraphClient."""

    REFRESH_MARGIN_S: int = 300  # Refresh 5 min before expiry

    def __init__(self, provider: DelegatedTokenProvider) -> None:
        self._provider = provider
        self._token: AuthToken | None = None
        self._lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        async with self._lock:
            if self._token and time.time() < self._token.expires_at - self.REFRESH_MARGIN_S:
                return self._token.access_token
            self._token = await self._provider.acquire_token(force_refresh=False)
            return self._token.access_token

    async def invalidate_and_refresh(self) -> str:
        """Force-invalidate cached token and acquire a new one."""
        async with self._lock:
            self._token = await self._provider.acquire_token(force_refresh=True)
            return self._token.access_token
