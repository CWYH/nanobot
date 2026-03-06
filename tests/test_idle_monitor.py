"""Tests for IdleMonitor in nanobot.discussion.idle."""

import asyncio
import time
from unittest.mock import patch

import pytest

from nanobot.config.schema import DiscussionConfig
from nanobot.discussion.idle import (
    IDLE_NEW_TOPIC_PROMPT,
    IDLE_NUDGE_PROMPT,
    IDLE_WARN_PROMPT,
    OPENING_PROMPT,
    IdleMonitor,
    build_opening_prompt,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_monitor(
    *,
    enabled: bool = True,
    warn: int = 300,
    nudge: int = 900,
    topic: int = 1800,
    max_nudges: int = 2,
    callback=None,
) -> IdleMonitor:
    cfg = DiscussionConfig(
        enabled=enabled,
        idle_warn_after_sec=warn,
        idle_nudge_after_sec=nudge,
        idle_new_topic_after_sec=topic,
        max_consecutive_manager_nudges=max_nudges,
    )
    if callback is None:

        async def _noop(prompt: str) -> None:
            pass

        callback = _noop
    return IdleMonitor(discussion_cfg=cfg, on_nudge=callback)


# ---------------------------------------------------------------------------
# _classify_tier
# ---------------------------------------------------------------------------


class TestClassifyTier:
    def test_no_action_below_warn(self):
        m = _make_monitor(warn=300)
        assert m._classify_tier(0) == 0
        assert m._classify_tier(299) == 0

    def test_warn_tier(self):
        m = _make_monitor(warn=300, nudge=900)
        assert m._classify_tier(300) == 1
        assert m._classify_tier(899) == 1

    def test_nudge_tier(self):
        m = _make_monitor(nudge=900, topic=1800)
        assert m._classify_tier(900) == 2
        assert m._classify_tier(1799) == 2

    def test_new_topic_tier(self):
        m = _make_monitor(topic=1800)
        assert m._classify_tier(1800) == 3
        assert m._classify_tier(9999) == 3


# ---------------------------------------------------------------------------
# _tier_prompt
# ---------------------------------------------------------------------------


class TestTierPrompt:
    def test_tier_1(self):
        assert IdleMonitor._tier_prompt(1) == IDLE_WARN_PROMPT

    def test_tier_2(self):
        assert IdleMonitor._tier_prompt(2) == IDLE_NUDGE_PROMPT

    def test_tier_3(self):
        assert IdleMonitor._tier_prompt(3) == IDLE_NEW_TOPIC_PROMPT

    def test_tier_high_defaults_to_new_topic(self):
        assert IdleMonitor._tier_prompt(99) == IDLE_NEW_TOPIC_PROMPT


# ---------------------------------------------------------------------------
# record_activity
# ---------------------------------------------------------------------------


class TestRecordActivity:
    def test_resets_counters(self):
        m = _make_monitor()
        m._consecutive_nudges = 5
        m._last_fired_tier = 3

        m.record_activity()

        assert m._consecutive_nudges == 0
        assert m._last_fired_tier == 0

    def test_updates_last_activity(self):
        m = _make_monitor()
        # Force last_activity to a known past value.
        m._last_activity = time.monotonic() - 10
        before = m._last_activity
        m.record_activity()
        assert m._last_activity > before


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_disabled(self):
        m = _make_monitor(enabled=False)
        await m.start()
        assert not m._running
        assert m._task is None

    @pytest.mark.asyncio
    async def test_start_enabled(self):
        m = _make_monitor(enabled=True)
        await m.start()
        assert m._running
        assert m._task is not None
        m.stop()
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        m = _make_monitor(enabled=True)
        await m.start()
        first_task = m._task
        await m.start()
        assert m._task is first_task
        m.stop()
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_stop_clears_state(self):
        m = _make_monitor(enabled=True)
        await m.start()
        m.stop()
        assert not m._running
        assert m._task is None


# ---------------------------------------------------------------------------
# _tick — escalation logic
# ---------------------------------------------------------------------------


class TestTick:
    @pytest.mark.asyncio
    async def test_no_fire_when_not_idle(self):
        fired: list[str] = []

        async def _cb(prompt: str) -> None:
            fired.append(prompt)

        m = _make_monitor(warn=300, callback=_cb)
        # Just created → last_activity is now → idle ~0s
        await m._tick()
        assert fired == []

    @pytest.mark.asyncio
    async def test_fires_warn(self):
        fired: list[str] = []

        async def _cb(prompt: str) -> None:
            fired.append(prompt)

        m = _make_monitor(warn=1, nudge=9999, callback=_cb)
        m._last_activity = time.monotonic() - 2  # 2s idle (>1s warn)
        await m._tick()
        assert len(fired) == 1
        assert fired[0] == IDLE_WARN_PROMPT

    @pytest.mark.asyncio
    async def test_fires_nudge(self):
        fired: list[str] = []

        async def _cb(prompt: str) -> None:
            fired.append(prompt)

        m = _make_monitor(warn=1, nudge=2, topic=9999, callback=_cb)
        m._last_activity = time.monotonic() - 3  # 3s idle (>2s nudge)
        await m._tick()
        assert len(fired) == 1
        assert fired[0] == IDLE_NUDGE_PROMPT

    @pytest.mark.asyncio
    async def test_fires_new_topic(self):
        fired: list[str] = []

        async def _cb(prompt: str) -> None:
            fired.append(prompt)

        m = _make_monitor(warn=1, nudge=2, topic=3, callback=_cb)
        m._last_activity = time.monotonic() - 4  # 4s idle (>3s topic)
        await m._tick()
        assert fired == [IDLE_NEW_TOPIC_PROMPT]

    @pytest.mark.asyncio
    async def test_does_not_refire_same_tier(self):
        fired: list[str] = []

        async def _cb(prompt: str) -> None:
            fired.append(prompt)

        m = _make_monitor(warn=1, nudge=9999, callback=_cb)
        m._last_activity = time.monotonic() - 2
        await m._tick()
        assert len(fired) == 1

        # Second tick at same idle level should not re-fire.
        await m._tick()
        assert len(fired) == 1

    @pytest.mark.asyncio
    async def test_escalates_through_tiers(self):
        fired: list[str] = []

        async def _cb(prompt: str) -> None:
            fired.append(prompt)

        m = _make_monitor(warn=1, nudge=2, topic=3, max_nudges=10, callback=_cb)
        m._last_activity = time.monotonic() - 1.5
        await m._tick()
        assert fired == [IDLE_WARN_PROMPT]

        m._last_activity = time.monotonic() - 2.5
        await m._tick()
        assert fired == [IDLE_WARN_PROMPT, IDLE_NUDGE_PROMPT]

        m._last_activity = time.monotonic() - 3.5
        await m._tick()
        assert fired == [IDLE_WARN_PROMPT, IDLE_NUDGE_PROMPT, IDLE_NEW_TOPIC_PROMPT]

    @pytest.mark.asyncio
    async def test_activity_resets_escalation(self):
        fired: list[str] = []

        async def _cb(prompt: str) -> None:
            fired.append(prompt)

        m = _make_monitor(warn=1, nudge=9999, callback=_cb)
        m._last_activity = time.monotonic() - 2
        await m._tick()
        assert len(fired) == 1

        # Activity arrives → reset.
        m.record_activity()

        # Become idle again.
        m._last_activity = time.monotonic() - 2
        await m._tick()
        assert len(fired) == 2  # fires again after reset


# ---------------------------------------------------------------------------
# Backoff logic
# ---------------------------------------------------------------------------


class TestBackoff:
    @pytest.mark.asyncio
    async def test_backoff_blocks_lower_tiers(self):
        fired: list[str] = []

        async def _cb(prompt: str) -> None:
            fired.append(prompt)

        m = _make_monitor(warn=1, nudge=2, topic=9999, max_nudges=1, callback=_cb)
        m._last_activity = time.monotonic() - 3

        # First tick: fires nudge (tier 2).
        await m._tick()
        assert len(fired) == 1
        assert m._consecutive_nudges == 1

        # Reset tier tracking to simulate new interval without activity.
        m._last_fired_tier = 0
        m._last_activity = time.monotonic() - 3

        # Second tick: consecutive_nudges(1) >= max(1), tier 2 blocked.
        await m._tick()
        assert len(fired) == 1  # no new fire

    @pytest.mark.asyncio
    async def test_backoff_allows_tier3(self):
        fired: list[str] = []

        async def _cb(prompt: str) -> None:
            fired.append(prompt)

        m = _make_monitor(warn=1, nudge=2, topic=3, max_nudges=1, callback=_cb)
        m._last_activity = time.monotonic() - 4

        # First tick: fires tier 3.
        await m._tick()
        assert fired == [IDLE_NEW_TOPIC_PROMPT]
        assert m._consecutive_nudges == 1

        # Reset last_fired_tier to allow re-evaluation.
        m._last_fired_tier = 0
        m._last_activity = time.monotonic() - 4

        # Under backoff (nudges >= max), lower tiers blocked but tier 3 allowed.
        await m._tick()
        assert len(fired) == 2
        assert fired[1] == IDLE_NEW_TOPIC_PROMPT


# ---------------------------------------------------------------------------
# Callback error handling
# ---------------------------------------------------------------------------


class TestCallbackErrors:
    @pytest.mark.asyncio
    async def test_callback_exception_does_not_crash(self):
        async def _bad_cb(prompt: str) -> None:
            raise RuntimeError("oops")

        m = _make_monitor(warn=1, callback=_bad_cb)
        m._last_activity = time.monotonic() - 2

        # Should not raise.
        await m._tick()
        # Tier was not advanced because callback failed.
        assert m._last_fired_tier == 0


# ---------------------------------------------------------------------------
# Prompt constants
# ---------------------------------------------------------------------------


class TestPromptConstants:
    def test_warn_prompt_not_empty(self):
        assert len(IDLE_WARN_PROMPT) > 0

    def test_nudge_prompt_not_empty(self):
        assert len(IDLE_NUDGE_PROMPT) > 0

    def test_new_topic_prompt_not_empty(self):
        assert len(IDLE_NEW_TOPIC_PROMPT) > 0

    def test_prompts_are_distinct(self):
        assert IDLE_WARN_PROMPT != IDLE_NUDGE_PROMPT
        assert IDLE_NUDGE_PROMPT != IDLE_NEW_TOPIC_PROMPT
        assert IDLE_WARN_PROMPT != IDLE_NEW_TOPIC_PROMPT

    def test_opening_prompt_not_empty(self):
        assert len(OPENING_PROMPT) > 0

    def test_opening_prompt_mentions_startup(self):
        assert "Discussion Startup" in OPENING_PROMPT

    def test_opening_prompt_distinct(self):
        assert OPENING_PROMPT != IDLE_WARN_PROMPT
        assert OPENING_PROMPT != IDLE_NUDGE_PROMPT
        assert OPENING_PROMPT != IDLE_NEW_TOPIC_PROMPT


# ---------------------------------------------------------------------------
# _loop integration — exercises the background loop path
# ---------------------------------------------------------------------------


class TestLoop:
    @pytest.mark.asyncio
    async def test_loop_fires_tick_and_can_be_cancelled(self):
        """Patch sleep to 0 so the loop runs fast, then cancel it."""
        fired: list[str] = []

        async def _cb(prompt: str) -> None:
            fired.append(prompt)

        m = _make_monitor(warn=1, nudge=9999, callback=_cb)
        m._last_activity = time.monotonic() - 2  # already idle

        with patch("nanobot.discussion.idle._CHECK_INTERVAL_SEC", 0):
            m._running = True
            task = asyncio.create_task(m._loop())
            await asyncio.sleep(0.05)  # give loop a chance to run
            m._running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert len(fired) >= 1
        assert fired[0] == IDLE_WARN_PROMPT

    @pytest.mark.asyncio
    async def test_loop_handles_tick_exception(self):
        """Exception in _tick should not kill the loop."""

        call_count = 0

        async def _exploding_cb(prompt: str) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("boom")

        m = _make_monitor(warn=1, nudge=9999, callback=_exploding_cb)
        m._last_activity = time.monotonic() - 2

        with patch("nanobot.discussion.idle._CHECK_INTERVAL_SEC", 0):
            m._running = True
            task = asyncio.create_task(m._loop())
            await asyncio.sleep(0.05)
            m._running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # The callback was called at least once. The loop survived the exception.
        assert call_count >= 1

    @pytest.mark.asyncio
    async def test_loop_stops_when_running_set_false(self):
        """Loop exits cleanly when _running is set to False."""
        m = _make_monitor(warn=9999)  # won't fire

        with patch("nanobot.discussion.idle._CHECK_INTERVAL_SEC", 0):
            m._running = True
            task = asyncio.create_task(m._loop())
            await asyncio.sleep(0.02)
            m._running = False
            await asyncio.sleep(0.02)
            # Loop should have exited by now
            assert task.done() or task.cancelled()
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


# ---------------------------------------------------------------------------
# build_opening_prompt
# ---------------------------------------------------------------------------


class TestBuildOpeningPrompt:
    def test_empty_list_returns_opening_prompt(self):
        assert build_opening_prompt([]) == OPENING_PROMPT

    def test_single_participant(self):
        result = build_opening_prompt(["IC-1"])
        assert OPENING_PROMPT in result
        assert "@IC-1" in result
        assert "IMPORTANT" in result

    def test_multiple_participants(self):
        result = build_opening_prompt(["IC-1", "IC-2"])
        assert "@IC-1" in result
        assert "@IC-2" in result
        assert "IMPORTANT" in result

    def test_result_starts_with_opening_prompt(self):
        result = build_opening_prompt(["IC-1"])
        assert result.startswith(OPENING_PROMPT)

    def test_fallback_for_none_equivalent(self):
        # Empty list is the fallback case
        result = build_opening_prompt([])
        assert result == OPENING_PROMPT
        assert "IMPORTANT" not in result
