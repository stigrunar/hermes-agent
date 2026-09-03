from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path

import pytest

from hermes_cli import outcomes_db as odb


@pytest.fixture
def conn(tmp_path):
    connection = odb.connect(tmp_path / "outcomes.db")
    try:
        yield connection
    finally:
        connection.close()


def test_outcome_is_idempotent_per_project_and_key(conn):
    first = odb.create_outcome(
        conn,
        project_id="p_one",
        outcome_key="STAFFING-TEST-ENABLER-R1",
        name="Bemanning real-data seam",
        frozen_acceptance=["real source", "read only"],
    )
    second = odb.create_outcome(
        conn,
        project_id="p_one",
        outcome_key="STAFFING-TEST-ENABLER-R1",
        name="ignored on idempotent create",
    )
    assert second == first
    outcome = odb.get_outcome(conn, first)
    assert outcome is not None
    assert outcome.project_id == "p_one"
    assert outcome.frozen_acceptance == ["real source", "read only"]


def test_same_key_can_exist_in_different_projects(conn):
    a = odb.create_outcome(conn, project_id="p_a", outcome_key="R1")
    b = odb.create_outcome(conn, project_id="p_b", outcome_key="R1")
    assert a != b
    assert odb.get_outcome(conn, "R1") is None  # ambiguous without project
    assert odb.get_outcome(conn, "R1", project_id="p_a").id == a


def test_conversation_lane_binds_context_not_project_implicitly(conn):
    oid = odb.create_outcome(conn, project_id="p_ps", outcome_key="STAFFING-R1")
    lane_id = odb.bind_conversation_lane(
        conn,
        project_id="p_ps",
        outcome_id=oid,
        platform="telegram",
        chat_id="-1001",
        thread_id="42",
        label="Bemanning",
    )
    lane = odb.find_conversation_lane(
        conn, platform="telegram", chat_id="-1001", thread_id="42"
    )
    assert lane is not None
    assert lane.id == lane_id
    assert lane.project_id == "p_ps"
    assert lane.outcome_id == oid

    # Rebinding the same coordinate inside the project is an update, not a
    # duplicate lane. Moving it to another project fails closed.
    same = odb.bind_conversation_lane(
        conn,
        project_id="p_ps",
        platform="telegram",
        chat_id="-1001",
        thread_id="42",
        lane_kind="control",
    )
    assert same == lane_id
    with pytest.raises(odb.OutcomeError, match="another project"):
        odb.bind_conversation_lane(
            conn,
            project_id="p_other",
            platform="telegram",
            chat_id="-1001",
            thread_id="42",
        )


def test_lane_cannot_bind_outcome_from_another_project(conn):
    oid = odb.create_outcome(conn, project_id="p_a", outcome_key="O1")
    with pytest.raises(odb.OutcomeError, match="different project"):
        odb.bind_conversation_lane(
            conn,
            project_id="p_b",
            outcome_id=oid,
            platform="telegram",
            chat_id="-1002",
            thread_id="99",
        )


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (["apps/prosjektstyring/app/bemanning/**"], ["apps/prosjektstyring/app/bemanning/page.tsx"], True),
        (["apps/prosjektstyring/lib/staffing-*"], ["apps/prosjektstyring/lib/staffing-capacity-provider.ts"], True),
        (["apps/prosjektstyring/app/salg/**"], ["apps/prosjektstyring/app/bemanning/**"], False),
        (["apps/foo"], ["apps/foo/bar.ts"], True),
        (["docs/**"], ["apps/**"], False),
    ],
)
def test_scope_overlap_is_conservative(left, right, expected):
    assert odb.scopes_overlap(left, right) is expected


def test_mutation_lease_blocks_competing_overlap_across_projects(conn):
    staffing = odb.create_outcome(conn, project_id="p_ps", outcome_key="STAFFING-R1")
    hwstaff = odb.create_outcome(conn, project_id="p_hw", outcome_key="HWSTAFF-R2")
    first = odb.acquire_mutation_lease(
        conn,
        project_id="p_ps",
        outcome_id=staffing,
        repository="stigrunar/hovewest-prosjektstyring",
        path_scope=["apps/prosjektstyring/app/bemanning/**"],
        owner_execution_id="codex:staffing-r1",
        base_ref="abc",
    )
    assert first["owner_execution_id"] == "codex:staffing-r1"

    # The dependency/request may come from HWStaffing, but it cannot acquire an
    # overlapping Prosjektstyring mutation while the current execution owns it.
    with pytest.raises(odb.MutationLeaseConflict) as exc:
        odb.acquire_mutation_lease(
            conn,
            project_id="p_hw",
            outcome_id=hwstaff,
            repository="stigrunar/hovewest-prosjektstyring",
            path_scope=["apps/prosjektstyring/app/bemanning/page.tsx"],
            owner_execution_id="kanban:hwstaffing:t_new",
            base_ref="def",
        )
    assert exc.value.conflicting["owner_execution_id"] == "codex:staffing-r1"


def test_mutation_lease_allows_independent_workstreams_and_releases(conn):
    sales = odb.create_outcome(conn, project_id="p_ps", outcome_key="SALES-R1")
    staffing = odb.create_outcome(conn, project_id="p_ps", outcome_key="STAFFING-R1")
    a = odb.acquire_mutation_lease(
        conn,
        project_id="p_ps",
        outcome_id=sales,
        repository="repo",
        path_scope=["apps/prosjektstyring/app/salg/**"],
        owner_execution_id="codex:sales",
    )
    b = odb.acquire_mutation_lease(
        conn,
        project_id="p_ps",
        outcome_id=staffing,
        repository="repo",
        path_scope=["apps/prosjektstyring/app/bemanning/**"],
        owner_execution_id="codex:staffing",
    )
    assert a["id"] != b["id"]
    assert len(odb.active_mutation_leases(conn, repository="repo")) == 2
    assert odb.release_mutation_lease(conn, owner_execution_id="codex:sales", reason="candidate frozen")
    assert [x["owner_execution_id"] for x in odb.active_mutation_leases(conn, repository="repo")] == ["codex:staffing"]


def test_same_owner_acquire_is_idempotent(conn):
    oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
    kwargs = dict(
        project_id="p",
        outcome_id=oid,
        repository="repo",
        path_scope=["src/**"],
        owner_execution_id="codex:o",
        base_ref="123",
    )
    first = odb.acquire_mutation_lease(conn, **kwargs)
    second = odb.acquire_mutation_lease(conn, **kwargs)
    assert second["id"] == first["id"]


def test_expired_lease_does_not_permanently_hold_scope(conn, monkeypatch):
    oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
    clock = {"now": 1000}
    monkeypatch.setattr(odb, "_now", lambda: clock["now"])
    first = odb.acquire_mutation_lease(
        conn,
        project_id="p",
        outcome_id=oid,
        repository="repo",
        path_scope=["src/**"],
        owner_execution_id="first",
        ttl_seconds=60,
    )
    assert first["expires_at"] == 1060
    clock["now"] = 1061
    second = odb.acquire_mutation_lease(
        conn,
        project_id="p",
        outcome_id=oid,
        repository="repo",
        path_scope=["src/file.py"],
        owner_execution_id="second",
        ttl_seconds=60,
    )
    assert second["owner_execution_id"] == "second"
    assert [x["owner_execution_id"] for x in odb.active_mutation_leases(conn)] == ["second"]


def test_renew_mutation_lease_extends_crash_fence(conn, monkeypatch):
    oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
    clock = {"now": 2000}
    monkeypatch.setattr(odb, "_now", lambda: clock["now"])
    odb.acquire_mutation_lease(
        conn,
        project_id="p",
        outcome_id=oid,
        repository="repo",
        path_scope=["src/**"],
        owner_execution_id="worker",
        ttl_seconds=60,
    )
    clock["now"] = 2030
    assert odb.renew_mutation_lease(conn, owner_execution_id="worker", ttl_seconds=120)
    lease = odb.active_mutation_leases(conn)[0]
    assert lease["expires_at"] == 2150


def test_repository_identity_normalizes_github_remote_forms():
    assert odb._normalize_repository("git@github.com:stigrunar/hovewest-prosjektstyring.git") == "stigrunar/hovewest-prosjektstyring"
    assert odb._normalize_repository("https://github.com/stigrunar/hovewest-prosjektstyring.git") == "stigrunar/hovewest-prosjektstyring"
    assert odb._normalize_repository("stigrunar/hovewest-prosjektstyring") == "stigrunar/hovewest-prosjektstyring"


def test_cross_project_outcome_dependency_is_explicit_and_idempotent(conn):
    staffing_surface = odb.create_outcome(
        conn, project_id="p_ps", outcome_key="STAFFING-TEST-ENABLER-R1"
    )
    staffing_agent = odb.create_outcome(
        conn, project_id="p_hw", outcome_key="HWSTAFFING-AGENT-R2"
    )
    first = odb.add_outcome_dependency(
        conn,
        outcome_id=staffing_agent,
        depends_on_outcome_id=staffing_surface,
    )
    second = odb.add_outcome_dependency(
        conn,
        outcome_id=staffing_agent,
        depends_on_outcome_id=staffing_surface,
    )
    assert second == first
    deps = odb.list_outcome_dependencies(conn, outcome_id=staffing_agent)
    assert deps == [
        {
            "id": first,
            "outcome_id": staffing_agent,
            "depends_on_outcome_id": staffing_surface,
            "dependency_kind": "requires",
            "created_at": deps[0]["created_at"],
            "project_id": "p_hw",
            "outcome_key": "HWSTAFFING-AGENT-R2",
            "depends_on_project_id": "p_ps",
            "depends_on_outcome_key": "STAFFING-TEST-ENABLER-R1",
        }
    ]
    snapshot = odb.project_snapshot(conn, "p_ps")
    assert snapshot["outcome_dependencies"][0]["project_id"] == "p_hw"


def test_outcome_cannot_depend_on_itself(conn):
    oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
    with pytest.raises(odb.OutcomeError, match="itself"):
        odb.add_outcome_dependency(conn, outcome_id=oid, depends_on_outcome_id=oid)


def test_feature_gate_can_fail_closed_before_admission(conn, monkeypatch):
    oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
    eid = odb.create_execution(
        conn,
        project_id="p",
        outcome_id=oid,
        execution_mode="direct_codex",
        owner="default",
        mutating=False,
    )
    monkeypatch.setattr(odb, "cross_project_orchestration_enabled", lambda: False)
    with pytest.raises(odb.ExecutionAdmissionBlocked, match="feature_gate_disabled"):
        odb.admit_execution(conn, eid, require_feature_gate=True)
    assert odb.get_execution(conn, eid)["state"] == "queued"


def test_orchestration_mode_is_project_only_for_bound_lane(conn):
    oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
    lane_id = odb.bind_conversation_lane(
        conn,
        project_id="p",
        outcome_id=oid,
        platform="telegram",
        chat_id="-1001",
        thread_id="42",
    )
    assert odb.resolve_orchestration_mode(
        conn, platform="telegram", chat_id="-1001", thread_id="42"
    ) == {
        "mode": "project",
        "project_id": "p",
        "outcome_id": oid,
        "conversation_lane_id": lane_id,
    }
    assert odb.resolve_orchestration_mode(
        conn, platform="telegram", chat_id="123"
    )["mode"] == "portfolio"
    assert odb.resolve_orchestration_mode(
        conn,
        platform="telegram",
        chat_id="-1001",
        thread_id="42",
        force_portfolio=True,
    )["mode"] == "portfolio"


def test_execution_crud_heartbeat_and_context_validation(conn, monkeypatch):
    oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
    other = odb.create_outcome(conn, project_id="other", outcome_key="O")
    lane = odb.bind_conversation_lane(
        conn,
        project_id="p",
        outcome_id=oid,
        platform="telegram",
        chat_id="-1001",
        thread_id="7",
    )
    clock = {"now": 1000}
    monkeypatch.setattr(odb, "_now", lambda: clock["now"])
    eid = odb.create_execution(
        conn,
        project_id="p",
        outcome_id=oid,
        execution_mode="direct_codex",
        owner="default",
        mutating=False,
        conversation_lane_id=lane,
    )
    execution = odb.get_execution(conn, eid)
    assert execution["delivery_target"] == "telegram:-1001:7"
    assert execution["state"] == "queued"
    odb.admit_execution(conn, eid)
    assert odb.get_execution(conn, eid)["state"] == "running"
    clock["now"] = 1010
    assert odb.heartbeat_execution(conn, eid)
    assert odb.get_execution(conn, eid)["last_heartbeat_at"] == 1010
    assert odb.terminalize_execution(conn, eid, state="completed", receipt_uri="receipt://done")
    assert odb.get_execution(conn, eid)["receipt_uri"] == "receipt://done"
    with pytest.raises(odb.OutcomeError, match="unknown outcome"):
        odb.create_execution(
            conn,
            project_id="p",
            outcome_id=other,
            execution_mode="kanban",
            owner="dollycode",
        )


def test_unified_mutating_admission_counts_backends_and_excludes_read_only(conn):
    oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
    for idx, mode in enumerate(("direct_codex", "kanban", "external"), start=1):
        kwargs = {}
        if mode == "direct_codex":
            kwargs = {"repository": "repo", "mutation_scope": ["direct/**"]}
        eid = odb.create_execution(
            conn,
            execution_id=f"ex_{idx}",
            project_id="p",
            outcome_id=oid,
            execution_mode=mode,
            owner=f"owner-{idx}",
            mutating=True,
            **kwargs,
        )
        odb.admit_execution(conn, eid)
    read_only = odb.create_execution(
        conn,
        execution_id="ex_read",
        project_id="p",
        outcome_id=oid,
        execution_mode="direct_codex",
        owner="reader",
        mutating=False,
        state="running",
    )
    assert odb.get_execution(conn, read_only)["state"] == "running"
    fourth = odb.create_execution(
        conn,
        execution_id="ex_fourth",
        project_id="p",
        outcome_id=oid,
        execution_mode="kanban",
        owner="owner-4",
        mutating=True,
    )
    with pytest.raises(odb.ExecutionAdmissionBlocked, match="global_mutating_cap"):
        odb.admit_execution(conn, fourth)


def test_concurrent_admission_cannot_overbook_last_global_slot(conn):
    oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
    for eid in ("ex_race_a", "ex_race_b"):
        odb.create_execution(
            conn,
            execution_id=eid,
            project_id="p",
            outcome_id=oid,
            execution_mode="direct_codex",
            owner=eid,
            mutating=True,
            repository="repo",
            mutation_scope=[f"race/{eid}/**"],
        )
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    barrier = threading.Barrier(2)

    def _admit(eid: str) -> str:
        local = odb.connect(db_path)
        try:
            barrier.wait(timeout=5)
            try:
                odb.admit_execution(local, eid, global_cap=1, owner_cap=2)
                return "running"
            except odb.ExecutionAdmissionBlocked as exc:
                return exc.reason
        finally:
            local.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(_admit, ("ex_race_a", "ex_race_b")))
    assert results == ["global_mutating_cap", "running"]
    assert len(odb.list_executions(conn, states=["running"])) == 1


def test_per_owner_mutating_cap_is_independent_of_global_cap(conn):
    oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
    for idx in range(2):
        eid = odb.create_execution(
            conn,
            execution_id=f"ex_owner_{idx}",
            project_id="p",
            outcome_id=oid,
            execution_mode="direct_codex",
            owner="default",
            mutating=True,
            repository="repo",
            mutation_scope=[f"owner/{idx}/**"],
        )
        odb.admit_execution(conn, eid, global_cap=10, owner_cap=2)
    third = odb.create_execution(
        conn,
        execution_id="ex_owner_3",
        project_id="p",
        outcome_id=oid,
        execution_mode="kanban",
        owner="default",
        mutating=True,
    )
    with pytest.raises(odb.ExecutionAdmissionBlocked, match="owner_mutating_cap"):
        odb.admit_execution(conn, third, global_cap=10, owner_cap=2)


def test_vectorworks_capacity_is_fixed_at_one(conn):
    oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
    eid = odb.create_execution(
        conn,
        execution_id="ex_capacity",
        project_id="p",
        outcome_id=oid,
        execution_mode="external",
        owner="dollyqa",
        mutating=False,
    )
    with pytest.raises(odb.OutcomeError, match="fixed at 1"):
        odb.request_resource_lease(
            conn,
            resource_key="vectorworks-local",
            owner_execution_id=eid,
            capacity=2,
        )


def test_resource_lease_is_fifo_and_never_stolen_by_ttl_alone(conn, monkeypatch):
    oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
    for eid in ("ex_a", "ex_b", "ex_c"):
        odb.create_execution(
            conn,
            execution_id=eid,
            project_id="p",
            outcome_id=oid,
            execution_mode="direct_codex",
            owner=eid,
            mutating=False,
        )
    clock = {"now": 2000}
    monkeypatch.setattr(odb, "_now", lambda: clock["now"])
    a = odb.request_resource_lease(conn, resource_key="vectorworks-local", owner_execution_id="ex_a")
    b = odb.request_resource_lease(conn, resource_key="vectorworks-local", owner_execution_id="ex_b")
    c = odb.request_resource_lease(conn, resource_key="vectorworks-local", owner_execution_id="ex_c")
    assert a["state"] == "acquired"
    assert b["state"] == "waiting"
    assert c["state"] == "waiting"
    clock["now"] = a["expires_at"] + 1
    with pytest.raises(odb.OutcomeError, match="verified_dead"):
        odb.release_resource_lease(conn, lease_id=a["id"], stale=True)
    assert odb.list_resource_leases(conn, resource_key="vectorworks-local")[0]["owner_execution_id"] == "ex_a"
    released = odb.release_resource_lease(conn, lease_id=a["id"], reason="done")
    assert released["promoted"] == [b["id"]]
    active = odb.list_resource_leases(conn, resource_key="vectorworks-local")
    assert [(item["owner_execution_id"], item["state"]) for item in active] == [
        ("ex_b", "acquired"),
        ("ex_c", "waiting"),
    ]
    odb.terminalize_execution(conn, "ex_b", state="completed")
    assert odb.list_resource_leases(conn, resource_key="vectorworks-local")[0]["owner_execution_id"] == "ex_c"


def test_resource_requirement_blocks_admission_until_promoted(conn):
    oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
    first = odb.create_execution(
        conn,
        execution_id="ex_first",
        project_id="p",
        outcome_id=oid,
        execution_mode="kanban",
        owner="dollyqa",
        mutating=False,
        resource_requirements=["vectorworks-local"],
    )
    second = odb.create_execution(
        conn,
        execution_id="ex_second",
        project_id="p",
        outcome_id=oid,
        execution_mode="direct_codex",
        owner="default",
        mutating=False,
        resource_requirements=["vectorworks-local"],
    )
    assert odb.admit_execution(conn, first)["state"] == "running"
    with pytest.raises(odb.ExecutionAdmissionBlocked, match="waiting_resource"):
        odb.admit_execution(conn, second)
    assert odb.get_execution(conn, second)["state"] == "waiting_resource"
    assert odb.terminalize_execution(conn, first, state="completed")
    assert odb.get_execution(conn, second)["state"] == "queued"
    assert odb.admit_execution(conn, second)["state"] == "running"


def test_visible_event_idempotency_is_stable(conn):
    oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
    eid = odb.create_execution(
        conn,
        project_id="p",
        outcome_id=oid,
        execution_mode="external",
        owner="default",
        mutating=False,
    )
    first_key, first_new = odb.record_visible_event(
        conn, execution_id=eid, event_kind="completed", candidate_revision="abc"
    )
    second_key, second_new = odb.record_visible_event(
        conn, execution_id=eid, event_kind="completed", candidate_revision="abc"
    )
    assert first_key == second_key
    assert first_new is True
    assert second_new is False
