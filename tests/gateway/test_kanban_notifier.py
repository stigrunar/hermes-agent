import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest


from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    lock_handle = object()
    with (
        patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            return_value=(lock_handle, "held"),
        ),
        patch("gateway.kanban_watchers._release_singleton_lock"),
        patch("gateway.kanban_watchers.asyncio.to_thread", side_effect=fake_to_thread),
    ):
        await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
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


def test_notifier_delivers_with_dispatch_disabled(tmp_path, monkeypatch):
    """Notifier ownership does not enable or depend on embedded dispatch."""
    db_path = tmp_path / "notifier-only.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "false")
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    with (
        patch(
            "hermes_cli.config.load_config",
            return_value={
                "kanban": {
                    "dispatch_in_gateway": False,
                    "notify_in_gateway": True,
                }
            },
        ),
        patch.object(kb, "dispatch_once") as dispatch_once,
    ):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]
    dispatch_once.assert_not_called()
    assert runner._kanban_notifier_lock_handle is None


def test_subscription_after_terminal_history_starts_from_now(tmp_path, monkeypatch):
    db_path = tmp_path / "from-now.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="already finished", assignee="worker")
        kb.complete_task(conn, tid, summary="historical completion")
        audit_rows = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?", (tid,)
        ).fetchone()[0]
        task_max = conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id=?", (tid,)
        ).fetchone()[0]
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
        )
        sub = kb.list_notify_subs(conn, tid)[0]
        assert sub["last_event_id"] == task_max
        assert sub["baseline_event_id"] == task_max
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert adapter.handled == []
    conn = kb.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?", (tid,)
        ).fetchone()[0] == audit_rows
        assert kb.list_notify_subs(conn, tid)[0]["last_event_id"] == task_max
    finally:
        conn.close()


def test_post_baseline_event_delivers_once_after_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "post-baseline-restart.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="restart delivery",
            assignee="worker",
            session_id="session-1",
        )
        kb._append_event(conn, tid, kind="crashed")
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
        )
    finally:
        conn.close()

    # The notifier is down while this genuinely new event is appended.
    conn = kb.connect()
    try:
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()
    assert len(adapter.handled) == 1


def test_notifier_baselines_legacy_rows_across_multiple_boards(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    task_ids = []
    for board in ("work-dev", "personal-dev"):
        kb.create_board(board)
        db_path = kb.kanban_db_path(board=board)
        conn = kb.connect(board=board)
        try:
            tid = kb.create_task(
                conn, title=f"{board} notification", assignee="worker",
            )
            task_ids.append(tid)
            kb._append_event(conn, tid, kind="crashed")
            # Simulate a subscription row written before baseline_event_id
            # existed. The next connect must baseline it once per board.
            conn.execute(
                "INSERT INTO kanban_notify_subs "
                "(task_id, platform, chat_id, created_at, last_event_id) "
                "VALUES (?, 'telegram', 'chat-1', 1000, 0)",
                (tid,),
            )
        finally:
            conn.close()
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert adapter.sent == []
    assert adapter.handled == []

    conn = kb.connect(board="work-dev")
    try:
        kb._append_event(conn, task_ids[0], kind="crashed")
    finally:
        conn.close()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    delivered = adapter.sent[0]["text"]
    assert task_ids[0] in delivered
    assert task_ids[1] not in delivered
    assert "[work-dev]" in delivered


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


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


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]

    recovered = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(recovered)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(recovered)))
    assert len(recovered.sent) == 1
    assert recovered.handled == []
    assert _unseen_terminal_events(tid) == []


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


def test_notifier_owning_profile_adapter_no_default_fallback(tmp_path, monkeypatch):
    """A subscription owned by a secondary profile whose profile-adapter
    registry entry EXISTS but lacks this platform must NOT fall back to the
    default profile's same-platform adapter — the notifier must route through
    the shared ``_authorization_adapter`` chokepoint, which forbids that
    fallback (gateway/authz_mixin.py). Delivering via the default profile's bot
    is the exact cross-profile mis-delivery this whole change exists to fix
    (`[230002] Bot can NOT be out of the chat`).

    Mutation check: reverting kanban_watchers.py's adapter selection to the old
    inline ``if adapter is None: adapter = self.adapters.get(plat)`` fallback
    makes this test FAIL (the default adapter receives the delivery).
    """
    db_path = tmp_path / "profile-no-fallback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned by beta", assignee="worker")
        # Subscription is owned by profile "beta".
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-beta",
            notifier_profile="beta",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    default_adapter = RecordingAdapter()
    other_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    # Default profile has a telegram adapter …
    runner.adapters = {Platform.TELEGRAM: default_adapter}
    # … and profile "beta" HAS a non-empty registry entry (so it passes the
    # notifier's upstream skip-filter, which only skips owning profiles with NO
    # adapter at all), but that entry does NOT contain a telegram adapter — beta
    # connected a different platform (discord). The telegram sub owned by beta
    # must therefore resolve to NO adapter, not silently borrow the default
    # profile's telegram bot.
    runner._profile_adapters = {"beta": {Platform.DISCORD: other_adapter}}
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The default profile's adapter must never receive beta's notification.
    assert default_adapter.sent == [], (
        "Owning-profile subscription must not fall back to the default "
        f"profile's adapter; got {default_adapter.sent!r}"
    )
    assert other_adapter.sent == [], (
        f"beta's discord adapter must not receive a telegram sub; got {other_adapter.sent!r}"
    )
    # The claim is rewound (adapter resolved to None → treated as disconnected),
    # so the event is still unseen and will deliver once beta's adapter connects.
    assert [ev.kind for ev in _unseen_terminal_events_for(tid, "chat-beta")] == ["completed"]


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
