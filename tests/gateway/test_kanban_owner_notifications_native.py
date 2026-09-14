"""Integrated native watcher proof for bound owner notifications."""

from __future__ import annotations

import asyncio

from hermes_cli import outcomes_db as odb
from gateway.kanban_watchers import _resolve_outcome_owner_wake_spec
from tests.gateway.test_kanban_outcome_owner_wake import (
    _OwnerAdapter,
    _bound_event,
    _inline_to_thread,
    _runner,
)
from tests.gateway.test_kanban_owner_replan import RecordingAdapter, _one_tick, _semantic_fixture
from hermes_cli import kanban_db as kb


def test_native_watcher_preserves_origins_and_sends_one_bound_owner(tmp_path, monkeypatch):
    _, _, _, _, _ = _bound_event(tmp_path, monkeypatch)
    adapter = _OwnerAdapter()
    runner = _runner(adapter)
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    _inline_to_thread(monkeypatch)
    real_sleep = asyncio.sleep

    async def stop_after_tick(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", stop_after_tick)
    asyncio.run(runner._kanban_notifier_owner_loop(interval=0))

    assert {args[0] for args, _ in adapter.sent} == {"origin-one", "origin-two"}
    assert [event.source.chat_id for event in adapter.handled] == ["owner-chat"]


def test_native_owner_wake_does_not_retarget_after_route_revision_change(tmp_path, monkeypatch):
    _, outcome_id, _, task, event = _bound_event(tmp_path, monkeypatch)
    spec = _resolve_outcome_owner_wake_spec("hermes", task, event)
    assert spec is not None
    with odb.connect_closing() as conn:
        odb.update_outcome(conn, outcome_id, current_candidate_ref="replacement")
    adapter = _OwnerAdapter()
    runner = _runner(adapter)
    _inline_to_thread(monkeypatch)
    asyncio.run(runner._deliver_outcome_owner_wakes([spec]))
    assert adapter.handled == []
    with odb.connect_closing() as conn:
        assert odb.list_outcome_owner_wakes(conn, board="hermes")[0]["status"] == "stale"


def test_native_semantic_completion_keeps_passive_notice_and_one_replan(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "semantic.db"))
    kb.init_db()
    with kb.connect() as conn:
        task_id = _semantic_fixture(conn)
    adapter = RecordingAdapter()
    runner = _runner(adapter)
    runner._running = True
    asyncio.run(_one_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert f"continuation_of={task_id}" in adapter.handled[0].text
    assert "topic_target=telegram:-1001:87" in adapter.handled[0].text
