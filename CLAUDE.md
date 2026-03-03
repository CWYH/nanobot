# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

nanobot is an ultra-lightweight personal AI assistant framework (~4,000 lines of core agent code). It connects to 13+ chat platforms and multiple LLM providers through a modular architecture. Python 3.11+, MIT licensed.

## Build & Development Commands

```bash
# Install from source (editable)
pip install -e .

# Install with dev dependencies
pip install -e ".[dev]"

# Install with Matrix channel support
pip install -e ".[matrix]"

# Run tests
pytest tests/

# Run a single test file
pytest tests/test_commands.py -v

# Run a specific test
pytest tests/test_commands.py::test_function_name -v

# Lint
ruff check nanobot/

# Format
ruff format nanobot/

# Run the CLI agent (interactive)
nanobot agent

# Run the gateway (starts all enabled channels)
nanobot gateway

# Docker
docker compose up -d nanobot-gateway
docker compose run --rm nanobot-cli agent -m "Hello!"
```

## Code Style

- **Line length**: 100 characters max
- **Linter**: Ruff with rules E, F, I, N, W (E501 ignored)
- **Target**: Python 3.11+
- **Config models**: Pydantic 2.x `BaseModel` subclasses that accept both camelCase and snake_case keys (via `to_camel` alias generator)
- **Async**: All I/O is async (asyncio). Tools, channels, and the agent loop are all async
- **Logging**: Use `loguru.logger`, not stdlib `logging`
- **Tests**: pytest with `asyncio_mode = "auto"` — async test functions work without decorators

## Architecture

### Core Processing Flow

```
Channels (Telegram, Slack, etc.)
        |
   MessageBus (async queue)
        |
   AgentLoop (nanobot/agent/loop.py)
     |- ContextBuilder → assembles system prompt from templates, memory, skills
     |- LLMProvider → calls LLM via LiteLLM abstraction
     |- ToolRegistry → executes tool calls (shell, filesystem, web, cron, MCP)
     |- MemoryStore → persists conversation history
        |
   Channel Adapters (send responses back)
```

### Key Modules

- **`nanobot/agent/loop.py`** — Central agent engine. Receives messages, builds context, calls LLM, executes tools iteratively (up to 40 iterations), returns responses.
- **`nanobot/agent/context.py`** — Assembles system prompts from bootstrap template files (`SOUL.md`, `AGENTS.md`, `USER.md`, `TOOLS.md`, `IDENTITY.md`), memory context, and loaded skills.
- **`nanobot/agent/memory.py`** — Persistent memory with consolidation. Stores conversation turns and memory entries in the workspace.
- **`nanobot/agent/tools/`** — Built-in tools that the agent can call: `shell.py` (exec), `filesystem.py` (read/write/edit/list), `web.py` (fetch/search), `cron.py` (scheduling), `spawn.py` (subagent), `message.py` (user messaging), `mcp.py` (MCP tool bridge).
- **`nanobot/agent/tools/base.py`** — Abstract `Tool` base class. All tools implement `name`, `description`, `parameters` (JSON Schema), and `execute(**kwargs) -> str`.
- **`nanobot/channels/base.py`** — Abstract `BaseChannel` with `start()`, `stop()`, and message handling. Each channel adapter (telegram.py, slack.py, etc.) implements this.
- **`nanobot/channels/manager.py`** — `ChannelManager` initializes enabled channels from config and routes outbound messages.
- **`nanobot/providers/registry.py`** — Single source of truth for LLM provider metadata. Each provider is a `ProviderSpec` dataclass. Adding a provider: (1) add `ProviderSpec` to `PROVIDERS` list, (2) add field to `ProvidersConfig` in `config/schema.py`.
- **`nanobot/providers/base.py`** — Abstract `LLMProvider`. `litellm_provider.py` is the main implementation wrapping LiteLLM.
- **`nanobot/config/schema.py`** — All configuration as Pydantic models. Root `Config` contains `ProvidersConfig`, `ChannelsConfig`, `AgentConfig`, `ToolsConfig`, etc. Config loaded from `~/.nanobot/config.json`.
- **`nanobot/bus/`** — Async message bus decoupling channels from the agent. `InboundMessage`/`OutboundMessage` events.
- **`nanobot/cron/`** — Scheduled task service using `croniter`. Has context-variable guards to prevent cron self-scheduling loops.
- **`nanobot/session/manager.py`** — Session persistence and history management.
- **`nanobot/skills/`** — Modular skill system. Each skill has a `SKILL.md` defining its behavior. Skills are loaded into the agent context at runtime.
- **`nanobot/templates/`** — Prompt template files loaded by `ContextBuilder` to compose the system prompt.
- **`nanobot/cli/commands.py`** — Typer CLI application. Entry point: `nanobot = "nanobot.cli.commands:app"`.

### WhatsApp Bridge

`bridge/` contains a TypeScript/Node.js WhatsApp bridge using `@whiskeysockets/baileys`. It runs as a separate process and communicates with the Python gateway via WebSocket. Requires Node.js >= 18.

## Adding New Components

### New Tool
1. Create a class in `nanobot/agent/tools/` extending `Tool` from `base.py`
2. Implement `name`, `description`, `parameters`, and `execute()`
3. Register it in `AgentLoop.__init__()` in `loop.py`

### New Channel
1. Create a class in `nanobot/channels/` extending `BaseChannel` from `base.py`
2. Implement `start()`, `stop()`, and message handling
3. Add its config model to `schema.py` and add initialization in `ChannelManager._init_channels()`

### New LLM Provider
1. Add a `ProviderSpec` entry to `PROVIDERS` in `nanobot/providers/registry.py`
2. Add a `ProviderConfig` field to `ProvidersConfig` in `nanobot/config/schema.py`

## Configuration

User config lives at `~/.nanobot/config.json`. The workspace defaults to `~/.nanobot/workspace/`. Config schema uses Pydantic with camelCase JSON keys mapped to snake_case Python attributes.

## Security Notes

- `allowFrom` on channels: empty list means deny-all in current source (post-v0.1.4.post3). Use `["*"]` for allow-all.
- `restrictToWorkspace: true` sandboxes all agent file/shell operations to the workspace directory.
- Cron execution uses `contextvars` to prevent self-scheduling loops.
