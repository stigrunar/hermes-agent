"""Gateway canaries for the durable default-owner replan wake."""
from __future__ import annotations

import asyncio
import json

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    supports_async_delivery = True

    def __init__(self, *, fail_wake: bool = False):
        self.sent = []
        self.handled = []
        self.fail_wake = fail_wake

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata or {}))

    async def handle_message(self, event):
        self.handled.append(event)
        if self.fail_wake:
            raise RuntimeError("owner wake failed")


def _runner(adapter: RecordingAdapter) -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._active_profile_name = lambda: "default"
    runner._session_key_for_source = lambda _source: "owner-session"
    return runner


async def _one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    async def inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    await runner._kanban_notifier_owner_loop(interval=1)


def _terminal_fixture(conn):
    tid = kb.create_task(
        conn,
        title="terminal owner wake",
        body="contract_id: owner-wake\nrevision: r1",
        assignee="dollycode",
        tenant="owner-project",
        workspace_kind="dir",
        workspace_path="/tmp/owner-wake-artifact",
        max_retries=0,
    )
    claimed = kb.claim_task(conn, tid, claimer="terminal-worker")
    assert claimed is not None and claimed.current_run_id is not None
    kb.add_notify_sub(
        conn,
        task_id=tid,
        platform="telegram",
        chat_id="owner-chat",
        chat_type="group",
        thread_id="17",
        notifier_profile="default",
    )
    kb._record_iteration_exhaustion(conn, tid, budget_used=60, budget_max=60)
    return tid


def _semantic_fixture(conn):
    tid = kb.create_task(
        conn,
        title="semantic owner wake",
        body="contract_id: semantic-owner\nrevision: r1\n"
        "topic_target: telegram:-1001:87",
        assignee="dollycode",
        tenant="owner-project",
        project_id="adopted-project",
        workspace_kind="dir",
        workspace_path="/tmp/semantic-owner-artifact",
        max_retries=0,
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET project_id=? WHERE id=?", ("adopted-project", tid))
    claimed = kb.claim_task(conn, tid, claimer="semantic-worker")
    assert claimed is not None and claimed.current_run_id is not None
    kb.add_notify_sub(
        conn,
        task_id=tid,
        platform="telegram",
        chat_id="owner-chat",
        chat_type="group",
        thread_id="17",
        notifier_profile="default",
        delivery_mode="notify+wake",
    )
    assert kb.complete_task(
        conn,
        tid,
        expected_run_id=claimed.current_run_id,
        summary="useful preserved patch",
        metadata={
            "owner_replan": {
                "owner": "default",
                "action": "inspect the preserved patch",
                "authority": "agent_internal",
                "needs_user_decision": False,
            },
        },
    )
    return tid


def _control(conn, tid, kind):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
        (tid, kind),
    ).fetchall()
    return [json.loads(row["payload"] or "{}") for row in rows]


def test_owner_replan_wakes_default_once_and_duplicate_tick_is_quiet(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "owner.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = _terminal_fixture(conn)
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.handled) == 1
    assert "HERMES OWNER REPLAN" in adapter.handled[0].text
    assert "Do not unblock or retry" in adapter.handled[0].text
    assert len(adapter.sent) == 1

    adapter2 = RecordingAdapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter2)))
    assert adapter2.handled == []
    conn = kb.connect()
    try:
        assert len(_control(conn, tid, "owner_replan_wake_claimed")) == 1
        assert len(_control(conn, tid, "owner_replan_delivered")) == 1
    finally:
        conn.close()


def test_semantic_completion_keeps_passive_notice_but_suppresses_generic_wake(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "semantic-owner.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = _semantic_fixture(conn)
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    prompt = adapter.handled[0].text
    assert "action: inspect the preserved patch" in prompt
    assert f"continuation_of={tid}" in prompt
    assert "project_id=adopted-project" in prompt
    assert "topic_target=telegram:-1001:87" in prompt
    assert "Do not unblock or retry" in prompt

    conn = kb.connect()
    try:
        assert len(_control(conn, tid, "owner_replan_delivered")) == 1
        assert _control(conn, tid, "owner_replan_wake_claimed")
    finally:
        conn.close()


def test_owner_replan_wake_failure_is_manual_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "owner-fail.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = _terminal_fixture(conn)
    finally:
        conn.close()
    adapter = RecordingAdapter(fail_wake=True)
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.handled) == 1

    adapter2 = RecordingAdapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter2)))
    assert adapter2.handled == []
    conn = kb.connect()
    try:
        failures = _control(conn, tid, "owner_replan_failed")
        assert len(failures) == 1
        assert failures[0]["resume_policy"] == "manual"
        assert failures[0]["retryable"] is False
    finally:
        conn.close()


def test_interrupted_owner_claim_is_not_rewoken(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "owner-interrupted.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = _terminal_fixture(conn)
    finally:
        conn.close()
    conn = kb.connect()
    try:
        claimed = kb.claim_owner_replan_for_route(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="owner-chat",
            thread_id="17",
        )
        assert claimed is not None
    finally:
        conn.close()
    adapter = RecordingAdapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert adapter.handled == []
    conn = kb.connect()
    try:
        assert len(_control(conn, tid, "owner_replan_failed")) == 1
        assert len(_control(conn, tid, "owner_replan_wake_claimed")) == 1
    finally:
        conn.close()
