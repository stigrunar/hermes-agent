"""End-to-end liveness checks for the Codex app-server notification bridge."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

from agent.activity_tracking import ActivityTrackingMixin
from agent import turn_liveness
from agent.transports.codex_app_server_session import CodexAppServerSession, TurnResult


class _ActivityAgent(ActivityTrackingMixin):
    session_id = None
    _session_db = None


def _watchdog(agent, *, active, commits):
    return turn_liveness.TurnLivenessWatchdog(
        agent,
        session_id="session-1",
        timeout_s=600.0,
        poll_s=0.01,
        stop_event=threading.Event(),
        activity_lock=agent._liveness_activity_lock(),
        is_turn_active=lambda: active[0],
        commit_abort=lambda snapshot, message: commits.append((snapshot, message)) or True,
        deactivate_turn=lambda: active.__setitem__(0, False),
    )


def _record_current_progress(agent, note):
    session = CodexAppServerSession(on_activity=agent._touch_activity)
    session._thread_id = "thread-1"
    result = TurnResult(thread_id="thread-1", turn_id="turn-1")
    return session._record_notification_activity(note, result)


def test_native_progress_keeps_the_real_activity_clock_under_600_seconds():
    agent = _ActivityAgent()
    agent._touch_activity("starting new turn")
    before_generation = agent._turn_liveness_activity_generation

    assert _record_current_progress(agent, {
        "method": "item/agentMessage/delta",
        "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "working"},
    }) is True

    assert agent._turn_liveness_activity_generation == before_generation + 1
    active = [True]
    commits = []
    watchdog = _watchdog(agent, active=active, commits=commits)
    with patch.object(turn_liveness.time, "time", return_value=agent._last_activity_ts + 599.999):
        assert watchdog._tick() is None
    assert commits == []
    assert active == [True]


def test_silence_at_the_unchanged_600_second_boundary_commits_abort_and_deactivates():
    agent = _ActivityAgent()
    agent._turn_liveness_activity_generation = 7
    agent._last_activity_ts = 1000.0
    active = [True]
    commits = []
    watchdog = _watchdog(agent, active=active, commits=commits)

    with patch.object(turn_liveness.time, "time", return_value=1600.0):
        assert watchdog._tick() is False

    assert len(commits) == 1
    snapshot, message = commits[0]
    assert snapshot.generation == 7
    assert snapshot.activity_ts == 1000.0
    assert snapshot.idle_seconds == 600.0
    assert "release the session" in message
    assert active == [False]


def test_activity_bridge_is_independent_of_missing_display_callbacks():
    activity = []
    agent = SimpleNamespace(_touch_activity=activity.append)

    assert _record_current_progress(agent, {
        "method": "item/commandExecution/outputDelta",
        "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "stdout"},
    }) is True

    assert activity == ["codex app-server progress: item/commandExecution/outputDelta"]
