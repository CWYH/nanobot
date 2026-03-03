"""HTTP client wrapper for Microsoft Graph API with auth, rate limiting, and retry."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from loguru import logger

from nanobot.channels.teams.auth import TeamsAuthError, TeamsAuthManager
from nanobot.config.schema import TeamsConfig

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


class GraphAPIError(Exception):
    """Non-retriable Graph API error (4xx other than 401/429)."""

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(f"Graph API {status_code} [{error_code}]: {message}")


class GraphClient:
    """HTTP client wrapper with auth injection, semaphore rate limiting, and retry."""

    def __init__(self, auth_manager: TeamsAuthManager, config: TeamsConfig) -> None:
        self._auth = auth_manager
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        self._retry_max = config.retry_max_attempts
        self._retry_base_delay_ms = config.retry_base_delay_ms
        self._client = httpx.AsyncClient(timeout=30.0)

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute an authenticated Graph API request with retry."""
        token = await self._auth.get_access_token()
        auth_retried = False

        for attempt in range(1, self._retry_max + 1):
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }

            async with self._semaphore:
                try:
                    resp = await self._client.request(
                        method,
                        url,
                        headers=headers,
                        json=json,
                        params=params,
                    )
                except httpx.TimeoutException:
                    delay = self._backoff_delay(attempt)
                    logger.warning(
                        "Teams API timeout, attempt {}/{}, backoff {}ms",
                        attempt,
                        self._retry_max,
                        delay,
                    )
                    await asyncio.sleep(delay / 1000)
                    continue

            if resp.status_code in (200, 201, 202, 204):
                return resp.json() if resp.content else {}

            if resp.status_code == 401 and not auth_retried:
                auth_retried = True
                try:
                    token = await self._auth.invalidate_and_refresh()
                except TeamsAuthError:
                    raise
                continue

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "60"))
                logger.warning("Teams API throttled, retry after {}s", retry_after)
                await asyncio.sleep(retry_after)
                continue

            if resp.status_code >= 500:
                delay = self._backoff_delay(attempt)
                logger.warning(
                    "Teams API {}, attempt {}/{}, backoff {}ms",
                    resp.status_code,
                    attempt,
                    self._retry_max,
                    delay,
                )
                await asyncio.sleep(delay / 1000)
                continue

            # 4xx (non-429, non-401): non-retriable
            error_body = resp.json() if resp.content else {}
            error_info = error_body.get("error", {})
            raise GraphAPIError(
                resp.status_code,
                error_info.get("code", "Unknown"),
                error_info.get("message", ""),
            )

        raise RuntimeError(f"Teams API request failed after {self._retry_max} attempts: {url}")

    def _backoff_delay(self, attempt: int) -> int:
        """Exponential backoff: base * 1.5^(attempt-1), capped at 30s."""
        delay = self._retry_base_delay_ms * (1.5 ** (attempt - 1))
        return min(int(delay), 30_000)

    async def get(self, url: str, **kw: Any) -> dict[str, Any]:
        """Convenience: GET request."""
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> dict[str, Any]:
        """Convenience: POST request."""
        return await self.request("POST", url, **kw)

    async def patch(self, url: str, **kw: Any) -> dict[str, Any]:
        """Convenience: PATCH request."""
        return await self.request("PATCH", url, **kw)

    async def delete(self, url: str, **kw: Any) -> dict[str, Any]:
        """Convenience: DELETE request."""
        return await self.request("DELETE", url, **kw)

    async def close(self) -> None:
        """Close the underlying httpx.AsyncClient."""
        await self._client.aclose()
