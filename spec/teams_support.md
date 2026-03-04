# Design Spec: Microsoft Teams Channel via Graph API

## 1. Overview

Add a `TeamsChannel` to nanobot that enables bidirectional messaging with Microsoft Teams users
through the Microsoft Graph API. The channel supports both **1:1 chats** and **team channel
conversations**, following the same `BaseChannel` contract used by all other nanobot channels.

### Scope

| In Scope | Out of Scope |
|----------|-------------|
| 1:1 chat messages (receive & send) | Bot Framework integration |
| Team channel messages (receive & send) | Back-in-time message migration |
| Group chat messages (receive & send) | File/attachment uploads |
| User-delegated Graph authentication (token provider abstraction) | Adaptive Cards / rich card rendering |
| Configurable inbound mode (`webhook` / `polling`) | `/teams/getAllMessages` tenant-wide subscriptions |
| Subscription lifecycle management | `/teams/getAllMessages` tenant-wide subscriptions |
| Rate limiting & retry | Cross-tenant on-behalf-of token exchange |
| Username/password token bootstrap (non-MFA tenants) | OAuth token storage in external secret vault |
| `allowFrom` access control | End-to-end encryption of notification payloads |

### Reference: DataIngestion Project

The existing `DataIngestion` project at
`C:\repos\COEP\COEPEMP\sources\dev\EvalsetManagementPlatform\DataIngestion` uses Graph API for
bulk Teams data ingestion. Key patterns borrowed from it:

- **GraphAPIClient.cs** — Partition-based rate limiting (`asyncio.Semaphore` per partition key),
  exponential backoff retry (growth factor 1.5, max 30s), credential caching with 2-hour TTL.
- **ChatProvider.cs** — Chat creation (`POST /chats`), message sending
  (`POST /chats/{chatId}/messages`), member management, ODataError handling, and permission
  error auto-recovery (auto-add bot to chat on `InsufficientPrivileges`).

These patterns inform the retry, rate limiting, and error handling designs below but adapted for
Python async/`httpx`.

---

## 2. Architecture

### 2.1 Transport Model

Teams inbound supports two modes:

1. **`webhook` mode (default)** — Graph change notifications + webhook callback.
2. **`polling` mode** — periodic Graph message polling for configured resources.

`webhook` mode has lower receive latency and lower steady-state API usage, but requires a public
HTTPS callback endpoint. `polling` mode does not require any public endpoint and is suitable for
local/private deployments.

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Microsoft Graph API                          │
└──────────┬──────────────────────────────────────┬───────────────────┘
           │  POST /subscriptions                 │  POST webhook
           │  PATCH /subscriptions/{id}           │  (change notification)
           │  POST /chats/{id}/messages           │
           │  POST /teams/{id}/channels/{id}/messages │
           ▲                                      ▼
┌──────────┴──────────────────────────────────────┴───────────────────┐
│                     TeamsChannel (nanobot)                           │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐    │
│  │ AuthManager   │  │ Subscription │  │ WebhookServer          │    │
│  │ (Delegated    │  │ Manager      │  │ (aiohttp lightweight)  │    │
│  │  User Token)  │  │              │  │                        │    │
│  │               │  │ (create /    │  │                        │    │
│  │ token cache   │  │  renew /     │  │ • validation token     │    │
│  │ auto-refresh  │  │  delete)     │  │ • clientState verify   │    │
│  └──────┬───────┘  └──────┬───────┘  │ • fetch message detail │    │
│         │                 │          │ • → _handle_message()   │    │
│         ▼                 ▼          └────────────┬───────────┘    │
│  ┌──────────────────────────────────┐             │                │
│  │  GraphClient (httpx.AsyncClient) │             │                │
│  │  • rate limiter (Semaphore)      │             ▼                │
│  │  • retry w/ exponential backoff  │     ┌──────────────┐        │
│  │  • 429/5xx handling              │     │  MessageBus   │        │
│  └──────────────────────────────────┘     └──────────────┘        │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 Inbound (Receiving Messages)

`inbound_mode=webhook`:

1. On `start()`, create Graph API **change notification subscriptions** for configured resources.
2. Graph API pushes notifications to the webhook endpoint.
3. Webhook handler fetches full message content via a separate GET call.
4. Message is forwarded to the `MessageBus` via `_handle_message()`.

`inbound_mode=polling`:

1. On `start()`, initialize per-resource cursors/checkpoints.
2. Periodically call Graph message list endpoints for each configured resource.
3. Sort/filter by timestamp + message ID, skip already-seen/self messages.
4. Forward new messages to `MessageBus` via `_handle_message()`.

### 2.3 Outbound (Sending Messages)

1. `send()` receives an `OutboundMessage` from the bus dispatcher.
2. Determines the target type (chat vs channel) from `metadata`.
3. Calls the appropriate Graph API endpoint to post the message.

---

## 3. Configuration

### 3.1 Config Model — `TeamsConfig` in `config/schema.py`

```python
class TeamsConfig(Base):
    """Microsoft Teams channel via Graph API."""

    enabled: bool = False

    # Azure AD App Registration (delegated user auth)
    tenant_id: str = ""          # Azure AD tenant ID
    client_id: str = ""          # Application (client) ID
    client_secret: str = ""      # Optional: used by future interactive flows

    # Auth mode (initial + extensible)
    auth_mode: str = "password"  # "password", "device_code", "fic", "token"
    username: str = ""           # Service account UPN/email for delegated auth
    password: str = ""           # Service account password (initial bootstrap)
    graph_token: str = ""        # Pre-acquired Graph API access token (auth_mode="token")
    delegated_scopes: list[str] = Field(
        default_factory=lambda: [
            "offline_access",
            "openid",
            "profile",
            "https://graph.microsoft.com/Chat.ReadWrite",
            "https://graph.microsoft.com/ChannelMessage.Read.All",
            "https://graph.microsoft.com/ChannelMessage.Send",
        ]
    )

    # Inbound mode
    inbound_mode: str = "webhook"  # "webhook" | "polling"

    # Webhook server (only used when inbound_mode="webhook")
    webhook_host: str = ""       # Required in webhook mode: public HTTPS base URL
    webhook_port: int = 18791    # Local HTTP server listen port
    webhook_path: str = "/teams/webhook"

    # Polling mode
    poll_interval_seconds: int = 10   # Effective only when inbound_mode="polling"
    poll_batch_size: int = 20         # Per-resource max messages fetched per cycle
    poll_lookback_minutes: int = 30   # Startup lookback window to initialize cursor

    # Subscription targets — at least one required
    # Chat subscriptions: "/chats/{chat-id}/messages" or "/users/{user-id}/chats/getAllMessages"
    # Channel subscriptions: "/teams/{team-id}/channels/{channel-id}/messages"
    subscriptions: list[str] = Field(default_factory=list)

    # Access control
    allow_from: list[str] = Field(default_factory=list)  # Azure AD user IDs or UPNs

    # Behavior
    reply_in_thread: bool = True  # Reply to channel messages in thread
    group_policy: str = "mention" # "mention", "open", "allowlist"
    group_allow_from: list[str] = Field(default_factory=list)

    # Rate limiting
    max_concurrent_requests: int = 4
    retry_max_attempts: int = 5
    retry_base_delay_ms: int = 1000
```

### 3.2 Example User Configuration (`~/.nanobot/config.json`)

```jsonc
{
  "channels": {
    "teams": {
      "enabled": true,
      "tenantId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "clientId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            "authMode": "password",
            "username": "teams-bot@contoso.com",
            "password": "your-service-account-password",
            "delegatedScopes": [
                "offline_access",
                "openid",
                "profile",
                "https://graph.microsoft.com/Chat.ReadWrite",
                "https://graph.microsoft.com/ChannelMessage.Read.All",
                "https://graph.microsoft.com/ChannelMessage.Send"
            ],
            "mfaProvider": "",
            "inboundMode": "webhook",
      "webhookHost": "https://bot.example.com",
      "webhookPort": 18791,
      "subscriptions": [
        "/chats/19:meeting_abc123@thread.v2/messages",
        "/teams/fbe2bf47-xxxx/channels/19:4a95f7d8@thread.tacv2/messages"
      ],
      "allowFrom": ["*"]
    }
  }
}
```

Polling-mode local/private deployment example:

```jsonc
{
    "channels": {
        "teams": {
            "enabled": true,
            "authMode": "token",
            "graphToken": "<graph-access-token>",
            "inboundMode": "polling",
            "pollIntervalSeconds": 10,
            "pollBatchSize": 20,
            "pollLookbackMinutes": 30,
            "subscriptions": [
                "/chats/<chat-id>/messages"
            ],
            "allowFrom": ["*"]
        }
    }
}
```

### 3.3 Registration in `ChannelsConfig`

```python
class ChannelsConfig(Base):
    # ... existing channels ...
    teams: TeamsConfig = Field(default_factory=TeamsConfig)
```

---

## 4. Azure AD App Setup (Prerequisites)

The user must register an Azure AD application with the following:

### 4.1 Required Delegated Permissions

| Permission | Purpose |
|------------|---------|
| `Chat.Read` / `Chat.ReadWrite` | Receive and send chat messages |
| `ChannelMessage.Read.All` | Subscribe to channel message notifications |
| `ChannelMessage.Send` | Send messages to team channels |
| `User.Read.All` | Resolve user display names (optional) |

Most permissions require **admin consent** in enterprise tenants.

### 4.2 Licensing Note

Tenant-level subscriptions (`/chats/getAllMessages`, `/teams/getAllMessages`) are **metered APIs**
with [licensing and payment requirements](https://learn.microsoft.com/en-us/graph/teams-licenses).
The design uses **per-resource subscriptions** (specific chat/channel IDs) by default to avoid
this cost.

### 4.3 Service Account Requirements

The delegated token is issued for a real Entra user identity (service account). That account must:

- Be licensed for Teams usage in the target tenant.
- Be added to chats/channels where send/receive is required.
- Satisfy tenant Conditional Access requirements.

---

## 5. Authentication

### 5.1 Design Goals

- Use **delegated user token** for all Graph operations (chat + channel send/receive).
- Support **username/password bootstrap now** for non-MFA tenants.
- Provide a stable abstraction so MFA-capable flows can be added without touching channel logic.

### 5.2 Auth Abstractions

```python
class DelegatedTokenProvider(Protocol):
    """Acquire/refresh delegated Graph tokens for a Teams user identity."""

    async def acquire_token(self, force_refresh: bool = False) -> AuthToken:
        ...


@dataclass
class AuthToken:
    access_token: str
    refresh_token: str | None
    expires_at: float


class PasswordGrantProvider:
    """Initial provider: username/password bootstrap (ROPC-like)."""

    TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    def __init__(self, tenant_id: str, client_id: str, username: str, password: str, scopes: list[str]):
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._username = username
        self._password = password
        self._scopes = scopes

    async def acquire_token(self, force_refresh: bool = False) -> AuthToken:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.TOKEN_URL.format(tenant_id=self._tenant_id),
                data={
                    "grant_type": "password",
                    "client_id": self._client_id,
                    "username": self._username,
                    "password": self._password,
                    "scope": " ".join(self._scopes),
                },
            )
            resp.raise_for_status()
            data = resp.json()

        return AuthToken(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=time.time() + data.get("expires_in", 3600),
        )


class TeamsAuthManager:
    """Provider-agnostic token cache/refresh facade used by GraphClient."""

    def __init__(self, provider: DelegatedTokenProvider):
        self._provider = provider
        self._token: AuthToken | None = None
        self._lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._token.expires_at - 300:
                return self._token.access_token
            self._token = await self._provider.acquire_token(force_refresh=False)
            return self._token.access_token
```

### 5.3 MFA-Ready Extension Interface

`TeamsChannel` constructs provider by `auth_mode`:

- `password` (initial): `PasswordGrantProvider` — username/password ROPC flow
- `token` (implemented): `StaticTokenProvider` — pre-acquired Graph API access token
- `device_code` (future): `DeviceCodeProvider`
- `fic` (future): `FicProvider`

Future MFA support plugs in at provider layer only:

- `DeviceCodeProvider` exposes `begin()` callback to emit verification URI + user code.
- Optional `InteractiveChallengeHandler` interface forwards challenge prompts to CLI/channel.
- `TeamsAuthManager` and `GraphClient` remain unchanged.

### 5.4 Token Lifecycle

- Cache token in memory; refresh 5 minutes before expiry.
- If `refresh_token` exists, prefer refresh grant; fallback to full re-acquire.
- On `401/invalid_grant`, invalidate cache and reacquire once, then surface error.
- Never log tokens/password; mask username in logs.

### 5.5 Security Notes

- `username/password` mode is for controlled service-account environments.
- Recommend moving to `device_code` (MFA) or `fic` (federated identity) for stricter CA tenants.
- Keep credentials in local config only for MVP; production should use secret manager/env injection.

---

## 6. Inbound Modes

### 6.1 Webhook Mode (`inbound_mode="webhook"`)

A lightweight `aiohttp` server started inside `TeamsChannel.start()`:

```python
async def start(self) -> None:
    if not self.config.tenant_id or not self.config.client_id:
        logger.error("Teams: tenant_id and client_id are required")
        return
    if not self.config.subscriptions:
        logger.error("Teams: at least one subscription resource is required")
        return

    self._running = True

    # Resolve bot identity (needed for self-message filtering in both modes)
    await self._resolve_bot_identity()

    if self.config.inbound_mode == "webhook":
        if not self.config.webhook_host:
            logger.error("Teams: webhook_host is required for webhook mode")
            self._running = False
            return

        # 1. Start webhook HTTP server
        app = web.Application()
        app.router.add_post(self.config.webhook_path, self._handle_webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.config.webhook_port)
        await site.start()
        logger.info("Teams webhook listening on port {}", self.config.webhook_port)

        # 2. Create subscriptions
        await self._subscription_manager.create_all()

        # 3. Run renewal loop
        self._renewal_task = asyncio.create_task(self._subscription_manager.renewal_loop())
    else:
        # Polling mode — no webhook server or subscriptions needed
        self._init_cursors()
        self._polling_task = asyncio.create_task(self._polling_loop())
        logger.info("Teams polling mode started (interval={}s)", self.config.poll_interval_seconds)

    while self._running:
        await asyncio.sleep(1)
```

#### Webhook Handler

```python
async def _handle_webhook(self, request: web.Request) -> web.Response:
    # Subscription validation handshake
    validation_token = request.query.get("validationToken")
    if validation_token:
        return web.Response(text=validation_token, content_type="text/plain")

    body = await request.json()

    for notification in body.get("value", []):
        # Verify clientState to prevent spoofed notifications
        if notification.get("clientState") != self._client_state:
            logger.warning("Teams webhook: invalid clientState, ignoring")
            continue

        # Process asynchronously to return 202 quickly
        asyncio.create_task(self._process_notification(notification))

    return web.Response(status=202)
```

### 6.2 Webhook Notification Processing

```python
async def _process_notification(self, notification: dict) -> None:
    resource = notification.get("resource", "")
    change_type = notification.get("changeType", "")

    if change_type != "created":
        return  # Only process new messages

    # Fetch full message content via GET
    message = await self._graph_client.get(
        f"https://graph.microsoft.com/v1.0/{resource}"
    )

    # Skip messages from the bot itself
    from_user = message.get("from", {}).get("user", {})
    if from_user.get("id") == self._bot_app_id:
        return

    sender_id = from_user.get("id", "")
    sender_name = from_user.get("displayName", "")
    body = message.get("body", {})
    content = body.get("content", "")

    # Strip HTML if contentType is "html"
    if body.get("contentType") == "html":
        content = self._strip_html(content)

    # Determine chat_id and message_type from resource path
    chat_id, msg_type, thread_id = self._parse_resource(resource)

    # Session key: for channel messages, scope by thread
    session_key = None
    if msg_type == "channel" and thread_id:
        session_key = f"teams:{chat_id}:{thread_id}"

    await self._handle_message(
        sender_id=sender_id,
        chat_id=chat_id,
        content=content,
        metadata={
            "teams": {
                "message_id": message.get("id"),
                "message_type": msg_type,       # "chat" or "channel"
                "thread_id": thread_id,         # for channel reply threading
                "sender_name": sender_name,
                "resource": resource,
            }
        },
        session_key=session_key,
    )
```

### 6.3 Subscription Manager

Manages the full lifecycle of Graph API subscriptions.

```python
class SubscriptionManager:
    RENEWAL_INTERVAL_S = 50 * 60   # Renew every 50 minutes
    SUBSCRIPTION_TTL_MIN = 55      # Subscription validity: 55 minutes

    def __init__(self, graph_client, config, client_state):
        self._graph_client = graph_client
        self._config = config
        self._client_state = client_state
        self._subscription_ids: list[str] = []

    async def create_all(self) -> None:
        """Create subscriptions for all configured resources."""
        notification_url = f"{self._config.webhook_host}{self._config.webhook_path}"

        for resource in self._config.subscriptions:
            expiry = (datetime.utcnow() + timedelta(minutes=self.SUBSCRIPTION_TTL_MIN))
            body = {
                "changeType": "created",
                "notificationUrl": notification_url,
                "resource": resource,
                "expirationDateTime": expiry.isoformat() + "Z",
                "clientState": self._client_state,
                "includeResourceData": False,
            }

            resp = await self._graph_client.post(
                "https://graph.microsoft.com/v1.0/subscriptions",
                json=body,
            )
            sub_id = resp.get("id")
            self._subscription_ids.append(sub_id)
            logger.info("Teams subscription created: {} -> {}", resource, sub_id)

    async def renewal_loop(self) -> None:
        """Periodically renew all subscriptions."""
        while True:
            await asyncio.sleep(self.RENEWAL_INTERVAL_S)
            for sub_id in self._subscription_ids:
                try:
                    expiry = (datetime.utcnow() + timedelta(minutes=self.SUBSCRIPTION_TTL_MIN))
                    await self._graph_client.patch(
                        f"https://graph.microsoft.com/v1.0/subscriptions/{sub_id}",
                        json={"expirationDateTime": expiry.isoformat() + "Z"},
                    )
                    logger.debug("Teams subscription renewed: {}", sub_id)
                except Exception as e:
                    logger.error("Failed to renew subscription {}: {}", sub_id, e)
                    # Re-create on failure
                    await self._recreate_subscription(sub_id)

    async def delete_all(self) -> None:
        """Delete all subscriptions on shutdown."""
        for sub_id in self._subscription_ids:
            try:
                await self._graph_client.delete(
                    f"https://graph.microsoft.com/v1.0/subscriptions/{sub_id}"
                )
            except Exception:
                pass
```

**Key design decisions:**

- **`includeResourceData: false`** — Avoids the need for encryption certificates. The handler
  makes a separate GET call to fetch message content. This adds one round-trip per notification
  but dramatically simplifies the implementation.
- **55-minute TTL with 50-minute renewal** — Subscriptions expire after at most 60 minutes (Graph
  API limit for `expirationDateTime > 1 hour` requires `lifecycleNotificationUrl`). Renewing at
  50 minutes gives a 5-minute safety buffer.
- **Re-create on failure** — If renewal fails (e.g., subscription was deleted server-side), the
  manager re-creates it from scratch.

### 6.4 Polling Mode (`inbound_mode="polling"`)

Polling mode loops over configured subscription resources and queries recent messages directly.
No public HTTPS endpoint or Graph subscriptions are required.

#### 6.4.1 Polling Loop

```python
async def _polling_loop(self) -> None:
    interval = max(3, int(self.config.poll_interval_seconds))

    while self._running:
        for resource in self.config.subscriptions:
            try:
                messages = await self._list_messages(resource, top=self.config.poll_batch_size)
                for message in self._filter_new_messages(resource, messages):
                    await self._process_message(resource, message)
            except Exception as e:
                logger.error("Teams polling failed for {}: {}", resource, e)

        await asyncio.sleep(interval)
```

#### 6.4.2 Message List API (`_list_messages`)

The `subscriptions` config holds resource paths like `/chats/{chatId}/messages` or
`/teams/{teamId}/channels/{channelId}/messages`. These paths are reused directly as GET
endpoints with query parameters for filtering and ordering:

```python
async def _list_messages(self, resource: str, top: int = 20) -> list[dict]:
    """Fetch recent messages for a resource path."""
    cursor_ts = self._resource_cursors.get(resource)
    url = f"{GRAPH_BASE_URL}/{resource}"
    params: dict[str, str] = {
        "$top": str(top),
        "$orderby": "createdDateTime desc",
    }
    if cursor_ts:
        params["$filter"] = f"createdDateTime gt {cursor_ts}"

    data = await self._graph_client.get(url, params=params)
    return data.get("value", [])
```

Graph API endpoints used:
- **Chat messages**: `GET /v1.0/chats/{chatId}/messages?$top=N&$orderby=createdDateTime desc`
- **Channel messages**: `GET /v1.0/teams/{teamId}/channels/{channelId}/messages?$top=N&$orderby=createdDateTime desc`

#### 6.4.3 Cursor & Checkpoint

Per-resource cursors track the last-seen message timestamp to avoid re-fetching old messages:

```python
# In __init__:
self._resource_cursors: dict[str, str] = {}  # resource -> ISO 8601 timestamp

# On startup (before first poll):
def _init_cursors(self) -> None:
    lookback = datetime.now(timezone.utc) - timedelta(minutes=self.config.poll_lookback_minutes)
    initial_ts = lookback.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for resource in self.config.subscriptions:
        self._resource_cursors[resource] = initial_ts
```

After processing messages from a poll cycle, advance the cursor to the most recent
`createdDateTime`:

```python
def _advance_cursor(self, resource: str, messages: list[dict]) -> None:
    if not messages:
        return
    newest_ts = max(m.get("createdDateTime", "") for m in messages)
    if newest_ts > self._resource_cursors.get(resource, ""):
        self._resource_cursors[resource] = newest_ts
```

**Design decisions:**
- Cursors are **in-memory only**. On restart, the lookback window re-initializes from
  `poll_lookback_minutes`, accepting possible re-processing. The existing `_processed_ids` dedup
  set handles any duplicates within the overlap window.
- Persistent cursor storage is a future enhancement (§10.2).

#### 6.4.4 Dedup / New-Message Filtering (`_filter_new_messages`)

Reuses the existing `_processed_ids` dedup set (shared with webhook mode):

```python
def _filter_new_messages(self, resource: str, messages: list[dict]) -> list[dict]:
    """Filter out already-processed and bot-own messages, return newest-first."""
    result = []
    for msg in messages:
        msg_id = msg.get("id", "")
        if msg_id in self._processed_ids:
            continue
        # Skip bot's own messages
        from_user = msg.get("from", {}).get("user", {})
        if self._bot_user_id and from_user.get("id") == self._bot_user_id:
            continue
        result.append(msg)
    return result
```

#### 6.4.5 Shared Message Processing (`_process_message`)

Both webhook and polling paths share a common `_process_message` method. The webhook path's
`_process_notification` fetches the full message via GET then delegates to `_process_message`.
The polling path already has the full message object and calls `_process_message` directly:

```python
async def _process_message(self, resource: str, message: dict) -> None:
    """Process a full message object (shared by webhook and polling paths)."""
    msg_id = message.get("id", "")
    if msg_id in self._processed_ids:
        return
    self._processed_ids.add(msg_id)
    self._trim_processed_ids()

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

    if msg_type == "channel" and not self._is_group_allowed(
        sender_id, chat_id, msg_type, raw_content
    ):
        return

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
```

The webhook `_process_notification` is simplified to:

```python
async def _process_notification(self, notification: dict) -> None:
    resource = notification.get("resource", "")
    if notification.get("changeType") != "created":
        return
    message = await self._graph_client.get(f"{GRAPH_BASE_URL}/{resource}")
    await self._process_message(resource, message)
```

#### 6.4.6 Polling Mode Design Notes

- Reuses existing `GraphClient` retry/rate-limit/auth behavior.
- Uses per-resource cursor/checkpoint (`_resource_cursors`) with `$filter` to avoid re-fetching.
- Shares `_processed_ids` dedup set with webhook mode for consistent dedup behavior.
- Keeps self-message filtering and `allowFrom` checks identical to webhook mode.
- Does not create Graph subscriptions and does not require `webhook_host`.

---

## 7. Outbound: Sending Messages

### 7.1 Send Implementation

```python
async def send(self, msg: OutboundMessage) -> None:
    teams_meta = msg.metadata.get("teams", {})
    msg_type = teams_meta.get("message_type", "chat")
    thread_id = teams_meta.get("thread_id")

    body = {
        "body": {
            "contentType": "text",
            "content": msg.content,
        }
    }

    if msg_type == "channel":
        # Channel message: POST /teams/{teamId}/channels/{channelId}/messages
        # or reply: POST .../messages/{messageId}/replies
        resource = teams_meta.get("resource", "")
        if self.config.reply_in_thread and thread_id:
            url = f"https://graph.microsoft.com/v1.0/{resource}/{thread_id}/replies"
        else:
            url = f"https://graph.microsoft.com/v1.0/{resource}"
        await self._graph_client.post(url, json=body)
    else:
        # Chat message: POST /chats/{chatId}/messages
        url = f"https://graph.microsoft.com/v1.0/chats/{msg.chat_id}/messages"
        await self._graph_client.post(url, json=body)
```

### 7.2 Rate Limiting

Graph API enforces a limit of **10 messages per 10 seconds** per chat. The implementation uses
a partition-based semaphore inspired by the DataIngestion project's `APIMiddleware`:

```python
class GraphClient:
    """HTTP client wrapper with auth, rate limiting, and retry."""

    def __init__(self, auth_manager: TeamsAuthManager, config: TeamsConfig):
        self._auth = auth_manager
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        self._retry_max = config.retry_max_attempts
        self._retry_base_delay_ms = config.retry_base_delay_ms
        self._client = httpx.AsyncClient(timeout=30.0)

    async def request(self, method: str, url: str, **kwargs) -> dict:
        token = await self._auth.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        for attempt in range(1, self._retry_max + 1):
            async with self._semaphore:
                try:
                    resp = await self._client.request(
                        method, url, headers=headers, **kwargs
                    )

                    if resp.status_code in (200, 201, 202, 204):
                        return resp.json() if resp.content else {}

                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get("Retry-After", "60"))
                        logger.warning("Teams API throttled, retry after {}s", retry_after)
                        await asyncio.sleep(retry_after)
                        continue

                    if resp.status_code >= 500:
                        delay = self._backoff_delay(attempt)
                        logger.warning(
                            "Teams API {}, attempt {}/{}, backoff {}ms",
                            resp.status_code, attempt, self._retry_max, delay,
                        )
                        await asyncio.sleep(delay / 1000)
                        continue

                    # 4xx (non-429): non-retriable
                    resp.raise_for_status()

                except httpx.TimeoutException:
                    delay = self._backoff_delay(attempt)
                    logger.warning("Teams API timeout, attempt {}/{}", attempt, self._retry_max)
                    await asyncio.sleep(delay / 1000)
                    continue

        raise RuntimeError(f"Teams API request failed after {self._retry_max} attempts: {url}")

    def _backoff_delay(self, attempt: int) -> int:
        """Exponential backoff: base * 1.5^(attempt-1), capped at 30s."""
        delay = self._retry_base_delay_ms * (1.5 ** (attempt - 1))
        return min(int(delay), 30_000)

    # Convenience methods
    async def get(self, url: str, **kw) -> dict:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw) -> dict:
        return await self.request("POST", url, **kw)

    async def patch(self, url: str, **kw) -> dict:
        return await self.request("PATCH", url, **kw)

    async def delete(self, url: str, **kw) -> dict:
        return await self.request("DELETE", url, **kw)

    async def close(self) -> None:
        await self._client.aclose()
```

---

## 8. Channel Registration

### 8.1 `channels/manager.py`

```python
# Microsoft Teams channel
if self.config.channels.teams.enabled:
    try:
        from nanobot.channels.teams import TeamsChannel
        self.channels["teams"] = TeamsChannel(
            self.config.channels.teams, self.bus
        )
        logger.info("Teams channel enabled")
    except ImportError as e:
        logger.warning("Teams channel not available: {}", e)
```

### 8.2 Dependencies (`pyproject.toml`)

```toml
[project.optional-dependencies]
teams = [
    "aiohttp>=3.9.0",   # Webhook HTTP server
    "httpx>=0.28.0",     # Graph API HTTP client (already a core dep)
]
```

`aiohttp` is only required in `webhook` mode (HTTP callback server). `httpx` is required in both
`webhook` and `polling` modes.

---

## 9. Known Limitations & Trade-offs

### 9.1 Public HTTPS Endpoint Required (Webhook Mode Only)

When `inbound_mode="webhook"`, Graph change notifications require a publicly reachable HTTPS URL.
Deployment options:

| Method | Use Case |
|--------|----------|
| Reverse proxy (nginx/caddy) | Production — terminate TLS at proxy, forward to `webhook_port` |
| Azure App Service | Cloud deployment — built-in HTTPS |

When `inbound_mode="polling"`, no public HTTPS endpoint is required.

### 9.2 Channel Message Sending Constraints

As of Graph API v1.0, posting to a **team channel** with application permissions is only supported
for migration (`Teamwork.Migrate.All`). This design avoids that limitation by using delegated user
tokens for both chat and channel send.

Current and future delegated options:

1. **Password bootstrap (current)** — Service account username/password to acquire delegated token.
2. **Device code (future)** — MFA-capable interactive sign-in without storing user password.
3. **FIC provider (future)** — Federated identity credential flow for secretless workloads.

Known trade-off: if tenant disables password grant/ROPC, `password` mode will fail and migration to
`device_code`/`fic` is required.

### 9.3 Subscription Limits

- Maximum **active subscriptions per app**: Varies by resource type and tenant. Monitor for
  `SubscriptionLimitExceeded` errors.
- Subscriptions with `expirationDateTime` > 1 hour require a `lifecycleNotificationUrl`. The
  design uses 55-minute TTL to stay within the limit.
- **Missed notifications**: If the webhook endpoint is down for > 60 minutes (subscription
  expires), messages are lost. Consider a polling fallback for recovery (§10.2).

### 9.4 Rate Limits

- **Sending**: 10 messages per 10 seconds per chat (enforced by Graph API).
- **Subscriptions**: Standard Graph API throttling (429 responses with `Retry-After` header).
- **GET message content**: Standard Graph API throttling applies.

### 9.5 Polling Trade-offs

- Higher receive latency (depends on `poll_interval_seconds`, minimum 3s).
- Higher Graph API request volume than webhook mode (one GET per resource per poll cycle).
- In-memory cursors: on restart, re-processes messages within the `poll_lookback_minutes` window.
  Dedup via `_processed_ids` prevents duplicate forwarding to the agent, but the lookback window
  should be kept short (default 30 min) to minimize redundant API calls.
- `$filter` and `$orderby` support varies by Graph API endpoint. Chat messages generally support
  `createdDateTime` filtering; some channel message endpoints may not. Implementation should
  gracefully fall back to client-side filtering if the API rejects the query parameters.

---

## 10. Future Extensions

### 10.1 Rich Notifications (`includeResourceData: true`)

Adding encrypted resource data to subscription notifications would eliminate the GET round-trip
per message. Requires:

- Generating an X.509 encryption certificate
- Setting `encryptionCertificate` and `encryptionCertificateId` on subscription
- Decrypting `encryptedContent` in the webhook handler using the certificate's private key

This is an optimization for high-throughput scenarios.

### 10.2 Polling Enhancements

Improve polling efficiency and correctness with:

- Delta-query support where available.
- Persistent cursor checkpoint storage in workspace state.
- Adaptive poll interval (slow when idle, fast on recent activity).

### 10.3 Adaptive Cards

Support sending structured Adaptive Card JSON in outbound messages for richer formatting (buttons,
forms, images). Requires `"contentType": "html"` with card attachment payloads.

### 10.4 @Mention Support

Parse `<at>` tags in inbound HTML messages and generate proper `mentions` arrays in outbound
messages to support @mentioning users in Teams.

### 10.5 Media/File Support

Upload files via `POST /chats/{id}/messages` with `hostedContents` (inline images) or OneDrive
attachment references.

---

## 11. File Inventory

| File | Action | Description |
|------|--------|-------------|
| `nanobot/channels/teams/__init__.py` | **Exists** | Package exports |
| `nanobot/channels/teams/auth.py` | **Exists** | Auth providers (Password, Static, AuthManager) |
| `nanobot/channels/teams/channel.py` | **Modify** | Add polling mode branch to `start()`/`stop()`, add polling methods |
| `nanobot/channels/teams/graph_client.py` | **Exists** | Graph API HTTP client wrapper |
| `nanobot/channels/teams/subscriptions.py` | **Exists** | SubscriptionManager for webhook mode |
| `nanobot/config/schema.py` | **Modify** | Add polling config fields to `TeamsConfig` |
| `nanobot/channels/manager.py` | **Exists** | Channel initialization |
| `tests/test_teams_channel.py` | **Modify** | Add polling-mode unit tests |

### `TeamsChannel` Method Structure

```
nanobot/channels/teams/channel.py
└── TeamsChannel(BaseChannel)
    ├── start()                    # Branching: webhook vs polling
    ├── stop()                     # Conditional cleanup for both modes
    ├── send()                     # Post message to chat or channel
    │
    ├── # Webhook-mode methods
    ├── _handle_webhook()          # HTTP handler for Graph notifications
    ├── _process_notification()    # Fetch message via GET → _process_message()
    │
    ├── # Polling-mode methods
    ├── _polling_loop()            # Periodic poll loop
    ├── _list_messages()           # GET /chats/{id}/messages with $filter/$top
    ├── _filter_new_messages()     # Dedup + skip bot-own messages
    ├── _init_cursors()            # Initialize per-resource timestamp cursors
    ├── _advance_cursor()          # Update cursor after processing
    │
    ├── # Shared methods (used by both modes)
    ├── _process_message()         # Core message processing (dedup, parse, forward)
    ├── _parse_resource()          # Extract chat_id / channel_id from resource path
    ├── _strip_html()              # Clean HTML content from Graph messages
    ├── _is_group_allowed()        # Group policy check
    └── _resolve_bot_identity()    # GET /me to resolve bot user ID
```

---

## 12. Testing Strategy

### 12.1 Unit Tests

- **Auth**: Token acquisition, caching, refresh-before-expiry.
- **Webhook handler**: Validation token handshake, clientState verification, notification
  parsing, self-message filtering.
- **Polling loop**: Cursor progression, de-dup, lookback initialization, self-message filtering.
- **Subscription manager**: Create, renew, delete, re-create on failure.
- **Send**: Chat vs channel routing, thread reply construction, HTML content type.
- **Rate limiting**: Semaphore fairness, 429 retry-after, exponential backoff.
- **Access control**: `is_allowed()` with Azure AD user IDs.

### 12.2 Integration Tests

- End-to-end with a real Azure AD app registration and a test Teams tenant.
- Webhook delivery via self-hosted public HTTPS endpoint (e.g., reverse proxy on cloud VM).
- Subscription lifecycle over multiple renewal cycles.
- Polling-only inbound without webhook/public endpoint.

### 12.3 Mock Strategy

Use `httpx`'s transport mocking (`httpx.MockTransport`) to simulate Graph API responses (200,
201, 429, 500) without real network calls. Use `aiohttp.test_utils.AioHTTPTestCase` for webhook
server tests.
