"""CLI commands for nanobot."""

import asyncio
import os
import select
import signal
import sys
from pathlib import Path

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from nanobot import __logo__, __version__
from nanobot.config.schema import Config
from nanobot.utils.helpers import sync_workspace_templates

app = typer.Typer(
    name="nanobot",
    help=f"{__logo__} nanobot - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios

        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios

        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session(data_dir: Path | None = None) -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios

        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    base = data_dir or (Path.home() / ".nanobot")
    history_file = base / "history" / "cli_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,  # Enter submits (single line mode)
    )


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} nanobot[/cyan]")
    console.print(body)
    console.print()


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} nanobot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(None, "--version", "-v", callback=version_callback, is_eager=True),
):
    """nanobot - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to config file",
        dir_okay=False,
        resolve_path=True,
    ),
):
    """Initialize nanobot configuration and workspace."""
    from nanobot.config.loader import get_config_path, get_data_dir, load_config, save_config
    from nanobot.config.schema import Config
    from nanobot.utils.helpers import get_workspace_path

    config_path = config or get_config_path()
    data_dir = get_data_dir(config_path=config)

    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
        console.print(
            "  [bold]N[/bold] = refresh config, keeping existing values and adding new fields"
        )
        if typer.confirm("Overwrite?"):
            cfg = Config()
            save_config(cfg, config_path=config_path)
            console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
        else:
            cfg = load_config(config_path=config_path)
            save_config(cfg, config_path=config_path)
            console.print(
                f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)"
            )
    else:
        save_config(Config(), config_path=config_path)
        console.print(f"[green]✓[/green] Created config at {config_path}")

    # Create workspace — use config-aware path when --config is provided
    if config:
        workspace = data_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        workspace = get_workspace_path()

    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace}")

    sync_workspace_templates(workspace)

    console.print(f"\n{__logo__} nanobot is ready!")
    console.print("\nNext steps:")
    console.print(f"  1. Add your API key to [cyan]{config_path}[/cyan]")
    console.print("     Get one at: https://openrouter.ai/keys")
    console.print('  2. Chat: [cyan]nanobot agent -m "Hello!"[/cyan]')
    console.print(
        "\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/nanobot#-chat-apps[/dim]"
    )


def _make_provider(config: Config):
    """Create the appropriate LLM provider from config."""
    from nanobot.providers.custom_provider import CustomProvider
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)

    # OpenAI Codex (OAuth)
    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        return OpenAICodexProvider(default_model=model)

    # Custom: direct OpenAI-compatible endpoint, bypasses LiteLLM
    if provider_name == "custom":
        return CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
        )

    from nanobot.providers.registry import find_by_name

    spec = find_by_name(provider_name)
    if not model.startswith("bedrock/") and not (p and p.api_key) and not (spec and spec.is_oauth):
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.nanobot/config.json under providers section")
        raise typer.Exit(1)

    return LiteLLMProvider(
        api_key=p.api_key if p else None,
        api_base=config.get_api_base(model),
        default_model=model,
        extra_headers=p.extra_headers if p else None,
        provider_name=provider_name,
    )


# ============================================================================
# Gateway / Server
# ============================================================================


def _resolve_port(ctx: typer.Context, cli_port: int, config_port: int) -> int:
    """Resolve gateway port with precedence: CLI explicit > config > schema default.

    Uses Click's parameter source tracking to distinguish between
    a user passing ``--port`` explicitly vs. the Typer default being applied.
    """
    import click

    source = ctx.get_parameter_source("port")
    if source == click.core.ParameterSource.COMMANDLINE:
        return cli_port
    return config_port


@app.command()
def gateway(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to config file",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start the nanobot gateway."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.config.loader import get_data_dir, load_config
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronJob
    from nanobot.heartbeat.service import HeartbeatService
    from nanobot.session.manager import SessionManager

    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG)

    cfg = load_config(config_path=config)
    effective_port = _resolve_port(ctx, cli_port=port, config_port=cfg.gateway.port)

    console.print(f"{__logo__} Starting nanobot gateway on port {effective_port}...")

    sync_workspace_templates(cfg.workspace_path)

    # Generate IDENTITY.md from config identity section (spec §3.2).
    # Overwrites on every startup so the workspace always matches the config.
    identity_cfg = cfg.agents.defaults.identity
    if identity_cfg and identity_cfg.role_id:
        from nanobot.utils.helpers import generate_identity_md

        identity_md = generate_identity_md(identity_cfg)
        (cfg.workspace_path / "IDENTITY.md").write_text(identity_md, encoding="utf-8")
        console.print(
            f"[green]✓[/green] Identity: {identity_cfg.display_name or identity_cfg.role_id}"
        )

    # Auto-apply discussion reply delay when discussion mode is enabled
    # but reply_delay was not explicitly configured.
    if cfg.discussion.enabled and not cfg.agents.defaults.reply_delay.enabled:
        from nanobot.config.schema import DISCUSSION_REPLY_DELAY_DEFAULTS

        effective = cfg.discussion.reply_delay_override or DISCUSSION_REPLY_DELAY_DEFAULTS
        cfg.agents.defaults.reply_delay = effective

    rd = cfg.agents.defaults.reply_delay
    if rd.enabled:
        console.print(
            f"[green]✓[/green] ReplyDelay: base={rd.base_delay_sec}s "
            f"jitter={rd.jitter_sec}s per100c={rd.per_100_chars_sec}s "
            f"max={rd.max_delay_sec}s"
        )

    bus = MessageBus()
    provider = _make_provider(cfg)
    session_manager = SessionManager(cfg.workspace_path)

    # Create cron service first (callback set after agent creation)
    data_dir = get_data_dir(config_path=config)
    cron_store_path = data_dir / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=cfg.workspace_path,
        model=cfg.agents.defaults.model,
        temperature=cfg.agents.defaults.temperature,
        max_tokens=cfg.agents.defaults.max_tokens,
        max_iterations=cfg.agents.defaults.max_tool_iterations,
        memory_window=cfg.agents.defaults.memory_window,
        reasoning_effort=cfg.agents.defaults.reasoning_effort,
        brave_api_key=cfg.tools.web.search.api_key or None,
        web_proxy=cfg.tools.web.proxy or None,
        exec_config=cfg.tools.exec,
        cron_service=cron,
        restrict_to_workspace=cfg.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=cfg.tools.mcp_servers,
        channels_config=cfg.channels,
        reply_delay=cfg.agents.defaults.reply_delay,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        from nanobot.agent.tools.cron import CronTool
        from nanobot.agent.tools.message import MessageTool

        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        # Prevent the agent from scheduling new cron jobs during execution
        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            response = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            from nanobot.bus.events import OutboundMessage

            await bus.publish_outbound(
                OutboundMessage(
                    channel=job.payload.channel or "cli", chat_id=job.payload.to, content=response
                )
            )
        return response

    cron.on_job = on_cron_job

    # Create channel manager
    channels = ChannelManager(cfg, bus, data_dir=data_dir)

    def _pick_heartbeat_target() -> tuple[str, str]:
        """Pick a routable channel/chat target for heartbeat-triggered messages."""
        enabled = set(channels.enabled_channels)
        # Prefer the most recently updated non-internal session on an enabled channel.
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        # Fallback: derive chat_id from Teams subscription resource paths.
        if "teams" in enabled and cfg.channels.teams.subscriptions:
            from nanobot.channels.teams.channel import _CHAT_MSG_RE

            for resource in cfg.channels.teams.subscriptions:
                m = _CHAT_MSG_RE.search(resource)
                if m:
                    return "teams", m.group("chat_id")
        # Fallback keeps prior behavior but remains explicit.
        return "cli", "direct"

    # Create heartbeat service
    async def on_heartbeat_execute(tasks: str) -> str:
        """Phase 2: execute heartbeat tasks through the full agent loop."""
        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        return await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

    async def on_heartbeat_notify(response: str) -> None:
        """Deliver a heartbeat response to the user's channel."""
        from nanobot.bus.events import OutboundMessage

        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return  # No external channel available to deliver to
        await bus.publish_outbound(
            OutboundMessage(channel=channel, chat_id=chat_id, content=response)
        )

    hb_cfg = cfg.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=cfg.workspace_path,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
    )

    # ------------------------------------------------------------------
    # Idle monitor for team discussion (spec §4.1–§4.3)
    # Only activates when discussion.enabled=true AND identity is manager.
    # ------------------------------------------------------------------
    idle_monitor = None
    is_manager = bool(identity_cfg and identity_cfg.role_id == "manager") if identity_cfg else False

    if cfg.discussion.enabled and is_manager:
        from nanobot.discussion.idle import IdleMonitor

        async def _on_idle_nudge(prompt: str) -> None:
            """Send an idle nudge through the agent and deliver to the group."""
            channel, chat_id = _pick_heartbeat_target()
            if channel == "cli":
                return  # No external channel — nothing to nudge
            response = await agent.process_direct(
                prompt,
                session_key=f"idle:{channel}:{chat_id}",
                channel=channel,
                chat_id=chat_id,
            )
            if response:
                from nanobot.bus.events import OutboundMessage

                await bus.publish_outbound(
                    OutboundMessage(channel=channel, chat_id=chat_id, content=response)
                )

        idle_monitor = IdleMonitor(
            discussion_cfg=cfg.discussion,
            on_nudge=_on_idle_nudge,
        )

        # Hook inbound messages to reset the idle timer.
        _orig_publish_inbound = bus.publish_inbound

        async def _publish_inbound_with_idle(msg):
            idle_monitor.record_activity()
            await _orig_publish_inbound(msg)

        bus.publish_inbound = _publish_inbound_with_idle

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    if idle_monitor:
        console.print(
            f"[green]✓[/green] IdleMonitor: warn={cfg.discussion.idle_warn_after_sec}s "
            f"nudge={cfg.discussion.idle_nudge_after_sec}s "
            f"topic={cfg.discussion.idle_new_topic_after_sec}s"
        )

    # ------------------------------------------------------------------
    # Opening messages — manager greets each subscribed Teams chat on startup.
    # ------------------------------------------------------------------
    async def _send_opening_messages():
        """Manager sends an opening message to each Teams subscription chat."""
        if not (cfg.channels.teams.enabled and cfg.channels.teams.subscriptions):
            return
        from loguru import logger

        from nanobot.channels.teams.channel import _CHAT_MSG_RE
        from nanobot.discussion.idle import build_opening_prompt

        # Build list of other team members' display names for @mentions.
        participant_names: list[str] = []
        if identity_cfg and identity_cfg.team_members:
            participant_names = [
                dname
                for rid, dname in identity_cfg.team_members.items()
                if rid != identity_cfg.role_id
            ]
        opening_prompt = build_opening_prompt(participant_names)

        for resource in cfg.channels.teams.subscriptions:
            m = _CHAT_MSG_RE.search(resource)
            if not m:
                continue
            chat_id = m.group("chat_id")
            try:
                response = await agent.process_direct(
                    opening_prompt,
                    session_key=f"teams:{chat_id}",
                    channel="teams",
                    chat_id=chat_id,
                )
                if response:
                    from nanobot.bus.events import OutboundMessage

                    await bus.publish_outbound(
                        OutboundMessage(channel="teams", chat_id=chat_id, content=response)
                    )
                    logger.info("Opening message sent to teams:{}", chat_id)
                    if idle_monitor:
                        idle_monitor.record_activity()
            except Exception:
                logger.exception("Failed to send opening message to teams:{}", chat_id)

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            if idle_monitor:
                await idle_monitor.start()
            agent_task = asyncio.create_task(agent.run())
            channels_task = asyncio.create_task(channels.start_all())
            # Let channels authenticate before sending opening messages.
            await asyncio.sleep(5)
            if idle_monitor:
                await _send_opening_messages()
            await asyncio.gather(agent_task, channels_task)
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        finally:
            await agent.close_mcp()
            if idle_monitor:
                idle_monitor.stop()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())


# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to config file",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    markdown: bool = typer.Option(
        True, "--markdown/--no-markdown", help="Render assistant output as Markdown"
    ),
    logs: bool = typer.Option(
        False, "--logs/--no-logs", help="Show nanobot runtime logs during chat"
    ),
):
    """Interact with the agent directly."""
    from loguru import logger

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import get_data_dir, load_config
    from nanobot.cron.service import CronService

    cfg = load_config(config_path=config)
    sync_workspace_templates(cfg.workspace_path)

    bus = MessageBus()
    provider = _make_provider(cfg)

    # Create cron service for tool usage (no callback needed for CLI unless running)
    data_dir = get_data_dir(config_path=config)
    cron_store_path = data_dir / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("nanobot")
    else:
        logger.disable("nanobot")

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=cfg.workspace_path,
        model=cfg.agents.defaults.model,
        temperature=cfg.agents.defaults.temperature,
        max_tokens=cfg.agents.defaults.max_tokens,
        max_iterations=cfg.agents.defaults.max_tool_iterations,
        memory_window=cfg.agents.defaults.memory_window,
        reasoning_effort=cfg.agents.defaults.reasoning_effort,
        brave_api_key=cfg.tools.web.search.api_key or None,
        web_proxy=cfg.tools.web.proxy or None,
        exec_config=cfg.tools.exec,
        cron_service=cron,
        restrict_to_workspace=cfg.tools.restrict_to_workspace,
        mcp_servers=cfg.tools.mcp_servers,
        channels_config=cfg.channels,
    )

    # Show spinner when logs are off (no output to miss); skip when logs are on
    def _thinking_ctx():
        if logs:
            from contextlib import nullcontext

            return nullcontext()
        # Animated spinner is safe to use with prompt_toolkit input handling
        return console.status("[dim]nanobot is thinking...[/dim]", spinner="dots")

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        console.print(f"  [dim]↳ {content}[/dim]")

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            with _thinking_ctx():
                response = await agent_loop.process_direct(
                    message, session_id, on_progress=_cli_progress
                )
            _print_agent_response(response, render_markdown=markdown)
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from nanobot.bus.events import InboundMessage

        _init_prompt_session(data_dir=data_dir)
        console.print(
            f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n"
        )

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _exit_on_sigint(signum, frame):
            _restore_terminal()
            console.print("\nGoodbye!")
            os._exit(0)

        signal.signal(signal.SIGINT, _exit_on_sigint)

        async def run_interactive():
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[str] = []

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                        if msg.metadata.get("_progress"):
                            is_tool_hint = msg.metadata.get("_tool_hint", False)
                            ch = agent_loop.channels_config
                            if ch and is_tool_hint and not ch.send_tool_hints:
                                pass
                            elif ch and not is_tool_hint and not ch.send_progress:
                                pass
                            else:
                                console.print(f"  [dim]↳ {msg.content}[/dim]")
                        elif not turn_done.is_set():
                            if msg.content:
                                turn_response.append(msg.content)
                            turn_done.set()
                        elif msg.content:
                            console.print()
                            _print_agent_response(msg.content, render_markdown=markdown)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        user_input = await _read_interactive_input_async()
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()

                        await bus.publish_inbound(
                            InboundMessage(
                                channel=cli_channel,
                                sender_id="user",
                                chat_id=cli_chat_id,
                                content=user_input,
                            )
                        )

                        with _thinking_ctx():
                            await turn_done.wait()

                        if turn_response:
                            _print_agent_response(turn_response[0], render_markdown=markdown)
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to config file",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
):
    """Show channel status."""
    from nanobot.config.loader import load_config

    cfg = load_config(config_path=config)

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    # WhatsApp
    wa = cfg.channels.whatsapp
    table.add_row("WhatsApp", "✓" if wa.enabled else "✗", wa.bridge_url)

    dc = cfg.channels.discord
    table.add_row("Discord", "✓" if dc.enabled else "✗", dc.gateway_url)

    # Feishu
    fs = cfg.channels.feishu
    fs_config = f"app_id: {fs.app_id[:10]}..." if fs.app_id else "[dim]not configured[/dim]"
    table.add_row("Feishu", "✓" if fs.enabled else "✗", fs_config)

    # Mochat
    mc = cfg.channels.mochat
    mc_base = mc.base_url or "[dim]not configured[/dim]"
    table.add_row("Mochat", "✓" if mc.enabled else "✗", mc_base)

    # Telegram
    tg = cfg.channels.telegram
    tg_config = f"token: {tg.token[:10]}..." if tg.token else "[dim]not configured[/dim]"
    table.add_row("Telegram", "✓" if tg.enabled else "✗", tg_config)

    # Slack
    slack = cfg.channels.slack
    slack_config = "socket" if slack.app_token and slack.bot_token else "[dim]not configured[/dim]"
    table.add_row("Slack", "✓" if slack.enabled else "✗", slack_config)

    # DingTalk
    dt = cfg.channels.dingtalk
    dt_config = (
        f"client_id: {dt.client_id[:10]}..." if dt.client_id else "[dim]not configured[/dim]"
    )
    table.add_row("DingTalk", "✓" if dt.enabled else "✗", dt_config)

    # QQ
    qq = cfg.channels.qq
    qq_config = f"app_id: {qq.app_id[:10]}..." if qq.app_id else "[dim]not configured[/dim]"
    table.add_row("QQ", "✓" if qq.enabled else "✗", qq_config)

    # Email
    em = cfg.channels.email
    em_config = em.imap_host if em.imap_host else "[dim]not configured[/dim]"
    table.add_row("Email", "✓" if em.enabled else "✗", em_config)

    console.print(table)


def _get_bridge_dir(data_dir: Path | None = None) -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess

    # User's bridge location
    base = data_dir or (Path.home() / ".nanobot")
    user_bridge = base / "bridge"

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # nanobot/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall nanobot")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to config file",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
):
    """Link device via QR code."""
    import subprocess

    from nanobot.config.loader import get_data_dir, load_config

    cfg = load_config(config_path=config)
    data_dir = get_data_dir(config_path=config)
    bridge_dir = _get_bridge_dir(data_dir=data_dir)

    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")

    env = {**os.environ}
    if cfg.channels.whatsapp.bridge_token:
        env["BRIDGE_TOKEN"] = cfg.channels.whatsapp.bridge_token

    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]npm not found. Please install Node.js.[/red]")


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to config file",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
):
    """Show nanobot status."""
    from nanobot.config.loader import get_config_path, load_config

    config_path = config or get_config_path()
    cfg = load_config(config_path=config)
    workspace = cfg.workspace_path

    console.print(f"{__logo__} nanobot Status\n")

    console.print(
        f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}"
    )
    console.print(
        f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}"
    )

    if config_path.exists():
        from nanobot.providers.registry import PROVIDERS

        console.print(f"Model: {cfg.agents.defaults.model}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(cfg.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(
                    f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}"
                )


# ============================================================================
# Cron Commands
# ============================================================================

cron_app = typer.Typer(help="Manage cron jobs")
app.add_typer(cron_app, name="cron")


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", help="Job name"),
    message: str = typer.Option(..., "--message", help="Message for the agent"),
    cron_expr: str = typer.Option(..., "--cron", help="Cron expression (e.g. '0 9 * * *')"),
    tz: str = typer.Option(None, "--tz", help="Timezone (e.g. 'America/New_York')"),
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to config file",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
):
    """Add a new cron job."""
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronSchedule

    schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)

    data_dir = get_data_dir()
    store_path = data_dir / "cron" / "jobs.json"
    svc = CronService(store_path)

    try:
        job = svc.add_job(name=name, schedule=schedule, message=message)
    except ValueError as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1)

    console.print(f"[green]✓[/green] Job '{job.name}' added ({job.id})")


# ============================================================================
# OAuth Login
# ============================================================================

provider_app = typer.Typer(help="Manage providers")
app.add_typer(provider_app, name="provider")


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn

    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(
        ..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"
    ),
):
    """Authenticate with an OAuth provider."""
    from nanobot.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive

        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(
            f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]"
        )
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    import asyncio

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        from litellm import acompletion

        await acompletion(
            model="github_copilot/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
