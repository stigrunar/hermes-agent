from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import outcomes_db as odb
from hermes_cli import projects_db as pdb


@pytest.fixture
def stores(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(odb, "cross_project_orchestration_enabled", lambda: True)
    repo = tmp_path / "repo"
    repo.mkdir()
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(pc, name="Prosjektstyring", folders=[str(repo)])
        project = pdb.get_project(pc, pid)
    with odb.connect_closing() as oc:
        oid = odb.create_outcome(
            oc,
            project_id=project.id,
            outcome_key="STAFFING-TEST-ENABLER-R1",
        )
    a = kb.connect(db_path=tmp_path / "board-a.db")
    b = kb.connect(db_path=tmp_path / "board-b.db")
    try:
        yield project, oid, a, b
    finally:
        a.close()
        b.close()


def _task(conn, project, outcome, *, title):
    return kb.create_task(
        conn,
        title=title,
        project_id=project.id,
        outcome_id=outcome,
        mutation_repository="stigrunar/hovewest-prosjektstyring",
        mutation_scope=["apps/prosjektstyring/app/bemanning/**"],
        mutation_base_ref="origin/main@abc",
    )


def test_claim_blocks_competing_mutator_across_board_databases(stores):
    project, outcome, first_db, second_db = stores
    first = _task(first_db, project, outcome, title="first")
    second = _task(second_db, project, outcome, title="second")

    assert kb.claim_task(first_db, first, claimer="worker-a") is not None
    assert kb.claim_task(second_db, second, claimer="worker-b") is None
    assert kb.get_task(second_db, second).status == "ready"

    event = second_db.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (second,),
    ).fetchone()
    assert event["kind"] == "mutation_lease_conflict"
    payload = json.loads(event["payload"])
    assert payload["conflicting_owner"].endswith(first)

    # Repeated dispatcher ticks do not append duplicate collision noise.
    assert kb.claim_task(second_db, second, claimer="worker-b") is None
    count = second_db.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='mutation_lease_conflict'",
        (second,),
    ).fetchone()[0]
    assert count == 1

    assert kb.complete_task(first_db, first, result="candidate frozen")
    claimed_second = kb.claim_task(second_db, second, claimer="worker-b")
    assert claimed_second is not None
    assert claimed_second.status == "running"


def test_non_overlapping_outcome_mutators_can_run_in_parallel(stores):
    project, outcome, first_db, second_db = stores
    with odb.connect_closing() as oc:
        sales = odb.create_outcome(oc, project_id=project.id, outcome_key="SALES-R1")
    staffing = _task(first_db, project, outcome, title="staffing")
    sales_task = kb.create_task(
        second_db,
        title="sales",
        project_id=project.id,
        outcome_id=sales,
        mutation_repository="stigrunar/hovewest-prosjektstyring",
        mutation_scope=["apps/prosjektstyring/app/salg/**"],
    )
    assert kb.claim_task(first_db, staffing, claimer="staffing") is not None
    assert kb.claim_task(second_db, sales_task, claimer="sales") is not None


def test_heartbeat_renews_active_mutation_lease(stores, monkeypatch):
    project, outcome, first_db, _ = stores
    task_id = _task(first_db, project, outcome, title="long")
    assert kb.claim_task(first_db, task_id, claimer="worker") is not None
    with odb.connect_closing() as oc:
        before = odb.active_mutation_leases(oc)[0]["expires_at"]
    # Renewal uses the same owner id and must keep the lease active. Exact wall
    # clock is not asserted because heartbeat and outcomes stores have separate
    # time reads.
    assert kb.heartbeat_claim(first_db, task_id, claimer="worker")
    with odb.connect_closing() as oc:
        after = odb.active_mutation_leases(oc)[0]["expires_at"]
    assert after >= before


def test_kanban_claim_projects_execution_and_completion_terminalizes(stores):
    project, outcome, first_db, _ = stores
    task_id = _task(first_db, project, outcome, title="projected")
    execution_id = kb.kanban_execution_id(task_id)

    assert kb.claim_task(first_db, task_id, claimer="worker") is not None
    with odb.connect_closing() as oc:
        execution = odb.get_execution(oc, execution_id)
        assert execution is not None
        assert execution["execution_mode"] == "kanban"
        assert execution["backend_id"] == task_id
        assert execution["state"] == "running"
        assert execution["mutating"] is True
        assert odb.active_mutation_leases(oc)[0]["owner_execution_id"] == execution_id

    assert kb.heartbeat_claim(first_db, task_id, claimer="worker")
    assert kb.complete_task(first_db, task_id, result="done")
    with odb.connect_closing() as oc:
        execution = odb.get_execution(oc, execution_id)
        assert execution is not None
        assert execution["state"] == "completed"
        assert execution["receipt_uri"] == f"kanban:default:{task_id}"
        assert odb.active_mutation_leases(oc) == []


def test_child_inheritance_survives_worker_without_local_project_db(stores, monkeypatch):
    project, outcome, first_db, _ = stores
    with odb.connect_closing() as oc:
        lane = odb.bind_conversation_lane(
            oc,
            project_id=project.id,
            outcome_id=outcome,
            platform="telegram",
            chat_id="-100123",
            thread_id="41",
        )
    parent = kb.create_task(
        first_db,
        title="parent-cross-profile",
        project_id=project.id,
        outcome_id=outcome,
        conversation_lane_id=lane,
    )
    # Simulate a worker profile whose own projects.db cannot resolve the
    # creator's Project. Parent task identity is the durable fallback.
    monkeypatch.setattr(pdb, "get_project", lambda *_args, **_kwargs: None)
    child = kb.create_task(first_db, title="child-cross-profile", parents=[parent])
    child_task = kb.get_task(first_db, child)
    assert child_task is not None
    assert child_task.project_id == project.id
    assert child_task.outcome_id == outcome
    assert child_task.conversation_lane_id == lane
    assert child_task.topic_target == "telegram:-100123:41"


def test_structured_topic_binding_overrides_origin_and_inherits(stores):
    project, outcome, first_db, _ = stores
    with odb.connect_closing() as oc:
        lane = odb.bind_conversation_lane(
            oc,
            project_id=project.id,
            outcome_id=outcome,
            platform="telegram",
            chat_id="-100123",
            thread_id="42",
            label="Plugin A",
        )
        lane2 = odb.bind_conversation_lane(
            oc,
            project_id=project.id,
            outcome_id=outcome,
            platform="telegram",
            chat_id="-100123",
            thread_id="43",
            label="Plugin A next",
        )

    parent = kb.create_task(
        first_db,
        title="parent",
        project_id=project.id,
        outcome_id=outcome,
        conversation_lane_id=lane,
    )
    parent_task = kb.get_task(first_db, parent)
    assert parent_task is not None
    assert parent_task.topic_target == "telegram:-100123:42"

    # Simulate creation from Dolly main-DM. Structured target must win and the
    # origin DM must not remain as a duplicate visible subscription.
    kb.add_notify_sub(
        first_db,
        task_id=parent,
        platform="telegram",
        chat_id="12345",
        chat_type="dm",
        notifier_profile="default",
        delivery_mode="notify+wake",
    )
    subs = kb.list_notify_subs(first_db, parent)
    assert [(s["platform"], s["chat_id"], s["thread_id"]) for s in subs] == [
        ("telegram", "-100123", "42")
    ]
    assert subs[0]["chat_type"] == "group"

    child = kb.create_task(first_db, title="child", parents=[parent])
    child_task = kb.get_task(first_db, child)
    assert child_task is not None
    assert child_task.project_id == project.id
    assert child_task.outcome_id == outcome
    assert child_task.conversation_lane_id == lane
    assert child_task.topic_target == "telegram:-100123:42"
    assert child_task.parent_execution_id == kb.kanban_execution_id(parent)

    kb.add_notify_sub(
        first_db,
        task_id=child,
        platform="telegram",
        chat_id="12345",
        chat_type="dm",
        notifier_profile="default",
        delivery_mode="notify+wake",
    )
    assert [(s["chat_id"], s["thread_id"]) for s in kb.list_notify_subs(first_db, child)] == [
        ("-100123", "42")
    ]

    assert kb.rebind_task_conversation(
        first_db, child, conversation_lane_id=lane2
    )
    rebound = kb.get_task(first_db, child)
    assert rebound is not None
    assert rebound.conversation_lane_id == lane2
    assert rebound.topic_target == "telegram:-100123:43"
    assert [(s["chat_id"], s["thread_id"]) for s in kb.list_notify_subs(first_db, child)] == [
        ("-100123", "43")
    ]


def test_child_parent_execution_identity_uses_connection_board(stores, tmp_path):
    project, outcome, _first_db, _ = stores
    board_db = tmp_path / ".hermes" / "kanban" / "boards" / "hermes" / "kanban.db"
    board_db.parent.mkdir(parents=True, exist_ok=True)
    conn = kb.connect(db_path=board_db)
    try:
        with odb.connect_closing() as oc:
            lane = odb.bind_conversation_lane(
                oc,
                project_id=project.id,
                outcome_id=outcome,
                platform="telegram",
                chat_id="-100999",
                thread_id="6",
            )
        parent = kb.create_task(
            conn,
            title="board-parent",
            project_id=project.id,
            outcome_id=outcome,
            conversation_lane_id=lane,
        )
        child = kb.create_task(conn, title="board-child", parents=[parent])
        child_task = kb.get_task(conn, child)
        assert child_task is not None
        assert child_task.parent_execution_id == f"kanban:hermes:{parent}"
        assert child_task.project_id == project.id
        assert child_task.outcome_id == outcome
        assert child_task.conversation_lane_id == lane
        assert child_task.topic_target == "telegram:-100999:6"
    finally:
        conn.close()
