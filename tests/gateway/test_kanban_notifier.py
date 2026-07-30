import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest


from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb

_REAL_ADD_NOTIFY_SUB = kb.add_notify_sub


def _add_notify_sub(conn, **kwargs):
    kwargs.setdefault("notifier_profile", "default")
    kwargs.setdefault("chat_type", "group")
    kwargs.setdefault("session_key", "test-session")
    return _REAL_ADD_NOTIFY_SUB(conn, **kwargs)


@pytest.fixture(autouse=True)
def _stamp_default_profile_on_legacy_test_fixtures(monkeypatch):
    """Keep old fixtures routable while dedicated tests cover NULL fail-close."""
    monkeypatch.setattr(kb, "add_notify_sub", _add_notify_sub)


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})
        return SendResult(success=True, message_id="test-message-id")

    async def handle_message(self, event):
        self.handled.append(event)

    async def handle_message(self, event):
        self.handled.append(event)


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner, *, profile="default"):
    real_sleep = asyncio.sleep

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def fake_sleep(_delay):
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with patch(
        "gateway.kanban_watchers.asyncio.to_thread", side_effect=fake_to_thread,
    ):
        await runner._kanban_notifier_owner_loop(
            interval=1,
            notifier_profile=profile,
        )


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._active_profile_name = lambda: "default"
    runner._session_key_for_source = lambda _source: "test-session"
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_replays_telegram_dm_topic_delivery_metadata(tmp_path, monkeypatch):
    db_path = tmp_path / "dm-topic-metadata.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm topic task",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            thread_id="20197",
            delivery_metadata={
                "chat_type": "dm",
                "direct_messages_topic_id": "20197",
                "telegram_dm_topic_reply_fallback": True,
                "telegram_reply_to_message_id": "462",
                "thread_id": "20197",
            },
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"] == {
        "chat_type": "dm",
        "direct_messages_topic_id": "20197",
        "telegram_dm_topic_reply_fallback": True,
        "telegram_reply_to_message_id": "462",
        "thread_id": "20197",
    }
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"
    assert adapter.handled[0].source.thread_id == "20197"


def test_active_named_profile_subscription_is_delivered(tmp_path, monkeypatch):
    """A sub stamped with the gateway's own named profile uses self.adapters.

    Regression for #71340: on a standalone (non-multiplex) gateway running a
    named profile, _authorization_adapter() used to treat the active name as a
    multiplex secondary, find no _profile_adapters entry, fail closed, and
    rewind the claim forever — silent zero-delivery.
    """
    db_path = tmp_path / "actionable-block.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    reason = "AGE-39 — https://linear.example/AGE-39 — publishing verified."
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="approval", assignee="publisher")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="main",
        )
        kb.block_task(conn, tid, reason=reason, kind="needs_input")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "main"

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    message = adapter.sent[0]["text"]
    assert tid in message
    assert "blocked" in message


def test_terminal_completion_wins_over_historical_failures(tmp_path, monkeypatch):
    db_path = tmp_path / "terminal-completion-wins.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="eventually succeeds",
            assignee="worker",
            session_id="session-1",
        )
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.block_task(conn, tid, reason="historical blocker", kind="needs_input")
        kb._append_event(
            conn,
            tid,
            kind="gave_up",
            payload={"error": "historical retries exhausted"},
        )
        kb.complete_task(conn, tid, summary="canonical success")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert "done" in adapter.sent[0]["text"].lower()
    assert "canonical success" in adapter.sent[0]["text"]
    assert "blocked" not in adapter.sent[0]["text"].lower()
    assert "gave up" not in adapter.sent[0]["text"].lower()
    assert len(adapter.handled) == 1
    wake = adapter.handled[0].text.lower()
    assert "completed" in wake
    assert "blocked" not in wake
    assert "gave up" not in wake

    conn = kb.connect()
    try:
        event_kinds = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                (tid,),
            ).fetchall()
        ]
    finally:
        conn.close()
    assert "blocked" in event_kinds
    assert "gave_up" in event_kinds
    assert "completed" in event_kinds


def test_terminal_task_ignores_delayed_historical_failures(tmp_path, monkeypatch):
    db_path = tmp_path / "terminal-delayed-history.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="already complete",
            assignee="worker",
            session_id="session-1",
        )
        kb.complete_task(conn, tid, summary="finished before delayed events")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        completed_cursor = int(
            conn.execute(
                "SELECT MAX(id) AS id FROM task_events WHERE task_id=?",
                (tid,),
            ).fetchone()["id"]
        )
        kb.advance_notify_cursor(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            new_cursor=completed_cursor,
        )
        kb._append_event(
            conn,
            tid,
            kind="blocked",
            payload={"reason": "delayed old blocker"},
        )
        kb._append_event(
            conn,
            tid,
            kind="gave_up",
            payload={"error": "delayed old retry failure"},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert adapter.handled == []
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, tid) == []
        delayed_kinds = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? AND id>? ORDER BY id",
                (tid, completed_cursor),
            ).fetchall()
        ]
    finally:
        conn.close()
    assert delayed_kinds == ["blocked", "gave_up"]


def test_terminal_task_ignores_delayed_historical_completion(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "terminal-delayed-completion.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="completed after retry",
            assignee="worker",
            session_id="session-1",
        )
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")

        old_claim = kb.claim_task(conn, tid, claimer="old-worker")
        assert old_claim is not None
        old_run_id = old_claim.current_run_id
        assert old_run_id is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="old completion",
            expected_run_id=old_run_id,
        )

        conn.execute(
            "UPDATE tasks SET status='ready', completed_at=NULL WHERE id=?",
            (tid,),
        )
        current_claim = kb.claim_task(conn, tid, claimer="current-worker")
        assert current_claim is not None
        current_run_id = current_claim.current_run_id
        assert current_run_id is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="current completion",
            expected_run_id=current_run_id,
        )
        assert kb.latest_run(conn, tid).id == current_run_id

        current_completion_cursor = int(
            conn.execute(
                "SELECT MAX(id) AS id FROM task_events WHERE task_id=?",
                (tid,),
            ).fetchone()["id"]
        )
        kb.advance_notify_cursor(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            new_cursor=current_completion_cursor,
        )
        kb._append_event(
            conn,
            tid,
            kind="completed",
            payload={"summary": "delayed old completion"},
            run_id=old_run_id,
        )
        delayed_event_id = int(
            conn.execute(
                "SELECT MAX(id) AS id FROM task_events WHERE task_id=?",
                (tid,),
            ).fetchone()["id"]
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    with patch.object(
        runner, "_kanban_advance", wraps=runner._kanban_advance,
    ) as advance:
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.handled == []
    advance.assert_called_once()
    assert advance.call_args.args[1] == delayed_event_id
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, tid) == []
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("current_kind", "expected_text"),
    [("blocked", "blocked"), ("gave_up", "gave up")],
)
def test_current_failure_still_notifies_and_wakes(
    current_kind, expected_text, tmp_path, monkeypatch,
):
    db_path = tmp_path / f"current-{current_kind}.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title=f"currently {current_kind}",
            assignee="worker",
            session_id="session-1",
        )
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        if current_kind == "blocked":
            kb.block_task(conn, tid, reason="current blocker", kind="needs_input")
        else:
            kb._record_task_failure(
                conn,
                tid,
                "current retries exhausted",
                outcome="spawn_failed",
                force_trip=True,
                release_claim=True,
                end_run=True,
            )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert expected_text in adapter.sent[0]["text"].lower()
    assert len(adapter.handled) == 1
    assert expected_text in adapter.handled[0].text.lower()
    conn = kb.connect()
    try:
        assert len(kb.list_notify_subs(conn, tid)) == 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("case_name", "metadata"),
    [
        ("notification-board-hygiene", {"notification": {"kind": "board_hygiene"}}),
        ("notification-superseded", {"notification": {"kind": "superseded"}}),
        ("closure-class-board-hygiene", {"closure_class": "board_hygiene"}),
        (
            "closure-class-superseded-by-replacement",
            {"closure_class": "superseded_by_replacement"},
        ),
        (
            "closure-class-duplicate-superseded",
            {"closure_class": "duplicate_superseded"},
        ),
    ],
    ids=[
        "notification-board-hygiene",
        "notification-superseded",
        "closure-class-board-hygiene",
        "closure-class-superseded-by-replacement",
        "closure-class-duplicate-superseded",
    ],
)
def test_structured_hygiene_completion_is_silent(
    case_name, metadata, tmp_path, monkeypatch,
):
    db_path = tmp_path / f"{case_name}.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="continuity closeout",
            assignee="worker",
            session_id="session-1",
        )
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(
            conn,
            tid,
            summary="close duplicate continuity card",
            metadata=metadata,
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert adapter.handled == []
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, tid) == []
        completed_events = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                (tid,),
            ).fetchall()
            if row["kind"] == "completed"
        ]
    finally:
        conn.close()
    assert completed_events == ["completed"]


def test_actionable_completion_prose_and_metadata_still_surface(tmp_path, monkeypatch):
    db_path = tmp_path / "actionable-completion.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="real product follow-up",
            assignee="worker",
            session_id="session-1",
        )
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(
            conn,
            tid,
            summary="Superseded one approach; changes requested remain actionable",
            metadata={"closure_class": "changes_requested_no_deploy"},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert "changes requested remain actionable" in adapter.sent[0]["text"].lower()
    assert len(adapter.handled) == 1
    assert "completed" in adapter.handled[0].text.lower()


def test_superseded_terminal_status_silences_delayed_failures(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "superseded-terminal.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="duplicate continuity card",
            assignee="worker",
            session_id="session-1",
        )
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(
            conn,
            tid,
            kind="blocked",
            payload={"reason": "historical blocker"},
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='superseded' WHERE id=?",
                (tid,),
            )
            kb._append_event(
                conn,
                tid,
                kind="status",
                payload={"status": "superseded"},
            )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert adapter.handled == []
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, tid) == []
    finally:
        conn.close()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


class ReportedFailureAdapter:
    """Adapter that REPORTS failure via SendResult(success=False) instead of
    raising — the exact contract the Telegram adapter uses for 'Not connected'
    and degraded-send paths."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        from gateway.platforms.base import SendResult
        return SendResult(success=False, error="Not connected")

    recovered = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(recovered)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(recovered)))
    assert len(recovered.sent) == 1
    assert len(recovered.handled) == 1
    assert _unseen_terminal_events(tid) == []


def test_kanban_notifier_rewinds_nonthrowing_unsuccessful_send_result(
    tmp_path, monkeypatch,
):
    """An unsuccessful SendResult is a delivery failure, not an acknowledgement."""
    db_path = tmp_path / "unsuccessful-send-result.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = UnsuccessfulAdapter()
    runner = _make_runner(adapter)
    with (
        patch.object(runner, "_kanban_advance", wraps=runner._kanban_advance) as advance,
        patch.object(runner, "_kanban_unsub", wraps=runner._kanban_unsub) as unsub,
        patch.object(runner, "_kanban_rewind", wraps=runner._kanban_rewind) as rewind,
    ):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.attempts == 1
    assert adapter.handled == []
    advance.assert_not_called()
    unsub.assert_not_called()
    rewind.assert_called_once()
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]
    conn = kb.connect()
    try:
        assert len(kb.list_notify_subs(conn, tid)) == 1
    finally:
        conn.close()


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_notifier_wakeup_uses_subscription_chat_type(tmp_path, monkeypatch):
    db_path = tmp_path / "chat-type-wakeup.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm requester",
            assignee="worker",
            session_id="origin-session",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-dm",
            chat_type="dm",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"

    # The wake must resume the creator's real DM session key — the whole bug
    # was that a hardcoded chat_type="group" made build_session_key() produce
    # a group-scoped key (a NEW session) instead of the ":dm:<chat_id>" shape
    # the original conversation runs under (#56580 / #68874).
    from gateway.session import build_session_key

    wake_key = build_session_key(adapter.handled[0].source)
    assert wake_key == "agent:main:telegram:dm:chat-dm"
    assert ":group:" not in wake_key


def test_notifier_refuses_unstamped_legacy_subscription(tmp_path, monkeypatch):
    db_path = tmp_path / "unstamped-profile.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="ambiguous legacy route", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile=None,
        )
        kb.complete_task(conn, tid, summary="must not route")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert adapter.handled == []
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_refuses_wake_when_persisted_session_key_mismatches(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "mismatched-session.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="wrong wake target", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="default",
            chat_type="group",
            session_key="different-session",
        )
        kb.complete_task(conn, tid, summary="notification only")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert adapter.handled == []
    assert _unseen_terminal_events(tid) == []


def test_notifier_missing_chat_type_sends_without_creating_wake_session(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "missing-chat-type.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy delivery only", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="8099892548",
            notifier_profile="default",
            chat_type="",
            session_key="agent:main:telegram:dm:8099892548",
        )
        kb.complete_task(conn, tid, summary="notification only")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert adapter.handled == []


def test_notify_subscription_migration_is_idempotent_and_preserves_legacy_row(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "legacy-notify-schema.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    legacy = sqlite3.connect(db_path)
    try:
        legacy.execute(
            """
            CREATE TABLE kanban_notify_subs (
                task_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                thread_id TEXT NOT NULL DEFAULT '',
                user_id TEXT,
                notifier_profile TEXT,
                created_at INTEGER NOT NULL,
                last_event_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (task_id, platform, chat_id, thread_id)
            )
            """
        )
        legacy.execute(
            """
            INSERT INTO kanban_notify_subs
                (task_id, platform, chat_id, created_at, last_event_id)
            VALUES ('t_legacy', 'telegram', '8099892548', 1, 7)
            """
        )
        legacy.commit()
    finally:
        legacy.close()

    kb.init_db()
    kb.init_db()
    conn = kb.connect()
    try:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(kanban_notify_subs)")
        }
        assert {
            "chat_type", "notifier_profile", "session_key",
            "delivery_metadata", "baseline_event_id",
        } <= columns
        [legacy_sub] = kb.list_notify_subs(conn, "t_legacy")
    finally:
        conn.close()

    assert legacy_sub["last_event_id"] == 7
    assert legacy_sub["notifier_profile"] is None
    assert legacy_sub["chat_type"] is None
    assert legacy_sub["session_key"] is None
    assert legacy_sub["delivery_metadata"] == {}


def test_project_topic_provenance_survives_create_inherit_delivery_and_wake(
    tmp_path, monkeypatch,
):
    from gateway.session import SessionSource, build_session_key

    db_path = tmp_path / "project-topic.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1003828321118",
        chat_type="group",
        thread_id="20211",
        user_id="8099892548",
        profile="default",
    )
    session_key = build_session_key(source, profile="default")
    metadata = {"chat_type": "group", "thread_id": "20211"}

    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="project parent", assignee="default")
        kb.add_notify_sub(
            conn,
            task_id=parent,
            platform="telegram",
            chat_id="-1003828321118",
            chat_type="group",
            thread_id="20211",
            user_id="8099892548",
            notifier_profile="default",
            session_key=session_key,
            delivery_metadata=metadata,
        )
        child = kb.create_task(
            conn,
            title="project child",
            assignee="worker",
            parents=(parent,),
        )
        [inherited] = kb.list_notify_subs(conn, child)
        assert {
            key: inherited[key]
            for key in (
                "platform", "chat_id", "chat_type", "thread_id", "user_id",
                "notifier_profile", "session_key", "delivery_metadata",
            )
        } == {
            "platform": "telegram",
            "chat_id": "-1003828321118",
            "chat_type": "group",
            "thread_id": "20211",
            "user_id": "8099892548",
            "notifier_profile": "default",
            "session_key": session_key,
            "delivery_metadata": metadata,
        }
        kb.remove_notify_sub(
            conn,
            task_id=parent,
            platform="telegram",
            chat_id="-1003828321118",
            thread_id="20211",
        )
        kb.complete_task(conn, parent, summary="dependency done")
        kb.recompute_ready(conn)
        kb.complete_task(conn, child, summary="child done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._session_key_for_source = lambda source: build_session_key(
        source,
        profile=source.profile,
    )
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "-1003828321118"
    assert adapter.sent[0]["metadata"] == metadata
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "group"
    assert adapter.handled[0].source.thread_id == "20211"
    assert adapter.handled[0].source.profile == "default"


def test_four_profile_workers_route_only_owned_rows_without_duplicates(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "four-profiles.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    profiles = ("default", "dollydesign", "dollyops", "dollyprivate")

    conn = kb.connect()
    try:
        for profile in profiles:
            tid = kb.create_task(
                conn,
                title=f"owned by {profile}",
                assignee=profile,
            )
            kb.add_notify_sub(
                conn,
                task_id=tid,
                platform="telegram",
                chat_id=f"chat-{profile}",
                notifier_profile=profile,
                chat_type="group",
                session_key="",
            )
            kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    runners = []
    adapters = {}
    for profile in profiles:
        adapter = RecordingAdapter()
        runner = _make_runner(adapter)
        runner._active_profile_name = lambda profile=profile: profile
        runners.append(runner)
        adapters[profile] = adapter

    real_sleep = asyncio.sleep

    async def finish_after_first_tick(_delay):
        for runner in runners:
            runner._running = False
        await real_sleep(0)

    async def run_all():
        await asyncio.gather(*(
            runner._kanban_notifier_owner_loop(
                interval=1,
                notifier_profile=profile,
            )
            for runner, profile in zip(runners, profiles)
        ))

    monkeypatch.setattr(asyncio, "sleep", finish_after_first_tick)
    asyncio.run(run_all())

    for profile in profiles:
        assert [item["chat_id"] for item in adapters[profile].sent] == [
            f"chat-{profile}"
        ]
        assert adapters[profile].handled == []

    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn) == []
    finally:
        conn.close()


def _unseen_terminal_events_for(tid, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_isolates_per_subscription_failure(tmp_path, monkeypatch):
    """One bad subscription must not block delivery for all others.

    Regression for #59269: when claim_unseen_events_for_sub raises for one
    subscription, the entire notifier tick used to abort — silently blocking
    delivery for every other subscription.
    """
    db_path = tmp_path / "isolation.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    # Create two tasks with subscriptions and complete both. The BAD task is
    # created first: list_notify_subs() has no ORDER BY, so SQLite's natural
    # scan returns insertion order — the failing subscription must be
    # processed BEFORE the good one or this test passes even without the
    # per-subscription isolation (the good delivery happens before the tick
    # aborts). A deterministic-order shim below removes the reliance on the
    # scan order entirely.
    conn = kb.connect()
    try:
        tid_bad = kb.create_task(conn, title="bad task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid_bad, platform="telegram", chat_id="chat-bad")
        kb.complete_task(conn, tid_bad, summary="done")

        tid_good = kb.create_task(conn, title="good task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid_good, platform="telegram", chat_id="chat-good")
        kb.complete_task(conn, tid_good, summary="done")
    finally:
        conn.close()

    original_claim = kb.claim_unseen_events_for_sub

    def selective_claim(conn, task_id, **kwargs):
        if task_id == tid_bad:
            raise RuntimeError("simulated DB corruption for bad task")
        return original_claim(conn, task_id=task_id, **kwargs)

    monkeypatch.setattr(kb, "claim_unseen_events_for_sub", selective_claim)

    # Force the failing subscription to be iterated FIRST regardless of the
    # unordered SELECT's scan order.
    original_list = kb.list_notify_subs

    def bad_first(conn, task_id=None):
        subs = original_list(conn, task_id)
        return sorted(subs, key=lambda s: 0 if s["task_id"] == tid_bad else 1)

    monkeypatch.setattr(kb, "list_notify_subs", bad_first)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The good task must still be delivered despite the bad task failing.
    assert len(adapter.sent) == 1
    assert tid_good in adapter.sent[0]["text"]


def test_notifier_delivers_block_loop_detected_triage_ping(tmp_path, monkeypatch):
    """A `block_loop_detected` event must reach the subscriber as a triage ping.

    Regression for the silent-triage gap (PR #62712): kanban_db routes a task
    to `triage` after BLOCK_RECURRENCE_LIMIT re-blocks for the same cause and
    emits ONLY a `block_loop_detected` event — no `blocked`/`status` event.
    Before `block_loop_detected` joined TERMINAL_KINDS with its own message
    branch, that one transition (the whole point of which is to force human
    attention) produced zero notification and the task stalled in triage
    silently.
    """
    db_path = tmp_path / "block-loop.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="loops forever", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(
            conn, tid, "block_loop_detected",
            {"reason": "needs credentials", "kind": "needs_input",
             "recurrences": 2, "limit": kb.BLOCK_RECURRENCE_LIMIT},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "block_loop_detected must produce a notification"
    text = adapter.sent[0]["text"]
    assert "TRIAGE" in text
    assert tid in text
    assert "needs credentials" in text
    # Cursor advanced: the event is claimed and not re-delivered.
    conn = kb.connect()
    try:
        _, remaining = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["block_loop_detected"],
        )
    finally:
        conn.close()
    assert remaining == []
