"""Microsoft Teams channel using Graph API change notifications."""

from __future__ import annotations

import asyncio
import html
import re
import secrets
from typing import Any

from aiohttp import web
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.teams.auth import (
    PasswordGrantProvider,
    TeamsAuthManager,
)
from nanobot.channels.teams.graph_client import GRAPH_BASE_URL, GraphClient
from nanobot.channels.teams.subscriptions import SubscriptionManager
from nanobot.config.schema import TeamsConfig

# Regex patterns for resource path extraction
_CHANNEL_MSG_RE = re.compile(
    r"/teams/(?P<team_id>[^/]+)/channels/(?P<channel_id>[^/]+)"
    r"/messages(?:/(?P<msg_id>[^/]+))?(?:/replies(?:/[^/]+)?)?$"
)
_CHAT_MSG_RE = re.compile(r"/chats/(?P<chat_id>[^/]+)/messages(?:/(?P<msg_id>[^/]+))?(?:/.*)?$")


class TeamsChannel(BaseChannel):
    """Microsoft Teams channel via Graph API change notifications."""

    name: str = "teams"

    def __init__(self, config: TeamsConfig, bus: MessageBus) -> None:
        super().__init__(config, bus)
        self.config: TeamsConfig = config

        # Unique secret verified on incoming webhooks
        self._client_state: str = secrets.token_urlsafe(32)

        # Auth stack
        self._auth_provider = self._build_auth_provider()
        self._auth_manager = TeamsAuthManager(self._auth_provider)
        self._graph_client = GraphClient(self._auth_manager, config)
        self._subscription_manager = SubscriptionManager(
            self._graph_client,
            config,
            self._client_state,
        )

        # aiohttp webhook server state
        self._runner: web.AppRunner | None = None
        self._renewal_task: asyncio.Task | None = None

        # Bot identity (resolved on start)
        self._bot_user_id: str | None = None
        self._bot_display_name: str | None = None

        # Dedup cache: set of message IDs recently processed
        self._processed_ids: set[str] = set()
        self._MAX_PROCESSED_IDS: int = 10_000

    def _build_auth_provider(self) -> PasswordGrantProvider:
        """Construct the DelegatedTokenProvider based on config.auth_mode."""
        if self.config.auth_mode == "password":
            return PasswordGrantProvider(
                tenant_id=self.config.tenant_id,
                client_id=self.config.client_id,
                username=self.config.username,
                password=self.config.password,
                scopes=self.config.delegated_scopes,
            )
        raise ValueError(f"Unsupported Teams auth_mode: {self.config.auth_mode}")

    async def start(self) -> None:
        """Start webhook server, authenticate, create subscriptions, run renewal loop."""
        if not self.config.tenant_id or not self.config.client_id:
            logger.error("Teams: tenant_id and client_id are required")
            return
        if not self.config.webhook_host:
            logger.error("Teams: webhook_host is required")
            return
        if not self.config.subscriptions:
            logger.error("Teams: at least one subscription resource is required")
            return

        self._running = True

        # 1. Start webhook HTTP server
        try:
            app = web.Application()
            app.router.add_post(self.config.webhook_path, self._handle_webhook)
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, "0.0.0.0", self.config.webhook_port)
            await site.start()
            logger.info("Teams webhook listening on port {}", self.config.webhook_port)
        except Exception as e:
            logger.error("Teams: failed to start webhook server: {}", e)
            self._running = False
            return

        # 2. Resolve bot identity
        await self._resolve_bot_identity()

        # 3. Create subscriptions
        try:
            await self._subscription_manager.create_all()
        except Exception as e:
            logger.error("Teams: subscription creation failed: {}", e)

        # 4. Run renewal loop
        self._renewal_task = asyncio.create_task(self._subscription_manager.renewal_loop())

        # Block until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop all resources."""
        self._running = False

        if self._renewal_task:
            self._renewal_task.cancel()
            try:
                await self._renewal_task
            except asyncio.CancelledError:
                pass
            self._renewal_task = None

        await self._subscription_manager.delete_all()

        if self._runner:
            await self._runner.cleanup()
            self._runner = None

        await self._graph_client.close()
        logger.info("Teams channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message to Teams (chat or channel)."""
        teams_meta = msg.metadata.get("teams", {})
        msg_type = teams_meta.get("message_type", "chat")
        thread_id = teams_meta.get("thread_id")
        resource = teams_meta.get("resource", "")

        body: dict[str, Any] = {
            "body": {"contentType": "text", "content": msg.content},
        }

        try:
            if msg_type == "channel" and resource:
                if self.config.reply_in_thread and thread_id:
                    url = f"{GRAPH_BASE_URL}/{resource}/{thread_id}/replies"
                else:
                    url = f"{GRAPH_BASE_URL}/{resource}"
                await self._graph_client.post(url, json=body)
            else:
                url = f"{GRAPH_BASE_URL}/chats/{msg.chat_id}/messages"
                await self._graph_client.post(url, json=body)
        except Exception as e:
            logger.error("Teams: failed to send message: {}", e)

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Handle incoming Graph API webhook notifications."""
        # Subscription validation handshake
        validation_token = request.query.get("validationToken")
        if validation_token:
            return web.Response(text=validation_token, content_type="text/plain")

        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400)

        for notification in payload.get("value", []):
            if notification.get("clientState") != self._client_state:
                logger.warning("Teams webhook: invalid clientState, ignoring")
                continue
            asyncio.create_task(self._process_notification(notification))

        return web.Response(status=202)

    async def _process_notification(self, notification: dict[str, Any]) -> None:
        """Process a single Graph API change notification."""
        try:
            resource = notification.get("resource", "")
            change_type = notification.get("changeType", "")

            if change_type != "created":
                return

            # Fetch full message content
            message = await self._graph_client.get(f"{GRAPH_BASE_URL}/{resource}")

            # Dedup
            msg_id = message.get("id", "")
            if msg_id in self._processed_ids:
                return
            self._processed_ids.add(msg_id)
            self._trim_processed_ids()

            # Skip bot's own messages
            from_user = message.get("from", {}).get("user", {})
            sender_id = from_user.get("id", "")
            if self._bot_user_id and sender_id == self._bot_user_id:
                return

            sender_name = from_user.get("displayName", "")
            body = message.get("body", {})
            raw_content = body.get("content", "")

            if body.get("contentType") == "html":
                content = self._strip_html(raw_content)
            else:
                content = raw_content

            chat_id, msg_type, thread_id = self._parse_resource(resource)

            # Group policy check for channel messages (uses raw HTML for mention detection)
            if msg_type == "channel" and not self._is_group_allowed(
                sender_id, chat_id, msg_type, raw_content
            ):
                return

            # Thread-scoped session key for channel messages
            session_key = None
            if msg_type == "channel" and thread_id:
                session_key = f"teams:{chat_id}:{thread_id}"

            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                metadata={
                    "teams": {
                        "message_id": msg_id,
                        "message_type": msg_type,
                        "thread_id": thread_id,
                        "sender_name": sender_name,
                        "resource": resource,
                    }
                },
                session_key=session_key,
            )
        except Exception:
            logger.exception("Teams: error processing notification")

    def _parse_resource(self, resource: str) -> tuple[str, str, str | None]:
        """Parse Graph API resource path to extract routing info."""
        m = _CHANNEL_MSG_RE.search(resource)
        if m:
            return (m.group("channel_id"), "channel", m.group("msg_id"))

        m = _CHAT_MSG_RE.search(resource)
        if m:
            return (m.group("chat_id"), "chat", None)

        logger.warning("Teams: unable to parse resource path: {}", resource)
        return (resource, "chat", None)

    @staticmethod
    def _strip_html(html_content: str) -> str:
        """Strip HTML tags from Graph API message body content."""
        text = re.sub(r"<at[^>]*>(.*?)</at>", r"@\1", html_content)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</(?:p|div|li)>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(
            r"<attachment[^>]*>.*?</attachment>",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    async def _resolve_bot_identity(self) -> None:
        """Resolve the authenticated user's identity via GET /me."""
        try:
            me = await self._graph_client.get(f"{GRAPH_BASE_URL}/me")
            self._bot_user_id = me.get("id")
            self._bot_display_name = me.get("displayName")
            masked = self._bot_display_name or "unknown"
            logger.info("Teams: authenticated as {} ({})", masked, self._bot_user_id)
        except Exception as e:
            logger.warning("Teams: could not resolve bot identity: {}", e)

    def _is_group_allowed(
        self, sender_id: str, chat_id: str, message_type: str, content: str
    ) -> bool:
        """Apply group policy for channel messages.

        The content parameter should be the raw (pre-stripped) HTML body so that
        ``<at id="...">`` tags can be matched for the mention policy.
        """
        if message_type != "channel":
            return True
        if self.config.group_policy == "open":
            return True
        if self.config.group_policy == "mention":
            if self._bot_user_id:
                if f'<at id="{self._bot_user_id}"' in content:
                    return True
            if self._bot_display_name:
                if f"@{self._bot_display_name}" in content:
                    return True
            # If bot identity is unknown, allow to avoid silent message loss
            if not self._bot_user_id and not self._bot_display_name:
                return True
            return False
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return False

    def _trim_processed_ids(self) -> None:
        """Trim dedup cache when it exceeds the max."""
        if len(self._processed_ids) > self._MAX_PROCESSED_IDS:
            self._processed_ids.clear()
