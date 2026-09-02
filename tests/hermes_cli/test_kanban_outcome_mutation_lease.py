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
