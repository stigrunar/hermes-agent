"""Gateway /loop command tests — dispatch, routing capture, mid-run guard."""

import logging
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import goals, loops


class _FakeSessionEntry:
    session_id = "sid-gateway-loop"


class _FakeSessionStore:
    def __init__(self):
        self.entry = _FakeSessionEntry()

    def get_or_create_session(self, source, *, touch_activity=True):
        return self.entry

    def _generate_session_key(self, source):
        return "agent:main:discord:channel:loop-test"


@pytest.fixture
def loop_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    # Pre-warm the SessionDB cache from this sync (non-loop) context. Inside
    # the async tests, a cold cache makes GoalManager.set() kick the bounded
    # background bootstrap (loop-thread path) and wait only
    # _DB_BOOTSTRAP_INIT_WAIT_S — on a loaded CI runner the init overruns the
    # window, the goal is never persisted, and the active-goal assertion
    # flakes (main run 33455779041). Warming here removes the race entirely.
    goals._get_session_db()
    yield home
    goals._DB_CACHE.clear()


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}
    return runner


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-loop",
            chat_type="channel",
            thread_id="thread-9",
            user_id="user-loop",
        ),
        message_id="msg-loop",
    )


@pytest.mark.asyncio
async def test_gateway_loop_create_captures_route(loop_env):
    runner = _make_runner()
    response = await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m check the deploy"))
    assert "Loop set" in response
    assert "every 5m" in response

    state = loops.load_loop("sid-gateway-loop")
    assert state is not None
    assert state.prompt == "check the deploy"
    assert state.route["platform"] == "discord"
    assert state.route["chat_id"] == "chat-loop"
    assert state.route["thread_id"] == "thread-9"


@pytest.mark.asyncio
async def test_gateway_loop_status_pause_stop(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    status = await GatewayRunner._handle_loop_command(runner, _make_event("/loop status"))
    assert "poll CI" in status

    paused = await GatewayRunner._handle_loop_command(runner, _make_event("/loop pause"))
    assert "paused" in paused.lower()

    stopped = await GatewayRunner._handle_loop_command(runner, _make_event("/loop stop"))
    assert "stopped" in stopped.lower()


@pytest.mark.asyncio
async def test_gateway_loop_goal_note_when_goal_active(loop_env):
    from hermes_cli.goals import GoalManager

    GoalManager(session_id="sid-gateway-loop").set("finish the migration")
    runner = _make_runner()
    response = await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))
    assert "active /goal" in response


@pytest.mark.asyncio
async def test_post_turn_loop_completion_completes_inflight_tick(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    mgr.state.next_due_at = time.time() - 1
    assert mgr.fire_tick() is not None

    entry = _FakeSessionEntry()
    await GatewayRunner._post_turn_loop_completion(
        runner,
        session_entry=entry,
        source=None,
        final_response="CI is done.\nLOOP_COMPLETE",
    )
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.status == "done"


@pytest.mark.asyncio
async def test_post_turn_completion_preserves_requested_interval_floor(loop_env):
    runner = _make_runner()
    clock = [100.0]
    with patch("hermes_cli.loops.time.time", side_effect=lambda: clock[0]):
        await GatewayRunner._handle_loop_command(
            runner, _make_event("/loop 30m poll CI")
        )
        mgr = loops.LoopManager(session_id="sid-gateway-loop")
        assert mgr.fire_tick() is not None

        # Model a quick gateway turn: completion must not make the next
        # watcher retry legal before the requested 30-minute floor.
        clock[0] = 105.0
        await GatewayRunner._post_turn_loop_completion(
            runner,
            session_entry=_FakeSessionEntry(),
            source=None,
            final_response="still working",
        )

    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded is not None
    assert reloaded.not_before_at == 1900.0
    assert reloaded.next_due_at == 1905.0


@pytest.mark.asyncio
async def test_self_paced_requested_interval_matches_incident_shape(loop_env):
    runner = _make_runner()
    clock = [100.0]
    prompt = "hold fremgang i prosjektene — sjekk hver halvtime"
    with patch("hermes_cli.loops.time.time", side_effect=lambda: clock[0]):
        response = await GatewayRunner._handle_loop_command(
            runner, _make_event(f"/loop --self-paced 30m {prompt}")
        )
        assert "minimum 30m" in response

        mgr = loops.LoopManager(session_id="sid-gateway-loop")
        state = mgr.state
        assert state is not None
        assert state.mode == "self_paced"
        assert state.requested_interval_seconds == 1800.0
        assert mgr.fire_tick() is not None
        clock[0] = 105.0
        await GatewayRunner._post_turn_loop_completion(
            runner,
            session_entry=_FakeSessionEntry(),
            source=None,
            final_response="still working",
        )

    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded is not None
    assert reloaded.not_before_at == 1900.0
    assert reloaded.next_due_at == 1905.0


@pytest.mark.asyncio
async def test_gateway_rejects_unstructured_incident_cadence(loop_env):
    runner = _make_runner()
    response = await GatewayRunner._handle_loop_command(
        runner,
        _make_event("/loop hold fremgang i prosjektene — sjekk hver halvtime"),
    )

    assert "must be explicit" in response
    assert loops.load_loop("sid-gateway-loop") is None


@pytest.mark.asyncio
async def test_gateway_watcher_honors_not_before_on_retry(loop_env):
    runner = _make_runner()
    runner._running = True
    runner._warm_goals_session_db = AsyncMock()
    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    result = loops.dispatch_loop_command(
        mgr,
        "--self-paced 30m hold fremgang i prosjektene — sjekk hver halvtime",
        route={"platform": "discord", "chat_id": "chat-loop"},
    )
    assert result["created"] is True
    state = mgr.state
    assert state is not None
    state.next_due_at = 100.0
    state.not_before_at = 1900.0
    loops.save_loop("sid-gateway-loop", state)

    async def stop_after_scan(delay):
        if delay != 5:
            runner._running = False

    with (
        patch("gateway.run.asyncio.sleep", side_effect=stop_after_scan),
        patch("gateway.run.time.time", return_value=105.0),
        patch(
            "hermes_cli.loops.list_active_loops",
            return_value=[("sid-gateway-loop", state)],
        ),
    ):
        await GatewayRunner._loop_wakeup_watcher(runner, interval=0)

    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded is not None
    assert reloaded.ticks_fired == 0
    assert reloaded.awaiting_response is False


def test_gateway_wakeup_dedupes_same_route_at_same_timestamp():
    parent = loops.LoopState(
        prompt="poll CI",
        next_due_at=100.0,
        not_before_at=100.0,
        created_at=10.0,
        route={
            "platform": "discord",
            "chat_id": "chat-loop",
            "chat_type": "channel",
            "thread_id": "thread-9",
            "user_id": "user-loop",
        },
    )
    child = loops.LoopState.from_json(parent.to_json())

    candidates = GatewayRunner._dedupe_loop_wakeup_candidates(
        [("parent-session", parent), ("z-child-session", child)]
    )

    assert [(sid, state.next_due_at) for sid, state in candidates] == [
        ("z-child-session", 100.0)
    ]


@pytest.mark.asyncio
async def test_post_turn_loop_completion_noop_without_inflight_tick(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))
    entry = _FakeSessionEntry()
    # No tick fired — the ordinary user turn must not consume loop state.
    await GatewayRunner._post_turn_loop_completion(
        runner,
        session_entry=entry,
        source=None,
        final_response="regular reply LOOP_COMPLETE",
    )
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.status == "active"
    assert reloaded.ticks_fired == 0


def test_streamed_already_sent_none_recovers_text_for_hooks():
    """Streamed turns return None. Hooks must still see the delivered reply."""
    event = _make_event("wakeup")
    event._streamed_final_response = "CI is green.\nLOOP_COMPLETE"
    assert GatewayRunner._final_text_for_post_turn_hooks(None, event) == (
        "CI is green.\nLOOP_COMPLETE"
    )
    assert GatewayRunner._final_text_for_post_turn_hooks(None, _make_event("x")) == ""
    assert (
        GatewayRunner._final_text_for_post_turn_hooks(
            {"final_response": "from dict"}, event
        )
        == "from dict"
    )


@pytest.mark.asyncio
async def test_streamed_already_sent_completes_loop_tick(loop_env):
    """A streamed wakeup must not leave awaiting_response stuck."""
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    mgr.state.next_due_at = time.time() - 1
    assert mgr.fire_tick() is not None
    assert mgr.state.awaiting_response is True
    assert mgr.is_due() is False

    event = _make_event("wakeup")
    event._streamed_final_response = "CI is done.\nLOOP_COMPLETE"
    # Same inputs the already_sent branch leaves for _handle_message.
    final_text = GatewayRunner._final_text_for_post_turn_hooks(None, event)
    assert final_text.strip()

    await GatewayRunner._post_turn_loop_completion(
        runner,
        session_entry=_FakeSessionEntry(),
        source=None,
        final_response=final_text,
    )
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.awaiting_response is False
    assert reloaded.status == "done"


@pytest.mark.asyncio
async def test_empty_agent_result_releases_inflight_loop_tick(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    mgr.state.next_due_at = time.time() - 1
    assert mgr.fire_tick() is not None
    assert mgr.state.awaiting_response is True

    runner._post_turn_goal_continuation = AsyncMock()
    await GatewayRunner._run_post_turn_hooks(
        runner,
        agent_result={"final_response": ""},
        source=_make_event("wakeup").source,
        is_internal=True,
    )

    runner._post_turn_goal_continuation.assert_not_awaited()
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.awaiting_response is False
    assert reloaded.status == "active"
    assert reloaded.next_due_at > time.time()


@pytest.mark.asyncio
async def test_goal_hook_failure_does_not_block_loop_completion(loop_env, caplog):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    mgr.state.next_due_at = time.time() - 1
    assert mgr.fire_tick() is not None

    runner._post_turn_goal_continuation = AsyncMock(side_effect=RuntimeError("judge failed"))
    with caplog.at_level(logging.DEBUG, logger="gateway.run"):
        await GatewayRunner._run_post_turn_hooks(
            runner,
            agent_result={"final_response": "still working"},
            source=_make_event("wakeup").source,
            is_internal=True,
        )

    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.awaiting_response is False
    assert "goal continuation hook failed: judge failed" in caplog.text


@pytest.mark.asyncio
async def test_post_turn_session_resolution_failure_is_logged(loop_env, caplog):
    runner = _make_runner()
    runner.session_store.get_or_create_session = Mock(side_effect=RuntimeError("store unavailable"))

    with caplog.at_level(logging.DEBUG, logger="gateway.run"):
        await GatewayRunner._run_post_turn_hooks(
            runner,
            agent_result={"final_response": ""},
            source=_make_event("wakeup").source,
            is_internal=True,
        )

    assert "post-turn session resolution failed: store unavailable" in caplog.text
