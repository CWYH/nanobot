# Multi-Bot Configuration Isolation and Startup Design

## Background

The current system reads configuration from a default home path and Docker Compose mounts a shared host directory into containers. This makes multi-instance deployment on one machine prone to shared config/state issues.

This spec defines how to run multiple nanobot instances on the same host with strict isolation:

- Each container has its own config file.
- Each container has its own runtime state directory.
- Each bot can be started with an explicit config path.
- Gateway port is controlled by each bot's own config.

## Goals

- Support explicit config path at startup (`configPath`) for each bot.
- Keep existing CLI behavior while adding config-driven port behavior.
- Ensure no config sharing across containers.
- Ensure no runtime-state sharing across containers.
- Support practical Docker Compose multi-bot deployment.

## Non-Goals

- Running multiple Teams identities in a single process.
- Rewriting channel authentication architecture.
- Large schema redesign beyond targeted enhancements.

## Current Problems

- Default config path is fixed to a home-based location.
- CLI entrypoints load config using default path unless explicitly changed in code.
- Docker Compose currently mounts a shared `~/.nanobot` path, which causes shared config/state.
- `gateway.port` exists in schema but startup flow does not fully use per-config port as the main mechanism.
- Host port conflicts occur when multiple containers expose the same host port.

## Proposed Design

### 1) Injectable Config Path

Add a `configPath` startup parameter in key CLI entrypoints (`gateway`, `agent`, `status`) and pass it through to config loading.

Behavior:

- If `configPath` is provided, load that file.
- If not provided, keep current default-path behavior for backward compatibility.

### 2) Port Resolution (Keep Existing + Add Config)

Port precedence:

1. Explicit CLI `--port`
2. `config.gateway.port`
3. Schema default

Rationale:

- Preserves existing scripts and operational behavior.
- Enables clean per-bot config-driven deployment for multi-instance setups.

### 3) Strict Per-Instance Isolation

Each bot instance must have its own directory containing at least:

- `config.json`
- workspace files
- session files
- cron/jobs and other runtime artifacts

Docker mount strategy:

- bot1: `~/.nanobot/instances/bot1 -> /root/.nanobot`
- bot2: `~/.nanobot/instances/bot2 -> /root/.nanobot`
- etc.

This guarantees each container reads/writes only its own state.

### 4) Docker Compose Multi-Bot Pattern

For each bot service:

- Unique `container_name`
- Unique host port mapping (container port may remain the same)
- Per-service config path (after CLI support is added)
- Per-service isolated data mount

## Implementation Steps

1. Add `configPath` option to CLI entrypoints and thread it to `load_config(config_path=...)`.
2. Wire gateway startup to read `config.gateway.port` with precedence `CLI > config > default`.
3. Normalize critical home-path usages to ensure per-instance data root isolation.
4. Extend `docker-compose.yml` with bot1/bot2 examples showing isolated mounts and unique host ports.
5. Update `README.md` with a dedicated "multiple bots on one host" deployment guide.
6. Add tests for config-path loading, port precedence, and instance isolation behavior.

## Acceptance Criteria

- Two or more containers start simultaneously on one host without config sharing.
- Each container reads a different config and has a different Teams account if configured.
- Runtime state is isolated; editing bot1 config/state does not impact bot2.
- Port behavior follows precedence rules exactly.
- Added/updated tests pass.

## Risks and Migration Notes

- Splitting only config file without splitting data directory still risks state cross-contamination.
- Reusing the same host port for multiple containers will fail at startup.
- Existing users should migrate gradually (start with two instances, validate, then scale).

## Open Questions

- Should `configPath` also be supported via environment variable (for container orchestration parity)?
- Should we add a single global `dataRoot` to eliminate scattered home-path assumptions completely?
- Should we provide a helper command to bootstrap per-instance directory structure automatically?
