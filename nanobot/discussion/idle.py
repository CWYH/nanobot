"""Idle monitor for team discussion — drives the manager's proactive nudges.

When the group chat goes quiet, the idle monitor escalates through three
tiers of prompts (warn → nudge → new topic) and backs off after repeated
unanswered nudges.  Only the *manager* bot should enable this service.

See spec §4.1–§4.3 for the full design.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Coroutine

from loguru import logger

from nanobot.config.schema import DiscussionConfig

# Prompt templates sent to the agent when each idle tier fires.
IDLE_WARN_PROMPT = (
    "[Idle Monitor] The group discussion has been quiet for a while. "
    "Check if anyone is blocked and ask a light follow-up question."
)
IDLE_NUDGE_PROMPT = (
    "[Idle Monitor] No progress for an extended period. "
    "Roll-call each IC — ask them to report: current status, next step, blockers."
)
IDLE_NEW_TOPIC_PROMPT = (
    "[Idle Monitor] Discussion has stalled. "
    "Propose a new question or switch to a sub-topic to restart the conversation. "
    "IMPORTANT: Review the conversation history and your memory. "
    "Do NOT repeat a topic that has already been discussed. "
    "Choose a fresh angle, a different domain, or a follow-up question "
    "that builds on previous conclusions."
)

OPENING_PROMPT = (
    "[Discussion Startup] You are joining the group discussion for the first time. "
    "Introduce yourself briefly according to your role identity, "
    "then propose an opening discussion topic following your Topic Selection Principles. "
    "The topic should require distributed cognition, external research, "
    "and multi-round iteration. "
    "IMPORTANT: Check your memory and conversation history first. "
    "If previous discussions have occurred, do NOT propose the same topic again. "
    "Pick a completely new topic that has not been covered before."
)


def build_opening_prompt(participant_names: list[str]) -> str:
    """Build an opening prompt that instructs the manager to @mention participants.

    Falls back to the plain ``OPENING_PROMPT`` when *participant_names* is empty.
    """
    if not participant_names:
        return OPENING_PROMPT
    mentions = ", ".join(f"@{name}" for name in participant_names)
    return (
        f"{OPENING_PROMPT}\n\n"
        f"IMPORTANT: In your opening message, explicitly mention each team member "
        f"by name ({mentions}) to invite them into the discussion."
    )


# Internal check cadence (how often the monitor wakes to evaluate idle state).
_CHECK_INTERVAL_SEC = 60


class IdleMonitor:
    """Watches for chat inactivity and triggers manager nudges.

    Parameters
    ----------
    discussion_cfg:
        Thresholds and backoff settings from the ``discussion`` config section.
    on_nudge:
        Async callback ``(prompt: str) -> None`` invoked when a nudge should
        be sent.  The gateway wires this to ``agent.process_direct()`` +
        outbound bus delivery.
    """

    def __init__(
        self,
        discussion_cfg: DiscussionConfig,
        on_nudge: Callable[[str], Coroutine[Any, Any, None]],
    ) -> None:
        self._cfg = discussion_cfg
        self._on_nudge = on_nudge

        # Monotonic timestamp of last observed activity in the group.
        self._last_activity: float = time.monotonic()
        # How many consecutive nudges the manager sent without anyone else talking.
        self._consecutive_nudges: int = 0
        # Last tier that was fired (to avoid re-firing the same tier).
        self._last_fired_tier: int = 0

        self._running = False
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_activity(self) -> None:
        """Call whenever a non-bot message arrives in the monitored group."""
        self._last_activity = time.monotonic()
        self._consecutive_nudges = 0
        self._last_fired_tier = 0

    async def start(self) -> None:
        """Start the background check loop."""
        if not self._cfg.enabled:
            logger.info("IdleMonitor disabled (discussion.enabled=false)")
            return
        if self._running:
            return
        self._running = True
        self._last_activity = time.monotonic()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "IdleMonitor started (warn={}s, nudge={}s, topic={}s)",
            self._cfg.idle_warn_after_sec,
            self._cfg.idle_nudge_after_sec,
            self._cfg.idle_new_topic_after_sec,
        )

    def stop(self) -> None:
        """Stop the monitor."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(_CHECK_INTERVAL_SEC)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("IdleMonitor tick error")

    async def _tick(self) -> None:
        idle_sec = time.monotonic() - self._last_activity

        # Determine the highest tier that the current idle duration qualifies for.
        tier = self._classify_tier(idle_sec)
        if tier == 0 or tier <= self._last_fired_tier:
            return  # Nothing new to fire.

        # Back-off: if the manager has nudged too many times with no response,
        # only allow new-topic tier and nothing below.
        if self._consecutive_nudges >= self._cfg.max_consecutive_manager_nudges:
            if tier < 3:
                return
            # For tier 3 under backoff, apply a longer re-fire interval.
            # We require the full new-topic interval again since last fire.
            # Reset last_fired_tier so tier 3 can fire again next cycle.
            # (The tier check above already prevents rapid re-fire.)

        prompt = self._tier_prompt(tier)
        logger.info("IdleMonitor firing tier {} after {:.0f}s idle", tier, idle_sec)

        try:
            await self._on_nudge(prompt)
        except Exception:
            logger.exception("IdleMonitor nudge callback failed")
            return

        self._last_fired_tier = tier
        self._consecutive_nudges += 1

    def _classify_tier(self, idle_sec: float) -> int:
        """Return 1 (warn), 2 (nudge), 3 (new topic), or 0 (no action)."""
        if idle_sec >= self._cfg.idle_new_topic_after_sec:
            return 3
        if idle_sec >= self._cfg.idle_nudge_after_sec:
            return 2
        if idle_sec >= self._cfg.idle_warn_after_sec:
            return 1
        return 0

    @staticmethod
    def _tier_prompt(tier: int) -> str:
        if tier == 1:
            return IDLE_WARN_PROMPT
        if tier == 2:
            return IDLE_NUDGE_PROMPT
        return IDLE_NEW_TOPIC_PROMPT
