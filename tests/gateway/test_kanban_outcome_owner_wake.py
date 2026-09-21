"""Focused proof for terminal Kanban -> Outcome owner wake delivery."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.kanban_watchers import (
    GatewayKanbanWatchersMixin,
    _resolve_outcome_owner_wake_spec,
)
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import outcomes_db as odb
from hermes_cli import projects_db as pdb


def _bound_event(
    tmp_path: Path,
    monkeypatch,
    *,
    payload=None,
    lane_kind="control",
    lane_outcome_bound=True,
    task_lane_bound=False,
    separate_task_lane=False,
    body=None,
):
    home = tmp_path / "hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    kb.init_db()
    with pdb.connect_closing() as conn:
        project_id = pdb.create_project(
            conn, name="Owner wake", slug="owner-wake",
            primary_path=str(home / "repo"), board_slug="hermes",
        )
    with odb.connect_closing() as conn:
        outcome_id = odb.create_outcome(
            conn, project_id=project_id, outcome_key="OWNER-WAKE",
            name="Owner wake", state="running", visible_owner="owner",
            current_base_ref="base",
        )
        odb.update_outcome(conn, outcome_id, current_candidate_ref="candidate")
        lane_id = odb.bind_conversation_lane(
            conn, project_id=project_id,
            outcome_id=outcome_id if lane_outcome_bound else None,
            platform="telegram", chat_id="owner-chat", thread_id="owner-topic",
            label=lane_kind, lane_kind=lane_kind,
        )
        task_lane_id = lane_id
        if separate_task_lane:
            task_lane_id = odb.bind_conversation_lane(
                conn,
                project_id=project_id,
                platform="telegram",
                chat_id="worker-chat",
                thread_id="worker-topic",
                label="workstream",
                lane_kind="workstream",
            )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="terminal implementation",
            body=body if body is not None else "project_id: " + project_id,
            assignee="worker", project_id=project_id, outcome_id=outcome_id,
            parent_execution_id="ex_impl",
            mutation_repository="example/repo", mutation_scope=["src/**"],
            mutation_base_ref="base",
            conversation_lane_id=task_lane_id if task_lane_bound else None,
        )
        for chat_id in ("origin-one", "origin-two"):
            kb.add_notify_sub(
                conn, task_id=task_id, platform="telegram", chat_id=chat_id,
                thread_id="origin-topic", chat_type="thread",
                notifier_profile="default", delivery_mode="notify+wake",
            )
        kb.complete_task(
            conn, task_id, summary="ready for QA",
            fire_lifecycle_hook=False,
        )
        event = [item for item in kb.list_events(conn, task_id) if item.kind == "completed"][-1]
        if payload is not None:
            event = kb.Event(
                id=event.id, task_id=event.task_id, kind=event.kind,
                payload=payload, created_at=event.created_at, run_id=event.run_id,
            )
        task = kb.get_task(conn, task_id)
    assert task is not None
    return project_id, outcome_id, lane_id, task, event


class _OwnerAdapter:
    supports_async_delivery = True

    def __init__(self, *, fail_once: bool = False):
        self.fail_once = fail_once
        self.handled = []
        self.sent = []

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("owner adapter unavailable")

    async def send(self, *_args, **_kwargs):
        self.sent.append((_args, _kwargs))
        return None


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {"owner": {Platform.TELEGRAM: adapter}}
    runner._active_profile_name = lambda: "default"
    return runner


def _inline_to_thread(monkeypatch):
    async def inline(func, *args):
        return func(*args)

    monkeypatch.setattr(asyncio, "to_thread", inline)


def test_bound_terminal_routes_owner_lane_and_dedupes_replay(tmp_path, monkeypatch):
    _, outcome_id, lane_id, task, event = _bound_event(tmp_path, monkeypatch)
    spec = _resolve_outcome_owner_wake_spec("hermes", task, event)
    assert spec is not None and spec["status"] == "deliver"
    assert spec["route"]["lane_id"] == lane_id
    assert spec["route"]["chat_id"] == "owner-chat"
    assert spec["route"]["thread_id"] == "owner-topic"
    assert outcome_id in spec["prompt"]
    assert "example/repo" in spec["prompt"]
    assert "src/**" in spec["prompt"]

    adapter = _OwnerAdapter()
    runner = _runner(adapter)
    _inline_to_thread(monkeypatch)
    asyncio.run(runner._deliver_outcome_owner_wakes([spec, spec]))

    assert [item.source.chat_id for item in adapter.handled] == ["owner-chat"]
    with odb.connect_closing() as conn:
        rows = odb.list_outcome_owner_wakes(conn, board="hermes")
    assert len(rows) == 1
    assert rows[0]["status"] == "delivered"
    assert rows[0]["attempts"] == 1


@pytest.mark.parametrize("candidate_label", ["exact downstream candidate", "approved", "exact"])
def test_rendered_contract_prose_does_not_override_bound_identity(
    tmp_path, monkeypatch, candidate_label,
):
    body = f"""## Execution contract (authoritative)
Outcome: Close Sol-confirmed owner-wake regression on the downstream candidate.
Owner: DollyCode
Repo/workspace + base revision: example/repo at 12553c0ba3
Candidate: {candidate_label}

## Review contract
Source/base: exact downstream candidate; no runtime activation
"""
    project_id, outcome_id, lane_id, task, event = _bound_event(
        tmp_path, monkeypatch, body=body,
    )

    spec = _resolve_outcome_owner_wake_spec("hermes", task, event)

    assert spec is not None and spec["status"] == "deliver"
    assert spec["project_id"] == project_id
    assert spec["outcome_id"] == outcome_id
    assert spec["route"]["lane_id"] == lane_id


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("outcome_id", "terminal event identity mismatches task binding"),
        ("candidate_ref", "terminal event candidate/base is not current"),
        ("current_candidate_ref", "terminal event candidate/base is not current"),
    ],
)
def test_explicit_body_identity_mismatch_stays_stale(
    tmp_path, monkeypatch, field, reason,
):
    _, _, _, task, event = _bound_event(
        tmp_path, monkeypatch, body=f"{field}: wrong-ref",
    )
    machine_field = _resolve_outcome_owner_wake_spec("hermes", task, event)
    assert machine_field is not None and machine_field["status"] == "stale"
    assert machine_field["reason"] == reason


def test_rendered_outcome_label_does_not_override_bound_identity(tmp_path, monkeypatch):
    _, _, _, task, event = _bound_event(
        tmp_path, monkeypatch, body="Outcome: o_deadbeef",
    )
    canonical_label = _resolve_outcome_owner_wake_spec("hermes", task, event)
    assert canonical_label is not None and canonical_label["status"] == "deliver"


def test_explicit_project_workstream_routes_owner_wake(tmp_path, monkeypatch):
    _, outcome_id, lane_id, task, event = _bound_event(
        tmp_path,
        monkeypatch,
        lane_kind="workstream",
        lane_outcome_bound=False,
        task_lane_bound=True,
    )

    spec = _resolve_outcome_owner_wake_spec("hermes", task, event)

    assert spec is not None and spec["status"] == "deliver"
    assert spec["route"]["lane_id"] == lane_id
    assert spec["route"]["lane_kind"] == "workstream"
    assert spec["route"]["target"] == "telegram:owner-chat:owner-topic"
    assert outcome_id in spec["prompt"]
    adapter = _OwnerAdapter()
    runner = _runner(adapter)
    _inline_to_thread(monkeypatch)
    asyncio.run(runner._deliver_outcome_owner_wakes([spec]))
    assert [item.source.chat_id for item in adapter.handled] == ["owner-chat"]
    assert [item.source.thread_id for item in adapter.handled] == ["owner-topic"]


def test_unique_control_lane_wins_over_explicit_task_lane(tmp_path, monkeypatch):
    _, _, control_lane_id, task, event = _bound_event(
        tmp_path,
        monkeypatch,
        task_lane_bound=True,
        separate_task_lane=True,
    )

    spec = _resolve_outcome_owner_wake_spec("hermes", task, event)

    assert spec is not None and spec["status"] == "deliver"
    assert spec["route"]["lane_id"] == control_lane_id
    assert spec["route"]["lane_kind"] == "control"
    adapter = _OwnerAdapter()
    runner = _runner(adapter)
    _inline_to_thread(monkeypatch)
    asyncio.run(runner._deliver_outcome_owner_wakes([spec]))
    assert [item.source.chat_id for item in adapter.handled] == ["owner-chat"]
    assert [item.source.thread_id for item in adapter.handled] == ["owner-topic"]


def test_multiple_control_lanes_do_not_fall_back_to_explicit_task_lane(
    tmp_path, monkeypatch,
):
    project_id, outcome_id, _, task, event = _bound_event(
        tmp_path,
        monkeypatch,
        task_lane_bound=True,
        separate_task_lane=True,
    )
    with odb.connect_closing() as conn:
        odb.bind_conversation_lane(
            conn,
            project_id=project_id,
            outcome_id=outcome_id,
            platform="telegram",
            chat_id="other-owner-chat",
            thread_id="other-owner-topic",
            label="control",
            lane_kind="control",
        )

    spec = _resolve_outcome_owner_wake_spec("hermes", task, event)

    assert spec is not None and spec["status"] == "noop"
    assert spec["reason"] == "exactly one bound control lane is required"


@pytest.mark.parametrize(
    "invalid_binding",
    ["wrong_project", "wrong_outcome", "mismatched_target", "missing_target"],
)
def test_explicit_owner_route_fails_closed(tmp_path, monkeypatch, invalid_binding):
    project_id, outcome_id, _, task, event = _bound_event(
        tmp_path,
        monkeypatch,
        lane_kind="workstream",
        lane_outcome_bound=False,
        task_lane_bound=True,
    )
    with odb.connect_closing() as conn:
        if invalid_binding == "wrong_project":
            with pdb.connect_closing() as project_conn:
                other_project = pdb.create_project(
                    project_conn,
                    name="Other project",
                    slug="other-project",
                    primary_path=str(tmp_path / "other-repo"),
                    board_slug="hermes",
                )
            task.conversation_lane_id = odb.bind_conversation_lane(
                conn,
                project_id=other_project,
                platform="telegram",
                chat_id="other-chat",
                thread_id="other-topic",
            )
            task.topic_target = "telegram:other-chat:other-topic"
        elif invalid_binding == "wrong_outcome":
            other_outcome = odb.create_outcome(
                conn,
                project_id=project_id,
                outcome_key="OTHER-OUTCOME",
                name="Other outcome",
            )
            task.conversation_lane_id = odb.bind_conversation_lane(
                conn,
                project_id=project_id,
                outcome_id=other_outcome,
                platform="telegram",
                chat_id="other-chat",
                thread_id="other-topic",
            )
            task.topic_target = "telegram:other-chat:other-topic"
        elif invalid_binding == "mismatched_target":
            task.topic_target = "telegram:owner-chat:different-topic"
        else:
            task.topic_target = None

    spec = _resolve_outcome_owner_wake_spec("hermes", task, event)

    assert spec is not None and spec["status"] == "noop"
    assert spec["reason"]
    with odb.connect_closing() as conn:
        receipt = odb.list_outcome_owner_wakes(conn, board="hermes")[0]
    assert receipt["status"] == "noop"
    assert receipt["last_error"] == spec["reason"]


def test_failed_owner_ack_is_retryable_without_duplicate_text(tmp_path, monkeypatch):
    _, _, _, task, event = _bound_event(tmp_path, monkeypatch)
    spec = _resolve_outcome_owner_wake_spec("hermes", task, event)
    assert spec is not None
    adapter = _OwnerAdapter(fail_once=True)
    runner = _runner(adapter)
    _inline_to_thread(monkeypatch)

    asyncio.run(runner._deliver_outcome_owner_wakes([spec]))
    with odb.connect_closing() as conn:
        row = odb.list_outcome_owner_wakes(conn, board="hermes")[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 1

    asyncio.run(runner._deliver_outcome_owner_wakes([spec]))
    with odb.connect_closing() as conn:
        row = odb.list_outcome_owner_wakes(conn, board="hermes")[0]
    assert row["status"] == "delivered"
    assert row["attempts"] == 2
    assert len(adapter.handled) == 2


def test_replay_after_outcome_revision_change_does_not_rewake_owner(tmp_path, monkeypatch):
    _, outcome_id, _, task, event = _bound_event(tmp_path, monkeypatch)
    spec = _resolve_outcome_owner_wake_spec("hermes", task, event)
    assert spec is not None
    adapter = _OwnerAdapter()
    runner = _runner(adapter)
    _inline_to_thread(monkeypatch)

    asyncio.run(runner._deliver_outcome_owner_wakes([spec]))
    with odb.connect_closing() as conn:
        odb.update_outcome(conn, outcome_id, next_action="owner processed event")

    replay = _resolve_outcome_owner_wake_spec("hermes", task, event)
    assert replay is not None and replay["status"] == "deliver"
    asyncio.run(runner._deliver_outcome_owner_wakes([replay]))

    with odb.connect_closing() as conn:
        rows = odb.list_outcome_owner_wakes(conn, board="hermes")
    assert len(rows) == 1
    assert rows[0]["status"] == "delivered"
    assert len(adapter.handled) == 1


def test_bound_event_keeps_passive_origin_notifications(tmp_path, monkeypatch):
    _, _, _, _, _ = _bound_event(tmp_path, monkeypatch)
    adapter = _OwnerAdapter()
    runner = _runner(adapter)
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    _inline_to_thread(monkeypatch)
    real_sleep = asyncio.sleep

    async def tick_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", tick_sleep)
    asyncio.run(runner._kanban_notifier_owner_loop(interval=0))

    assert {args[0] for args, _ in adapter.sent} == {"origin-one", "origin-two"}
    assert [item.source.chat_id for item in adapter.handled] == ["owner-chat"]


def test_stale_revision_and_human_gate_fail_closed(tmp_path, monkeypatch):
    _, outcome_id, _, task, event = _bound_event(tmp_path, monkeypatch)
    stale = _resolve_outcome_owner_wake_spec("hermes", task, event)
    assert stale is not None
    with odb.connect_closing() as conn:
        odb.update_outcome(conn, outcome_id, current_candidate_ref="new-candidate")
    runner = _runner(_OwnerAdapter())
    _inline_to_thread(monkeypatch)
    asyncio.run(runner._deliver_outcome_owner_wakes([stale]))
    with odb.connect_closing() as conn:
        rows = odb.list_outcome_owner_wakes(conn, board="hermes")
    assert rows[0]["status"] == "stale"
    assert not runner.adapters[Platform.TELEGRAM].handled

    _, _, _, task, event = _bound_event(
        tmp_path / "human", monkeypatch, payload={"needs_user_decision": True},
    )
    gated = _resolve_outcome_owner_wake_spec("hermes", task, event)
    assert gated is not None and gated["human_gate"] is True
    assert "typed blocker/manual decision" in gated["prompt"]
    assert "Do not unblock, merge, deploy" in gated["prompt"]
