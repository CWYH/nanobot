"""Comprehensive unit tests for the Microsoft Teams channel."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.teams.auth import (
    AuthToken,
    PasswordGrantProvider,
    TeamsAuthError,
    TeamsAuthManager,
)
from nanobot.channels.teams.graph_client import GraphAPIError, GraphClient
from nanobot.channels.teams.subscriptions import Subscription, SubscriptionManager
from nanobot.config.schema import TeamsConfig

if TYPE_CHECKING:
    from nanobot.channels.teams.channel import TeamsChannel


# ── Helpers ──


def _make_config(**overrides: object) -> TeamsConfig:
    """Factory for TeamsConfig with sensible test defaults."""
    defaults: dict = {
        "enabled": True,
        "tenant_id": "test-tenant",
        "client_id": "test-client",
        "auth_mode": "password",
        "username": "bot@test.com",
        "password": "secret",
        "webhook_host": "https://test.example.com",
        "webhook_port": 18791,
        "webhook_path": "/teams/webhook",
        "subscriptions": ["/chats/19:abc@thread.v2/messages"],
        "allow_from": ["*"],
        "max_concurrent_requests": 4,
        "retry_max_attempts": 3,
        "retry_base_delay_ms": 10,
    }
    defaults.update(overrides)
    return TeamsConfig(**defaults)


def _make_auth_token(expires_in: int = 3600) -> AuthToken:
    return AuthToken(
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        expires_at=time.time() + expires_in,
    )


def _make_expired_token() -> AuthToken:
    return AuthToken(
        access_token="expired-token",
        refresh_token="old-refresh",
        expires_at=time.time() - 100,
    )


# ── Auth: PasswordGrantProvider ──


class TestPasswordGrantProvider:
    """Tests for token acquisition via password grant."""

    @pytest.mark.asyncio
    async def test_acquire_token_password_grant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """First call uses password grant."""
        provider = PasswordGrantProvider(
            tenant_id="t1",
            client_id="c1",
            username="u@test.com",
            password="pw",
            scopes=["offline_access"],
        )
        token_resp = {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_in": 3600,
        }

        async def _mock_post(self_client: object, url: str, **kw: object) -> httpx.Response:
            return httpx.Response(200, json=token_resp)

        monkeypatch.setattr(httpx.AsyncClient, "post", _mock_post)
        token = await provider.acquire_token()
        assert token.access_token == "at-1"
        assert token.refresh_token == "rt-1"

    @pytest.mark.asyncio
    async def test_acquire_token_uses_refresh_on_second_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second call uses refresh_token grant."""
        provider = PasswordGrantProvider(
            tenant_id="t1",
            client_id="c1",
            username="u@test.com",
            password="pw",
            scopes=["offline_access"],
        )
        calls: list[dict] = []

        async def _mock_post(
            self_client: object, url: str, *, data: dict | None = None, **kw: object
        ) -> httpx.Response:
            calls.append(data or {})
            return httpx.Response(
                200,
                json={
                    "access_token": f"at-{len(calls)}",
                    "refresh_token": f"rt-{len(calls)}",
                    "expires_in": 3600,
                },
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", _mock_post)

        # First call: password grant
        await provider.acquire_token()
        assert calls[0].get("grant_type") == "password"

        # Second call: refresh grant
        await provider.acquire_token()
        assert calls[1].get("grant_type") == "refresh_token"

    @pytest.mark.asyncio
    async def test_acquire_token_fallback_on_refresh_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falls back to password grant if refresh fails."""
        provider = PasswordGrantProvider(
            tenant_id="t1",
            client_id="c1",
            username="u@test.com",
            password="pw",
            scopes=["offline_access"],
        )
        provider._refresh_token = "old-rt"
        call_count = 0

        async def _mock_post(
            self_client: object, url: str, *, data: dict | None = None, **kw: object
        ) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if data and data.get("grant_type") == "refresh_token":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": "at-fallback",
                    "refresh_token": "rt-new",
                    "expires_in": 3600,
                },
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", _mock_post)
        token = await provider.acquire_token()
        assert token.access_token == "at-fallback"
        assert call_count == 2  # refresh failed + password fallback

    @pytest.mark.asyncio
    async def test_acquire_token_raises_on_bad_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raises TeamsAuthError on 400 from password grant."""
        provider = PasswordGrantProvider(
            tenant_id="t1",
            client_id="c1",
            username="u@test.com",
            password="bad",
            scopes=["offline_access"],
        )

        async def _mock_post(self_client: object, url: str, **kw: object) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "Bad credentials",
                },
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", _mock_post)
        with pytest.raises(TeamsAuthError, match="invalid_grant"):
            await provider.acquire_token()

    @pytest.mark.asyncio
    async def test_force_refresh_skips_cached_refresh_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """force_refresh=True bypasses refresh_token and uses password grant."""
        provider = PasswordGrantProvider(
            tenant_id="t1",
            client_id="c1",
            username="u@test.com",
            password="pw",
            scopes=["offline_access"],
        )
        provider._refresh_token = "old-rt"
        calls: list[dict] = []

        async def _mock_post(
            self_client: object, url: str, *, data: dict | None = None, **kw: object
        ) -> httpx.Response:
            calls.append(data or {})
            return httpx.Response(
                200,
                json={
                    "access_token": "at-forced",
                    "refresh_token": "rt-new",
                    "expires_in": 3600,
                },
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", _mock_post)
        token = await provider.acquire_token(force_refresh=True)
        assert token.access_token == "at-forced"
        assert len(calls) == 1
        assert calls[0].get("grant_type") == "password"


# ── Auth: TeamsAuthManager ──


class TestTeamsAuthManager:
    """Tests for token caching and refresh logic."""

    @pytest.mark.asyncio
    async def test_returns_cached_token_when_valid(self) -> None:
        provider = AsyncMock()
        provider.acquire_token.return_value = _make_auth_token(expires_in=3600)
        manager = TeamsAuthManager(provider)

        token1 = await manager.get_access_token()
        token2 = await manager.get_access_token()
        assert token1 == token2
        assert provider.acquire_token.call_count == 1

    @pytest.mark.asyncio
    async def test_refreshes_when_within_margin(self) -> None:
        provider = AsyncMock()
        expired = _make_auth_token(expires_in=100)  # Within 300s margin
        fresh = _make_auth_token(expires_in=3600)
        provider.acquire_token.side_effect = [expired, fresh]
        manager = TeamsAuthManager(provider)

        # First call acquires
        await manager.get_access_token()
        # Token is within margin (100s < 300s), next call should refresh
        await manager.get_access_token()
        assert provider.acquire_token.call_count == 2

    @pytest.mark.asyncio
    async def test_invalidate_and_refresh(self) -> None:
        provider = AsyncMock()
        provider.acquire_token.return_value = _make_auth_token()
        manager = TeamsAuthManager(provider)

        await manager.get_access_token()
        await manager.invalidate_and_refresh()
        assert provider.acquire_token.call_count == 2


# ── GraphClient ──


class TestGraphClient:
    """Tests for HTTP request handling, retry, and rate limiting."""

    def _make_client(self, config: TeamsConfig | None = None) -> tuple[GraphClient, AsyncMock]:
        auth = AsyncMock(spec=TeamsAuthManager)
        auth.get_access_token.return_value = "test-token"
        auth.invalidate_and_refresh.return_value = "refreshed-token"
        cfg = config or _make_config()
        client = GraphClient(auth, cfg)
        return client, auth

    @pytest.mark.asyncio
    async def test_successful_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = self._make_client()
        resp_data = {"id": "msg-1", "content": "hello"}

        async def _mock_request(
            self_client: object, method: str, url: str, **kw: object
        ) -> httpx.Response:
            return httpx.Response(200, json=resp_data)

        monkeypatch.setattr(httpx.AsyncClient, "request", _mock_request)
        result = await client.get("https://graph.microsoft.com/v1.0/me")
        assert result == resp_data
        await client.close()

    @pytest.mark.asyncio
    async def test_retries_on_429(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = self._make_client()
        attempt_count = 0

        async def _mock_request(
            self_client: object, method: str, url: str, **kw: object
        ) -> httpx.Response:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(httpx.AsyncClient, "request", _mock_request)
        result = await client.get("https://example.com")
        assert result == {"ok": True}
        assert attempt_count == 2
        await client.close()

    @pytest.mark.asyncio
    async def test_retries_on_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = self._make_client()
        attempt_count = 0

        async def _mock_request(
            self_client: object, method: str, url: str, **kw: object
        ) -> httpx.Response:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                return httpx.Response(500)
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(httpx.AsyncClient, "request", _mock_request)
        result = await client.get("https://example.com")
        assert result == {"ok": True}
        assert attempt_count == 3
        await client.close()

    @pytest.mark.asyncio
    async def test_reauth_on_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, auth = self._make_client()
        attempt_count = 0

        async def _mock_request(
            self_client: object, method: str, url: str, **kw: object
        ) -> httpx.Response:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count == 1:
                return httpx.Response(401)
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(httpx.AsyncClient, "request", _mock_request)
        result = await client.get("https://example.com")
        assert result == {"ok": True}
        auth.invalidate_and_refresh.assert_called_once()
        await client.close()

    @pytest.mark.asyncio
    async def test_raises_graph_api_error_on_4xx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = self._make_client()

        async def _mock_request(
            self_client: object, method: str, url: str, **kw: object
        ) -> httpx.Response:
            return httpx.Response(
                403,
                json={
                    "error": {"code": "Forbidden", "message": "Access denied"},
                },
            )

        monkeypatch.setattr(httpx.AsyncClient, "request", _mock_request)
        with pytest.raises(GraphAPIError) as exc_info:
            await client.get("https://example.com")
        assert exc_info.value.status_code == 403
        assert exc_info.value.error_code == "Forbidden"
        await client.close()

    @pytest.mark.asyncio
    async def test_raises_runtime_error_after_max_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = self._make_client()

        async def _mock_request(
            self_client: object, method: str, url: str, **kw: object
        ) -> httpx.Response:
            return httpx.Response(500)

        monkeypatch.setattr(httpx.AsyncClient, "request", _mock_request)
        with pytest.raises(RuntimeError, match="failed after 3 attempts"):
            await client.get("https://example.com")
        await client.close()

    @pytest.mark.asyncio
    async def test_timeout_triggers_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = self._make_client()
        attempt_count = 0

        async def _mock_request(
            self_client: object, method: str, url: str, **kw: object
        ) -> httpx.Response:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count == 1:
                raise httpx.TimeoutException("timeout")
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(httpx.AsyncClient, "request", _mock_request)
        result = await client.get("https://example.com")
        assert result == {"ok": True}
        assert attempt_count == 2
        await client.close()

    @pytest.mark.asyncio
    async def test_post_with_json_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = self._make_client()
        captured_kwargs: dict = {}

        async def _mock_request(
            self_client: object, method: str, url: str, **kw: object
        ) -> httpx.Response:
            captured_kwargs.update(kw)
            return httpx.Response(201, json={"id": "new-1"})

        monkeypatch.setattr(httpx.AsyncClient, "request", _mock_request)
        result = await client.post("https://example.com", json={"body": "test"})
        assert result == {"id": "new-1"}
        assert captured_kwargs.get("json") == {"body": "test"}
        await client.close()

    @pytest.mark.asyncio
    async def test_204_returns_empty_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = self._make_client()

        async def _mock_request(
            self_client: object, method: str, url: str, **kw: object
        ) -> httpx.Response:
            return httpx.Response(204)

        monkeypatch.setattr(httpx.AsyncClient, "request", _mock_request)
        result = await client.delete("https://example.com/sub/1")
        assert result == {}
        await client.close()

    def test_backoff_delay(self) -> None:
        client, _ = self._make_client(_make_config(retry_base_delay_ms=1000))
        assert client._backoff_delay(1) == 1000
        assert client._backoff_delay(2) == 1500
        assert client._backoff_delay(3) == 2250
        # Verify cap at 30000
        assert client._backoff_delay(100) == 30_000


# ── SubscriptionManager ──


class TestSubscriptionManager:
    """Tests for subscription CRUD and renewal."""

    def _make_manager(self) -> tuple[SubscriptionManager, AsyncMock]:
        mock_client = AsyncMock(spec=GraphClient)
        config = _make_config()
        manager = SubscriptionManager(mock_client, config, "test-client-state")
        return manager, mock_client

    @pytest.mark.asyncio
    async def test_create_all(self) -> None:
        manager, mock_client = self._make_manager()
        mock_client.post.return_value = {"id": "sub-123"}
        await manager.create_all()
        assert len(manager._subscriptions) == 1
        assert manager._subscriptions[0].id == "sub-123"
        assert manager._subscriptions[0].resource == "/chats/19:abc@thread.v2/messages"

    @pytest.mark.asyncio
    async def test_create_all_survives_partial_failure(self) -> None:
        config = _make_config(
            subscriptions=[
                "/chats/19:a@thread.v2/messages",
                "/chats/19:b@thread.v2/messages",
            ]
        )
        mock_client = AsyncMock(spec=GraphClient)
        manager = SubscriptionManager(mock_client, config, "state")

        call_count = 0

        async def _side_effect(*args: object, **kw: object) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("network error")
            return {"id": "sub-2"}

        mock_client.post.side_effect = _side_effect
        await manager.create_all()
        assert len(manager._subscriptions) == 1
        assert manager._subscriptions[0].id == "sub-2"

    @pytest.mark.asyncio
    async def test_create_all_handles_missing_id(self) -> None:
        manager, mock_client = self._make_manager()
        mock_client.post.return_value = {}  # No "id" in response
        await manager.create_all()
        assert len(manager._subscriptions) == 0

    @pytest.mark.asyncio
    async def test_renew_subscription_success(self) -> None:
        manager, mock_client = self._make_manager()
        sub = Subscription(id="sub-1", resource="/chats/x/messages", expiry=None)
        mock_client.patch.return_value = {}
        ok = await manager._renew_subscription(sub)
        assert ok is True
        assert sub.expiry is not None
        mock_client.patch.assert_called_once()

    @pytest.mark.asyncio
    async def test_renew_subscription_failure(self) -> None:
        manager, mock_client = self._make_manager()
        sub = Subscription(id="sub-1", resource="/chats/x/messages", expiry=None)
        mock_client.patch.side_effect = RuntimeError("fail")
        ok = await manager._renew_subscription(sub)
        assert ok is False

    @pytest.mark.asyncio
    async def test_recreate_subscription(self) -> None:
        manager, mock_client = self._make_manager()
        old_sub = Subscription(id="sub-old", resource="/chats/x/messages", expiry=None)
        manager._subscriptions.append(old_sub)

        mock_client.delete.return_value = {}
        mock_client.post.return_value = {"id": "sub-new"}

        await manager._recreate_subscription(old_sub)
        assert len(manager._subscriptions) == 1
        assert manager._subscriptions[0].id == "sub-new"

    @pytest.mark.asyncio
    async def test_delete_all(self) -> None:
        manager, mock_client = self._make_manager()
        manager._subscriptions = [
            Subscription(id="s1", resource="/r1", expiry=None),
            Subscription(id="s2", resource="/r2", expiry=None),
        ]
        mock_client.delete.return_value = {}
        await manager.delete_all()
        assert len(manager._subscriptions) == 0
        assert mock_client.delete.call_count == 2

    @pytest.mark.asyncio
    async def test_delete_all_ignores_errors(self) -> None:
        manager, mock_client = self._make_manager()
        manager._subscriptions = [
            Subscription(id="s1", resource="/r1", expiry=None),
        ]
        mock_client.delete.side_effect = RuntimeError("fail")
        await manager.delete_all()
        assert len(manager._subscriptions) == 0

    def test_format_expiry(self) -> None:
        from datetime import datetime, timezone

        dt = datetime(2026, 3, 3, 12, 0, 0, tzinfo=timezone.utc)
        result = SubscriptionManager._format_expiry(dt)
        assert result == "2026-03-03T12:00:00.0000000Z"


# ── TeamsChannel: Pure Functions ──


class TestParseResource:
    """Tests for resource path parsing."""

    def _channel(self) -> "TeamsChannel":
        from nanobot.channels.teams.channel import TeamsChannel

        config = _make_config()
        return TeamsChannel(config, MessageBus())

    def test_chat_message_path(self) -> None:
        ch = self._channel()
        chat_id, msg_type, thread_id = ch._parse_resource("/chats/19:abc@thread.v2/messages/1234")
        assert chat_id == "19:abc@thread.v2"
        assert msg_type == "chat"
        assert thread_id is None

    def test_chat_messages_path(self) -> None:
        ch = self._channel()
        chat_id, msg_type, _ = ch._parse_resource("/chats/19:abc@thread.v2/messages")
        assert chat_id == "19:abc@thread.v2"
        assert msg_type == "chat"

    def test_channel_message_path(self) -> None:
        ch = self._channel()
        chat_id, msg_type, thread_id = ch._parse_resource(
            "/teams/team-1/channels/channel-1/messages/msg-1"
        )
        assert chat_id == "channel-1"
        assert msg_type == "channel"
        assert thread_id == "msg-1"

    def test_channel_reply_path(self) -> None:
        ch = self._channel()
        chat_id, msg_type, thread_id = ch._parse_resource(
            "/teams/team-1/channels/channel-1/messages/msg-1/replies/reply-1"
        )
        assert chat_id == "channel-1"
        assert msg_type == "channel"
        assert thread_id == "msg-1"

    def test_channel_messages_no_id(self) -> None:
        ch = self._channel()
        chat_id, msg_type, thread_id = ch._parse_resource(
            "/teams/team-1/channels/channel-1/messages"
        )
        assert chat_id == "channel-1"
        assert msg_type == "channel"
        assert thread_id is None

    def test_unknown_path_fallback(self) -> None:
        ch = self._channel()
        chat_id, msg_type, _ = ch._parse_resource("/some/unknown/path")
        assert msg_type == "chat"


class TestStripHtml:
    """Tests for HTML stripping."""

    def test_strips_at_mentions(self) -> None:
        from nanobot.channels.teams.channel import TeamsChannel

        result = TeamsChannel._strip_html('<at id="user-1">John</at> hello')
        assert result == "@John hello"

    def test_replaces_br_with_newlines(self) -> None:
        from nanobot.channels.teams.channel import TeamsChannel

        result = TeamsChannel._strip_html("line1<br/>line2<br>line3")
        assert result == "line1\nline2\nline3"

    def test_replaces_block_closing_tags(self) -> None:
        from nanobot.channels.teams.channel import TeamsChannel

        result = TeamsChannel._strip_html("<p>para1</p><p>para2</p>")
        assert "para1" in result
        assert "para2" in result

    def test_decodes_html_entities(self) -> None:
        from nanobot.channels.teams.channel import TeamsChannel

        result = TeamsChannel._strip_html("a &amp; b &lt; c")
        assert result == "a & b < c"

    def test_strips_attachment_tags(self) -> None:
        from nanobot.channels.teams.channel import TeamsChannel

        result = TeamsChannel._strip_html('text<attachment id="1">content</attachment>more')
        assert "content" not in result
        assert "textmore" in result

    def test_preserves_plain_text(self) -> None:
        from nanobot.channels.teams.channel import TeamsChannel

        result = TeamsChannel._strip_html("hello world")
        assert result == "hello world"

    def test_collapses_multiple_newlines(self) -> None:
        from nanobot.channels.teams.channel import TeamsChannel

        result = TeamsChannel._strip_html("<p></p><p></p><p></p><p>text</p>")
        assert "\n\n\n" not in result
        assert "text" in result


# ── TeamsChannel: Webhook Handler ──


class TestWebhookHandler:
    """Tests for the webhook HTTP handler."""

    def _make_channel(self, **config_overrides: object) -> "TeamsChannel":
        from nanobot.channels.teams.channel import TeamsChannel

        config = _make_config(**config_overrides)
        ch = TeamsChannel(config, MessageBus())
        ch._graph_client = AsyncMock(spec=GraphClient)
        ch._bot_user_id = "bot-user-id"
        return ch

    @pytest.mark.asyncio
    async def test_validation_handshake(self) -> None:
        ch = self._make_channel()
        request = MagicMock()
        request.query = {"validationToken": "my-token-123"}
        resp = await ch._handle_webhook(request)
        assert resp.status == 200
        assert resp.text == "my-token-123"

    @pytest.mark.asyncio
    async def test_invalid_client_state_ignored(self) -> None:
        ch = self._make_channel()
        request = MagicMock()
        request.query = {}
        request.json = AsyncMock(
            return_value={
                "value": [{"clientState": "wrong-state", "resource": "/chats/x/messages/1"}]
            }
        )

        resp = await ch._handle_webhook(request)
        assert resp.status == 202
        # No notification should be processed
        ch._graph_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_notification_dispatched(self) -> None:
        ch = self._make_channel()
        # Override _process_notification to track calls
        processed = []

        async def _track(notification: dict) -> None:
            processed.append(notification)

        ch._process_notification = _track

        request = MagicMock()
        request.query = {}
        request.json = AsyncMock(
            return_value={
                "value": [
                    {
                        "clientState": ch._client_state,
                        "resource": "/chats/19:abc/messages/1",
                        "changeType": "created",
                    }
                ]
            }
        )

        resp = await ch._handle_webhook(request)
        assert resp.status == 202
        # Allow the create_task to run
        await asyncio.sleep(0.05)
        assert len(processed) == 1

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self) -> None:
        ch = self._make_channel()
        request = MagicMock()
        request.query = {}
        request.json = AsyncMock(side_effect=ValueError("bad json"))
        resp = await ch._handle_webhook(request)
        assert resp.status == 400


# ── TeamsChannel: Notification Processing ──


class TestProcessNotification:
    """Tests for notification processing."""

    def _make_channel(self) -> "TeamsChannel":
        from nanobot.channels.teams.channel import TeamsChannel

        config = _make_config()
        ch = TeamsChannel(config, MessageBus())
        ch._graph_client = AsyncMock(spec=GraphClient)
        ch._bot_user_id = "bot-user-id"
        return ch

    @pytest.mark.asyncio
    async def test_process_fetches_and_forwards(self) -> None:
        ch = self._make_channel()
        ch._graph_client.get.return_value = {
            "id": "msg-1",
            "from": {"user": {"id": "user-1", "displayName": "Alice"}},
            "body": {"contentType": "text", "content": "hello bot"},
        }
        ch._handle_message = AsyncMock()

        await ch._process_notification(
            {
                "resource": "/chats/19:abc@thread.v2/messages/msg-1",
                "changeType": "created",
            }
        )

        ch._handle_message.assert_called_once()
        call_kwargs = ch._handle_message.call_args
        assert call_kwargs.kwargs["content"] == "hello bot"
        assert call_kwargs.kwargs["sender_id"] == "user-1"

    @pytest.mark.asyncio
    async def test_skips_non_created_change_type(self) -> None:
        ch = self._make_channel()
        ch._handle_message = AsyncMock()

        await ch._process_notification(
            {
                "resource": "/chats/19:abc/messages/1",
                "changeType": "updated",
            }
        )

        ch._graph_client.get.assert_not_called()
        ch._handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_bot_own_messages(self) -> None:
        ch = self._make_channel()
        ch._graph_client.get.return_value = {
            "id": "msg-2",
            "from": {"user": {"id": "bot-user-id", "displayName": "Bot"}},
            "body": {"contentType": "text", "content": "I said this"},
        }
        ch._handle_message = AsyncMock()

        await ch._process_notification(
            {
                "resource": "/chats/19:abc/messages/msg-2",
                "changeType": "created",
            }
        )

        ch._handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_skips_repeated_message(self) -> None:
        ch = self._make_channel()
        ch._graph_client.get.return_value = {
            "id": "msg-dup",
            "from": {"user": {"id": "user-1", "displayName": "Alice"}},
            "body": {"contentType": "text", "content": "hello"},
        }
        ch._handle_message = AsyncMock()

        notification = {
            "resource": "/chats/19:abc/messages/msg-dup",
            "changeType": "created",
        }
        await ch._process_notification(notification)
        await ch._process_notification(notification)

        assert ch._handle_message.call_count == 1

    @pytest.mark.asyncio
    async def test_html_content_stripped(self) -> None:
        ch = self._make_channel()
        ch._graph_client.get.return_value = {
            "id": "msg-html",
            "from": {"user": {"id": "user-1", "displayName": "Alice"}},
            "body": {
                "contentType": "html",
                "content": "<p>Hello <b>world</b></p>",
            },
        }
        ch._handle_message = AsyncMock()

        await ch._process_notification(
            {
                "resource": "/chats/19:abc/messages/msg-html",
                "changeType": "created",
            }
        )

        call_kwargs = ch._handle_message.call_args.kwargs
        assert "<p>" not in call_kwargs["content"]
        assert "Hello" in call_kwargs["content"]
        assert "world" in call_kwargs["content"]

    @pytest.mark.asyncio
    async def test_channel_message_thread_scoped_session(self) -> None:
        ch = self._make_channel()
        ch._graph_client.get.return_value = {
            "id": "msg-ch",
            "from": {"user": {"id": "user-1", "displayName": "Alice"}},
            "body": {"contentType": "text", "content": "channel message"},
        }
        ch._handle_message = AsyncMock()
        ch.config.group_policy = "open"

        await ch._process_notification(
            {
                "resource": "/teams/t1/channels/ch-1/messages/thread-1",
                "changeType": "created",
            }
        )

        call_kwargs = ch._handle_message.call_args.kwargs
        assert call_kwargs["session_key"] == "teams:ch-1:thread-1"
        meta = call_kwargs["metadata"]["teams"]
        assert meta["message_type"] == "channel"
        assert meta["thread_id"] == "thread-1"

    @pytest.mark.asyncio
    async def test_process_notification_handles_error_gracefully(self) -> None:
        ch = self._make_channel()
        ch._graph_client.get.side_effect = RuntimeError("network error")
        # Should not raise
        await ch._process_notification(
            {
                "resource": "/chats/19:abc/messages/1",
                "changeType": "created",
            }
        )


# ── TeamsChannel: Send ──


class TestSend:
    """Tests for outbound message sending."""

    def _make_channel(self) -> "TeamsChannel":
        from nanobot.channels.teams.channel import TeamsChannel

        config = _make_config()
        ch = TeamsChannel(config, MessageBus())
        ch._graph_client = AsyncMock(spec=GraphClient)
        return ch

    @pytest.mark.asyncio
    async def test_send_chat_message(self) -> None:
        ch = self._make_channel()
        ch._graph_client.post.return_value = {}
        msg = OutboundMessage(
            channel="teams",
            chat_id="19:abc@thread.v2",
            content="Hello!",
            metadata={"teams": {"message_type": "chat"}},
        )

        await ch.send(msg)

        ch._graph_client.post.assert_called_once()
        call_args = ch._graph_client.post.call_args
        assert "/chats/19:abc@thread.v2/messages" in call_args.args[0]
        assert call_args.kwargs["json"]["body"]["content"] == "Hello!"

    @pytest.mark.asyncio
    async def test_send_channel_reply_in_thread(self) -> None:
        ch = self._make_channel()
        ch._graph_client.post.return_value = {}
        ch.config.reply_in_thread = True
        msg = OutboundMessage(
            channel="teams",
            chat_id="ch-1",
            content="Reply!",
            metadata={
                "teams": {
                    "message_type": "channel",
                    "thread_id": "thread-1",
                    "resource": "teams/t1/channels/ch-1/messages",
                }
            },
        )

        await ch.send(msg)

        url = ch._graph_client.post.call_args.args[0]
        assert "thread-1/replies" in url

    @pytest.mark.asyncio
    async def test_send_channel_no_thread(self) -> None:
        ch = self._make_channel()
        ch._graph_client.post.return_value = {}
        ch.config.reply_in_thread = False
        msg = OutboundMessage(
            channel="teams",
            chat_id="ch-1",
            content="New message",
            metadata={
                "teams": {
                    "message_type": "channel",
                    "resource": "teams/t1/channels/ch-1/messages",
                }
            },
        )

        await ch.send(msg)

        url = ch._graph_client.post.call_args.args[0]
        assert "replies" not in url

    @pytest.mark.asyncio
    async def test_send_handles_error(self) -> None:
        ch = self._make_channel()
        ch._graph_client.post.side_effect = RuntimeError("send error")
        msg = OutboundMessage(
            channel="teams",
            chat_id="19:abc",
            content="will fail",
            metadata={"teams": {"message_type": "chat"}},
        )
        # Should not raise
        await ch.send(msg)

    @pytest.mark.asyncio
    async def test_send_default_message_type_is_chat(self) -> None:
        ch = self._make_channel()
        ch._graph_client.post.return_value = {}
        msg = OutboundMessage(
            channel="teams",
            chat_id="19:abc",
            content="hi",
            metadata={},
        )

        await ch.send(msg)

        url = ch._graph_client.post.call_args.args[0]
        assert "/chats/" in url


# ── TeamsChannel: Group Policy ──


class TestGroupPolicy:
    """Tests for group message policy."""

    def _make_channel(self, **kw: object) -> "TeamsChannel":
        from nanobot.channels.teams.channel import TeamsChannel

        config = _make_config(**kw)
        ch = TeamsChannel(config, MessageBus())
        ch._bot_user_id = "bot-id"
        ch._bot_display_name = "NanoBot"
        return ch

    def test_open_policy_allows_all(self) -> None:
        ch = self._make_channel(group_policy="open")
        assert ch._is_group_allowed("u1", "ch-1", "channel", "hello") is True

    def test_mention_policy_with_at_tag(self) -> None:
        ch = self._make_channel(group_policy="mention")
        content = '<at id="bot-id">NanoBot</at> help me'
        assert ch._is_group_allowed("u1", "ch-1", "channel", content) is True

    def test_mention_policy_with_display_name(self) -> None:
        ch = self._make_channel(group_policy="mention")
        content = "@NanoBot help me"
        assert ch._is_group_allowed("u1", "ch-1", "channel", content) is True

    def test_mention_policy_without_mention(self) -> None:
        ch = self._make_channel(group_policy="mention")
        assert ch._is_group_allowed("u1", "ch-1", "channel", "hello everyone") is False

    def test_mention_policy_no_bot_identity(self) -> None:
        """When bot identity is unknown, mention policy allows all."""
        ch = self._make_channel(group_policy="mention")
        ch._bot_user_id = None
        ch._bot_display_name = None
        assert ch._is_group_allowed("u1", "ch-1", "channel", "hello") is True

    def test_unknown_policy_denies(self) -> None:
        ch = self._make_channel(group_policy="nonexistent")
        assert ch._is_group_allowed("u1", "ch-1", "channel", "hello") is False

    def test_allowlist_policy_allowed_channel(self) -> None:
        ch = self._make_channel(group_policy="allowlist", group_allow_from=["ch-1"])
        assert ch._is_group_allowed("u1", "ch-1", "channel", "hi") is True

    def test_allowlist_policy_blocked_channel(self) -> None:
        ch = self._make_channel(group_policy="allowlist", group_allow_from=["ch-other"])
        assert ch._is_group_allowed("u1", "ch-1", "channel", "hi") is False

    def test_chat_messages_always_allowed(self) -> None:
        ch = self._make_channel(group_policy="allowlist")
        assert ch._is_group_allowed("u1", "chat-1", "chat", "hi") is True


# ── TeamsChannel: Lifecycle ──


class TestChannelLifecycle:
    """Tests for start/stop edge cases."""

    def _make_channel(self, **kw: object) -> "TeamsChannel":
        from nanobot.channels.teams.channel import TeamsChannel

        config = _make_config(**kw)
        return TeamsChannel(config, MessageBus())

    @pytest.mark.asyncio
    async def test_start_returns_early_without_tenant_id(self) -> None:
        ch = self._make_channel(tenant_id="")
        await ch.start()
        assert ch.is_running is False

    @pytest.mark.asyncio
    async def test_start_returns_early_without_webhook_host(self) -> None:
        ch = self._make_channel(webhook_host="")
        await ch.start()
        assert ch.is_running is False

    @pytest.mark.asyncio
    async def test_start_returns_early_without_subscriptions(self) -> None:
        ch = self._make_channel(subscriptions=[])
        await ch.start()
        assert ch.is_running is False

    def test_build_auth_provider_password(self) -> None:
        ch = self._make_channel(auth_mode="password")
        provider = ch._build_auth_provider()
        assert isinstance(provider, PasswordGrantProvider)

    def test_build_auth_provider_unsupported(self) -> None:
        from nanobot.channels.teams.channel import TeamsChannel

        config = _make_config(auth_mode="unknown")
        with pytest.raises(ValueError, match="Unsupported"):
            TeamsChannel(config, MessageBus())

    def test_trim_processed_ids(self) -> None:
        ch = self._make_channel()
        ch._MAX_PROCESSED_IDS = 3
        ch._processed_ids = {"a", "b", "c", "d"}
        ch._trim_processed_ids()
        assert len(ch._processed_ids) == 0


# ── Config: TeamsConfig ──


class TestTeamsConfig:
    """Tests for the TeamsConfig model."""

    def test_default_config(self) -> None:
        config = TeamsConfig()
        assert config.enabled is False
        assert config.auth_mode == "password"
        assert config.webhook_port == 18791
        assert config.max_concurrent_requests == 4

    def test_camel_case_alias(self) -> None:
        config = TeamsConfig(**{"tenantId": "t1", "clientId": "c1"})
        assert config.tenant_id == "t1"
        assert config.client_id == "c1"

    def test_snake_case_works(self) -> None:
        config = TeamsConfig(tenant_id="t1", client_id="c1")
        assert config.tenant_id == "t1"

    def test_default_scopes(self) -> None:
        config = TeamsConfig()
        assert "offline_access" in config.delegated_scopes
        assert any("Chat.ReadWrite" in s for s in config.delegated_scopes)


# ── Additional Coverage Tests ──


class TestStartStop:
    """Tests for start() and stop() full lifecycle."""

    def _make_channel(self, **kw: object) -> "TeamsChannel":
        from nanobot.channels.teams.channel import TeamsChannel

        config = _make_config(**kw)
        ch = TeamsChannel(config, MessageBus())
        ch._auth_manager = AsyncMock()
        ch._graph_client = AsyncMock(spec=GraphClient)
        ch._subscription_manager = AsyncMock(spec=SubscriptionManager)
        return ch

    @pytest.mark.asyncio
    async def test_start_full_lifecycle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests start() through the full path: webhook server, identity, subscriptions."""
        from aiohttp import web

        ch = self._make_channel()

        # Mock the aiohttp web server
        mock_runner = AsyncMock()
        mock_runner.setup = AsyncMock()
        mock_runner.cleanup = AsyncMock()
        monkeypatch.setattr(web, "AppRunner", lambda app: mock_runner)

        mock_site = AsyncMock()
        mock_site.start = AsyncMock()
        monkeypatch.setattr(web, "TCPSite", lambda runner, host, port: mock_site)

        # Mock identity resolution
        ch._graph_client.get.return_value = {"id": "bot-1", "displayName": "Bot"}

        # Make subscription renewal_loop exit immediately
        ch._subscription_manager.renewal_loop = AsyncMock()

        # Stop the channel after a short delay
        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            ch._running = False

        asyncio.create_task(_stop_soon())
        await ch.start()

        assert ch._bot_user_id == "bot-1"
        assert ch._bot_display_name == "Bot"
        ch._subscription_manager.create_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_webhook_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests start() when webhook server fails to start."""
        from aiohttp import web

        ch = self._make_channel()

        mock_runner = AsyncMock()
        mock_runner.setup = AsyncMock(side_effect=OSError("port in use"))
        monkeypatch.setattr(web, "AppRunner", lambda app: mock_runner)

        await ch.start()
        assert ch.is_running is False

    @pytest.mark.asyncio
    async def test_stop_full_cleanup(self) -> None:
        """Tests stop() cleans up all resources."""
        ch = self._make_channel()
        ch._running = True
        ch._renewal_task = asyncio.create_task(asyncio.sleep(999))
        ch._runner = AsyncMock()
        ch._runner.cleanup = AsyncMock()

        await ch.stop()

        assert ch._running is False
        assert ch._renewal_task is None
        assert ch._runner is None
        ch._subscription_manager.delete_all.assert_called_once()
        ch._graph_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_without_runner_or_task(self) -> None:
        """Tests stop() when no runner/task was created."""
        ch = self._make_channel()
        ch._running = True
        ch._renewal_task = None
        ch._runner = None

        await ch.stop()

        assert ch._running is False
        ch._subscription_manager.delete_all.assert_called_once()
        ch._graph_client.close.assert_called_once()


class TestResolveBotIdentity:
    """Tests for _resolve_bot_identity."""

    def _make_channel(self) -> "TeamsChannel":
        from nanobot.channels.teams.channel import TeamsChannel

        config = _make_config()
        ch = TeamsChannel(config, MessageBus())
        ch._graph_client = AsyncMock(spec=GraphClient)
        return ch

    @pytest.mark.asyncio
    async def test_resolve_bot_identity_success(self) -> None:
        ch = self._make_channel()
        ch._graph_client.get.return_value = {"id": "bot-123", "displayName": "TestBot"}
        await ch._resolve_bot_identity()
        assert ch._bot_user_id == "bot-123"
        assert ch._bot_display_name == "TestBot"

    @pytest.mark.asyncio
    async def test_resolve_bot_identity_failure(self) -> None:
        ch = self._make_channel()
        ch._graph_client.get.side_effect = RuntimeError("network error")
        await ch._resolve_bot_identity()
        assert ch._bot_user_id is None
        assert ch._bot_display_name is None


class TestGraphClientAdditional:
    """Additional GraphClient tests for coverage."""

    def _make_client(self, config: TeamsConfig | None = None) -> tuple[GraphClient, AsyncMock]:
        auth = AsyncMock(spec=TeamsAuthManager)
        auth.get_access_token.return_value = "test-token"
        auth.invalidate_and_refresh.return_value = "refreshed-token"
        cfg = config or _make_config()
        client = GraphClient(auth, cfg)
        return client, auth

    @pytest.mark.asyncio
    async def test_401_when_reauth_raises_teams_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When 401 re-auth raises TeamsAuthError, it propagates."""
        client, auth = self._make_client()
        auth.invalidate_and_refresh.side_effect = TeamsAuthError("auth failed")

        async def _mock_request(
            self_client: object, method: str, url: str, **kw: object
        ) -> httpx.Response:
            return httpx.Response(401)

        monkeypatch.setattr(httpx.AsyncClient, "request", _mock_request)
        with pytest.raises(TeamsAuthError, match="auth failed"):
            await client.get("https://example.com")
        await client.close()

    @pytest.mark.asyncio
    async def test_patch_convenience(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test the patch convenience method."""
        client, _ = self._make_client()

        async def _mock_request(
            self_client: object, method: str, url: str, **kw: object
        ) -> httpx.Response:
            assert method == "PATCH"
            return httpx.Response(200, json={"updated": True})

        monkeypatch.setattr(httpx.AsyncClient, "request", _mock_request)
        result = await client.patch("https://example.com/sub/1", json={"key": "val"})
        assert result == {"updated": True}
        await client.close()


class TestAuthAdditional:
    """Additional auth tests for coverage."""

    @pytest.mark.asyncio
    async def test_refresh_grant_updates_stored_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that successful refresh_grant updates the provider's stored refresh_token."""
        provider = PasswordGrantProvider(
            tenant_id="t1",
            client_id="c1",
            username="u@test.com",
            password="pw",
            scopes=["offline_access"],
        )
        provider._refresh_token = "old-rt"
        calls: list[dict] = []

        async def _mock_post(
            self_client: object, url: str, *, data: dict | None = None, **kw: object
        ) -> httpx.Response:
            calls.append(data or {})
            resp = httpx.Response(
                200,
                json={
                    "access_token": f"at-{len(calls)}",
                    "refresh_token": f"rt-new-{len(calls)}",
                    "expires_in": 3600,
                },
                request=httpx.Request("POST", url),
            )
            return resp

        monkeypatch.setattr(httpx.AsyncClient, "post", _mock_post)
        token = await provider.acquire_token(force_refresh=False)
        # Should have used refresh grant
        assert len(calls) == 1
        assert calls[0].get("grant_type") == "refresh_token"
        assert token.access_token == "at-1"
        # The stored refresh_token should be updated
        assert provider._refresh_token == "rt-new-1"

    @pytest.mark.asyncio
    async def test_refresh_grant_preserves_old_if_no_new(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If refresh response has no refresh_token, keep the old one."""
        provider = PasswordGrantProvider(
            tenant_id="t1",
            client_id="c1",
            username="u@test.com",
            password="pw",
            scopes=["offline_access"],
        )
        provider._refresh_token = "old-rt"

        async def _mock_post(
            self_client: object, url: str, *, data: dict | None = None, **kw: object
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "access_token": "at-no-refresh",
                    # No refresh_token in response
                    "expires_in": 3600,
                },
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", _mock_post)
        token = await provider.acquire_token(force_refresh=False)
        assert token.access_token == "at-no-refresh"
        # Should preserve old refresh token
        assert provider._refresh_token == "old-rt"

    @pytest.mark.asyncio
    async def test_username_without_at_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Username without @ should not crash masking."""
        provider = PasswordGrantProvider(
            tenant_id="t1",
            client_id="c1",
            username="noatsymbol",
            password="pw",
            scopes=["offline_access"],
        )

        async def _mock_post(self_client: object, url: str, **kw: object) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "expires_in": 3600,
                },
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", _mock_post)
        token = await provider.acquire_token()
        assert token.access_token == "at-1"


class TestSubscriptionAdditional:
    """Additional subscription tests for coverage."""

    def _make_manager(self) -> tuple[SubscriptionManager, AsyncMock]:
        mock_client = AsyncMock(spec=GraphClient)
        config = _make_config()
        manager = SubscriptionManager(mock_client, config, "test-client-state")
        return manager, mock_client

    @pytest.mark.asyncio
    async def test_renewal_loop_renew_and_recreate(self) -> None:
        """Test that renewal_loop renews, and recreates on failure."""
        manager, mock_client = self._make_manager()
        sub = Subscription(id="sub-1", resource="/chats/x/messages", expiry=None)
        manager._subscriptions.append(sub)

        # First renewal call fails, triggering recreate
        mock_client.patch.side_effect = RuntimeError("renew failed")
        mock_client.delete.return_value = {}
        mock_client.post.return_value = {"id": "sub-new"}

        # Override RENEWAL_INTERVAL_S to 0 for immediate test execution
        original_interval = SubscriptionManager.RENEWAL_INTERVAL_S
        SubscriptionManager.RENEWAL_INTERVAL_S = 0

        try:
            # Run one iteration of the loop then cancel
            loop_task = asyncio.create_task(manager.renewal_loop())
            await asyncio.sleep(0.05)
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
        finally:
            SubscriptionManager.RENEWAL_INTERVAL_S = original_interval

        # Should have attempted renew, then recreate
        mock_client.patch.assert_called()
        mock_client.post.assert_called()

    @pytest.mark.asyncio
    async def test_recreate_subscription_when_delete_fails(self) -> None:
        """Test recreate when the old subscription delete fails."""
        manager, mock_client = self._make_manager()
        old_sub = Subscription(id="sub-old", resource="/chats/x/messages", expiry=None)
        manager._subscriptions.append(old_sub)

        mock_client.delete.side_effect = RuntimeError("delete failed")
        mock_client.post.return_value = {"id": "sub-new"}

        await manager._recreate_subscription(old_sub)
        # Old sub should be removed, new one added
        assert len(manager._subscriptions) == 1
        assert manager._subscriptions[0].id == "sub-new"

    @pytest.mark.asyncio
    async def test_recreate_subscription_when_create_fails(self) -> None:
        """Test recreate when re-creation fails."""
        manager, mock_client = self._make_manager()
        old_sub = Subscription(id="sub-old", resource="/chats/x/messages", expiry=None)
        manager._subscriptions.append(old_sub)

        mock_client.delete.return_value = {}
        mock_client.post.side_effect = RuntimeError("create failed")

        await manager._recreate_subscription(old_sub)
        # Old sub removed, no new sub added
        assert len(manager._subscriptions) == 0


class TestProcessNotificationGroupPolicy:
    """Tests for group policy filtering in notification processing."""

    def _make_channel(self) -> "TeamsChannel":
        from nanobot.channels.teams.channel import TeamsChannel

        config = _make_config(group_policy="mention")
        ch = TeamsChannel(config, MessageBus())
        ch._graph_client = AsyncMock(spec=GraphClient)
        ch._bot_user_id = "bot-user-id"
        ch._bot_display_name = "NanoBot"
        return ch

    @pytest.mark.asyncio
    async def test_channel_message_blocked_by_group_policy(self) -> None:
        ch = self._make_channel()
        ch._graph_client.get.return_value = {
            "id": "msg-blocked",
            "from": {"user": {"id": "user-1", "displayName": "Alice"}},
            "body": {"contentType": "text", "content": "hello everyone"},
        }
        ch._handle_message = AsyncMock()

        await ch._process_notification(
            {
                "resource": "/teams/t1/channels/ch-1/messages/thread-1",
                "changeType": "created",
            }
        )

        ch._handle_message.assert_not_called()
