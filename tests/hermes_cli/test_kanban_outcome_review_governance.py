"""Focused contract tests for typed Kanban outcome-review governance."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_kanban(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    for name in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _active_review(conn):
    source = kb.create_task(conn, title="delivery source", assignee="builder")
    review = kb.create_task(
        conn, title="exact candidate QA", assignee="qa", parents=[source]
    )
    kb.register_review_handoff(conn, source, review)
    claimed = kb.claim_task(conn, source, claimer="builder:1")
    assert claimed is not None
    assert kb.block_task(
        conn,
        source,
        reason="review-required: exact candidate ready",
        expected_run_id=claimed.current_run_id,
    )
    return source, review


def _graph_snapshot(conn):
    return {
        "tasks": [
            tuple(row)
            for row in conn.execute("SELECT id, status FROM tasks ORDER BY id")
        ],
        "links": [
            tuple(row)
            for row in conn.execute(
                "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
            )
        ],
        "handoffs": [
            tuple(row)
            for row in conn.execute(
                "SELECT source_task_id, review_task_id, state, verdict "
                "FROM review_handoffs ORDER BY source_task_id"
            )
        ],
    }


def _valid_blocker():
    return {
        "classification": "blocker",
        "basis": "frozen_acceptance",
        "evidence_refs": ["pytest:contract::AC3", "diff:hermes_cli/kanban_db.py"],
        "outcome_impact": "AC3 would permit unproved review prose to reopen delivery",
        "minimum_fix": "validate the typed finding before the source status update",
        "criterion_id": "AC3",
    }


def test_valid_typed_blocker_is_accepted(isolated_kanban):
    with kb.connect() as conn:
        source, review = _active_review(conn)

        assert kb.submit_review_verdict(
            conn,
            review,
            verdict="changes_requested",
            summary="Concrete AC3 regression",
            findings=[_valid_blocker()],
        )

        assert kb.get_task(conn, source).status == "ready"
        run = kb.latest_run(conn, review)
        assert run is not None
        assert run.metadata["review_findings"][0]["criterion_id"] == "AC3"


@pytest.mark.parametrize(
    "patch",
    [
        {"evidence_refs": []},
        {"basis": "code_quality"},
    ],
)
def test_malformed_blocker_is_rejected_without_graph_mutation(isolated_kanban, patch):
    with kb.connect() as conn:
        _, review = _active_review(conn)
        before = _graph_snapshot(conn)
        blocker = {**_valid_blocker(), **patch}

        with pytest.raises(ValueError):
            kb.submit_review_verdict(
                conn,
                review,
                verdict="changes_requested",
                summary="prose must not control state",
                findings=[blocker],
            )

        assert _graph_snapshot(conn) == before


def test_follow_up_is_recorded_without_dependency_block_or_spawn(isolated_kanban):
    with kb.connect() as conn:
        source, review = _active_review(conn)
        task_ids = {row[0] for row in conn.execute("SELECT id FROM tasks")}
        links = {tuple(row) for row in conn.execute("SELECT * FROM task_links")}

        assert kb.submit_review_verdict(
            conn,
            review,
            verdict="approved",
            summary="Candidate passes; cleanup can follow later",
            findings=[
                {
                    "classification": "follow_up",
                    "evidence_refs": ["note:cleanup"],
                    "outcome_impact": "No impact on the frozen outcome",
                    "minimum_fix": "Optional naming cleanup",
                }
            ],
        )

        assert kb.get_task(conn, source).status == "done"
        assert kb.get_task(conn, review).status == "done"
        assert {row[0] for row in conn.execute("SELECT id FROM tasks")} == task_ids
        assert {tuple(row) for row in conn.execute("SELECT * FROM task_links")} == links


def test_not_verified_can_only_become_targeted_evidence_need(isolated_kanban):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="delivery closeout")
        before = _graph_snapshot(conn)

        result = kb.apply_outcome_review_decision(
            conn,
            task_id,
            actor_profile="default",
            decision="hold_closeout",
            mandatory_criterion_id="AC-runtime",
            findings=[
                {
                    "classification": "not_verified",
                    "criterion_id": "AC-runtime",
                    "evidence_refs": ["test:not-run"],
                    "outcome_impact": "Mandatory runtime criterion lacks evidence",
                    "minimum_fix": "Run the single targeted runtime smoke",
                }
            ],
        )

        assert result["mandatory_criterion_id"] == "AC-runtime"
        assert _graph_snapshot(conn) == before
        events = kb.list_events(conn, task_id)
        assert events[-1].kind == "targeted_evidence_needed"


def test_only_canonical_default_can_reopen_scope_or_authorize_extra_review(
    isolated_kanban,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="frozen delivery")
        for profile in ("Architect", "QA", "Dolly", "Dolly/default", "other"):
            for decision in (
                "mutate_frozen_scope",
                "reopen_scope",
                "accept_risk",
                "authorize_extra_review",
                "release",
                "closeout",
            ):
                with pytest.raises(PermissionError, match="canonical profile default"):
                    kb.apply_outcome_review_decision(
                        conn,
                        task_id,
                        actor_profile=profile,
                        decision=decision,
                    )

        for decision in ("reopen_scope", "authorize_extra_review"):
            applied = kb.apply_outcome_review_decision(
                conn,
                task_id,
                actor_profile="default",
                decision=decision,
            )
            assert applied["decision"] == decision
            assert applied["standard_review_budget"] == {
                "qa": 1,
                "bounded_remediation": 1,
                "targeted_recheck": 1,
            }

        source, review = _active_review(conn)
        assert kb.submit_review_verdict(
            conn,
            review,
            verdict="changes_requested",
            findings=[_valid_blocker()],
        )
        claimed = kb.claim_task(conn, source, claimer="builder:remediation")
        assert claimed is not None
        assert kb.block_task(
            conn,
            source,
            reason="review-required: targeted recheck",
            expected_run_id=claimed.current_run_id,
        )
        with pytest.raises(ValueError, match="standard review budget exhausted"):
            kb.submit_review_verdict(
                conn,
                review,
                verdict="changes_requested",
                findings=[_valid_blocker()],
            )
        kb.apply_outcome_review_decision(
            conn,
            source,
            actor_profile="default",
            decision="authorize_extra_review",
        )
        assert kb.submit_review_verdict(
            conn,
            review,
            verdict="changes_requested",
            findings=[_valid_blocker()],
        )


def test_delivery_closeout_succeeds_with_non_blocking_findings(isolated_kanban):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="delivery candidate")
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        link_count = conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0]

        kb.apply_outcome_review_decision(
            conn,
            task_id,
            actor_profile="Default",
            decision="closeout",
            summary="Frozen outcome delivered with deferred observations",
            findings=[
                {"classification": "follow_up", "evidence_refs": ["note:polish"]},
                {"classification": "unrelated", "evidence_refs": ["note:other-area"]},
                {"classification": "accepted_risk", "evidence_refs": ["risk:R1"]},
            ],
        )

        assert kb.get_task(conn, task_id).status == "done"
        run = kb.latest_run(conn, task_id)
        assert run.metadata["outcome_review_decision"]["findings"] == [
            {
                "classification": classification,
                "basis": None,
                "evidence_refs": refs,
                "outcome_impact": None,
                "minimum_fix": None,
                "criterion_id": None,
            }
            for classification, refs in (
                ("follow_up", ["note:polish"]),
                ("unrelated", ["note:other-area"]),
                ("accepted_risk", ["risk:R1"]),
            )
        ]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == task_count
        assert (
            conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == link_count
        )
