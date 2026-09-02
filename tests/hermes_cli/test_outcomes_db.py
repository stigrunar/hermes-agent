from __future__ import annotations

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
