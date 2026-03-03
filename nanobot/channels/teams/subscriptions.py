"""Graph API subscription lifecycle management."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger

from nanobot.channels.teams.graph_client import GRAPH_BASE_URL, GraphClient
from nanobot.config.schema import TeamsConfig

SUBSCRIPTIONS_URL = f"{GRAPH_BASE_URL}/subscriptions"


@dataclass
class Subscription:
    """Tracks a single Graph API subscription."""

    id: str
    resource: str
    expiry: datetime


class SubscriptionManager:
    """Manages Graph API change notification subscriptions."""

    RENEWAL_INTERVAL_S: int = 50 * 60  # 50 minutes
    SUBSCRIPTION_TTL_MIN: int = 55  # 55 minutes

    def __init__(
        self,
        graph_client: GraphClient,
        config: TeamsConfig,
        client_state: str,
    ) -> None:
        self._graph_client = graph_client
        self._config = config
        self._client_state = client_state
        self._subscriptions: list[Subscription] = []

    async def create_all(self) -> None:
        """Create subscriptions for all configured resources."""
        notification_url = f"{self._config.webhook_host}{self._config.webhook_path}"

        for resource in self._config.subscriptions:
            try:
                sub = await self._create_subscription(resource, notification_url)
                if sub:
                    self._subscriptions.append(sub)
            except Exception as e:
                logger.error("Teams: failed to create subscription for {}: {}", resource, e)

    async def _create_subscription(
        self, resource: str, notification_url: str
    ) -> Subscription | None:
        """Create a single subscription."""
        expiry = self._make_expiry()
        body = {
            "changeType": "created",
            "notificationUrl": notification_url,
            "resource": resource,
            "expirationDateTime": self._format_expiry(expiry),
            "clientState": self._client_state,
            "includeResourceData": False,
        }

        resp = await self._graph_client.post(SUBSCRIPTIONS_URL, json=body)
        sub_id = resp.get("id")
        if not sub_id:
            logger.warning("Teams: subscription response missing id for {}", resource)
            return None

        logger.info("Teams: subscription created: {} -> {}", resource, sub_id)
        return Subscription(id=sub_id, resource=resource, expiry=expiry)

    async def renewal_loop(self) -> None:
        """Periodically renew all subscriptions."""
        while True:
            await asyncio.sleep(self.RENEWAL_INTERVAL_S)
            for sub in list(self._subscriptions):
                ok = await self._renew_subscription(sub)
                if not ok:
                    await self._recreate_subscription(sub)

    async def _renew_subscription(self, sub: Subscription) -> bool:
        """Renew a single subscription. Returns True on success."""
        try:
            expiry = self._make_expiry()
            await self._graph_client.patch(
                f"{SUBSCRIPTIONS_URL}/{sub.id}",
                json={"expirationDateTime": self._format_expiry(expiry)},
            )
            sub.expiry = expiry
            logger.debug("Teams: subscription renewed: {}", sub.id)
            return True
        except Exception as e:
            logger.error("Teams: failed to renew subscription {}: {}", sub.id, e)
            return False

    async def _recreate_subscription(self, sub: Subscription) -> None:
        """Delete a failed subscription and re-create it."""
        # Remove old
        try:
            await self._graph_client.delete(f"{SUBSCRIPTIONS_URL}/{sub.id}")
        except Exception:
            pass
        self._subscriptions = [s for s in self._subscriptions if s.id != sub.id]

        # Re-create
        notification_url = f"{self._config.webhook_host}{self._config.webhook_path}"
        try:
            new_sub = await self._create_subscription(sub.resource, notification_url)
            if new_sub:
                self._subscriptions.append(new_sub)
                logger.info("Teams: subscription re-created for {}", sub.resource)
        except Exception as e:
            logger.error("Teams: failed to re-create subscription for {}: {}", sub.resource, e)

    async def delete_all(self) -> None:
        """Delete all subscriptions (best-effort cleanup)."""
        for sub in self._subscriptions:
            try:
                await self._graph_client.delete(f"{SUBSCRIPTIONS_URL}/{sub.id}")
                logger.debug("Teams: subscription deleted: {}", sub.id)
            except Exception:
                pass
        self._subscriptions.clear()

    def _make_expiry(self) -> datetime:
        """Compute next subscription expiration datetime."""
        return datetime.now(timezone.utc) + timedelta(minutes=self.SUBSCRIPTION_TTL_MIN)

    @staticmethod
    def _format_expiry(dt: datetime) -> str:
        """Format datetime as ISO 8601 for Graph API."""
        return dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
