"""Typed exact-review gate lifecycle regressions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


CANDIDATE_SHA = "a" * 40
CANDIDATE_TREE = "b" * 40
OTHER_SHA = "c" * 40
OTHER_TREE = "d" * 40


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _create_active_gate(conn: sqlite3.Connection) -> tuple[str, str, int]:
    source = kb.create_task(conn, title="implement", assignee="builder")
    claimed = kb.claim_task(conn, source, claimer="builder:test")
    assert claimed is not None and claimed.current_run_id is not None
    review = kb.create_task(
        conn,
        title="review exact candidate",
        assignee="reviewer",
        review_gate={
            "source_task_id": source,
            "candidate_sha": CANDIDATE_SHA,
            "candidate_tree": CANDIDATE_TREE,
        },
    )
    assert kb.get_task(conn, review).status == "todo"
    assert kb.parent_ids(conn, review) == []
    assert kb.child_ids(conn, source) == []
    assert kb.block_task(
        conn,
        source,
        reason="candidate ready for exact review",
        kind="review_gate",
        expected_run_id=claimed.current_run_id,
    )
    assert kb.get_task(conn, source).status == "blocked"
    assert kb.get_task(conn, source).block_kind == "review_gate"
    assert kb.get_task(conn, review).status == "ready"
    assert kb.get_review_gate(conn, review_task_id=review)["status"] == "active"
    return source, review, claimed.current_run_id


def _claim_and_complete_review(
    conn: sqlite3.Connection,
    review: str,
    receipt: dict | None,
) -> bool:
    claimed = kb.claim_task(conn, review, claimer="reviewer:test")
    assert claimed is not None and claimed.current_run_id is not None
    return kb.complete_task(
        conn,
        review,
        summary="review finished",
        review_receipt=receipt,
        expected_run_id=claimed.current_run_id,
    )


def _exact_receipt(verdict: str = "approved") -> dict[str, str]:
    return {
        "verdict": verdict,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
    }


def test_exact_approved_linked_review_releases_source_once(kanban_home: Path) -> None:
    with kb.connect() as conn:
        source, review, _ = _create_active_gate(conn)

        assert _claim_and_complete_review(conn, review, _exact_receipt())
        assert kb.get_task(conn, review).status == "done"
        released = kb.get_task(conn, source)
        assert released.status == "ready"
        assert released.block_kind is None

        gate = kb.get_review_gate(conn, review_task_id=review)
        assert gate["status"] == "released"
        assert gate["verdict"] == "approved"
        assert gate["reconciliation_reason"] == "approved_exact_match"
        assert gate["receipt_candidate_sha"] == CANDIDATE_SHA
        assert gate["receipt_candidate_tree"] == CANDIDATE_TREE

        # A retry cannot complete/reconcile the already-terminal review again.
        assert not kb.complete_task(
            conn,
            review,
            summary="duplicate delivery",
            review_receipt=_exact_receipt(),
        )
        source_events = kb.list_events(conn, source)
        assert [e.kind for e in source_events].count("review_gate_released") == 1
        assert kb.claim_task(conn, source, claimer="builder:next") is not None
        assert kb.claim_task(conn, source, claimer="builder:duplicate") is None


def test_changes_requested_holds_source(kanban_home: Path) -> None:
    with kb.connect() as conn:
        source, review, _ = _create_active_gate(conn)
        assert _claim_and_complete_review(
            conn, review, _exact_receipt("changes_requested")
        )

        assert kb.get_task(conn, source).status == "blocked"
        gate = kb.get_review_gate(conn, review_task_id=review)
        assert gate["status"] == "held"
        assert gate["reconciliation_reason"] == "changes_requested"
        assert not any(
            event.kind == "review_gate_released"
            for event in kb.list_events(conn, source)
        )


@pytest.mark.parametrize(
    ("receipt", "reason"),
    [
        (
            {
                "verdict": "approved",
                "candidate_sha": OTHER_SHA,
                "candidate_tree": CANDIDATE_TREE,
            },
            "candidate_sha_mismatch",
        ),
        (
            {
                "verdict": "approved",
                "candidate_sha": CANDIDATE_SHA,
                "candidate_tree": OTHER_TREE,
            },
            "candidate_tree_mismatch",
        ),
        (None, "missing_receipt"),
        ({"candidate_sha": CANDIDATE_SHA, "candidate_tree": CANDIDATE_TREE}, "missing_verdict"),
        ({"verdict": "approved", "candidate_tree": CANDIDATE_TREE}, "missing_candidate_sha"),
        ({"verdict": "approved", "candidate_sha": CANDIDATE_SHA}, "missing_candidate_tree"),
    ],
)
def test_missing_or_mismatched_receipt_holds_source(
    kanban_home: Path,
    receipt: dict | None,
    reason: str,
) -> None:
    with kb.connect() as conn:
        source, review, _ = _create_active_gate(conn)
        assert _claim_and_complete_review(conn, review, receipt)

        assert kb.get_task(conn, source).status == "blocked"
        gate = kb.get_review_gate(conn, review_task_id=review)
        assert gate["status"] == "held"
        assert gate["reconciliation_reason"] == reason
        held = [
            e for e in kb.list_events(conn, source)
            if e.kind == "review_gate_held"
        ]
        assert len(held) == 1
        assert held[0].payload["reason"] == reason


def test_unrelated_sticky_block_and_text_only_approval_are_untouched(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source mentioned by approved review")
        source_claim = kb.claim_task(conn, source)
        assert kb.block_task(
            conn,
            source,
            reason="review-required: legacy prose remains manual",
            kind="needs_input",
            expected_run_id=source_claim.current_run_id,
        )
        review = kb.create_task(
            conn,
            title=f"APPROVED review for {source} at {CANDIDATE_SHA}",
            body=f"source_task_id={source}; candidate_tree={CANDIDATE_TREE}",
        )
        review_claim = kb.claim_task(conn, review)
        assert kb.complete_task(
            conn,
            review,
            summary="approved",
            metadata={
                "verdict": "approved",
                "source_task_id": source,
                "candidate_sha": CANDIDATE_SHA,
                "candidate_tree": CANDIDATE_TREE,
            },
            review_receipt=_exact_receipt(),
            expected_run_id=review_claim.current_run_id,
        )

        assert kb.get_task(conn, source).status == "blocked"
        assert kb.get_task(conn, source).block_kind == "needs_input"
        assert kb.get_review_gate(conn, review_task_id=review) is None
        assert not any(
            event.kind.startswith("review_gate_")
            for event in kb.list_events(conn, review)
        )


def test_gate_is_unambiguous_and_active_gate_rejects_broad_unblock(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        source, review, _ = _create_active_gate(conn)
        with pytest.raises(ValueError, match="already has an unresolved review gate"):
            kb.create_task(
                conn,
                title="ambiguous second reviewer",
                review_gate={
                    "source_task_id": source,
                    "candidate_sha": OTHER_SHA,
                    "candidate_tree": OTHER_TREE,
                },
            )
        assert not kb.unblock_task(conn, source)
        promoted, why = kb.promote_task(conn, source, actor="test")
        assert not promoted
        assert why == "typed review gate is unresolved"
        assert kb.get_task(conn, source).status == "blocked"
        assert kb.get_task(conn, review).status == "ready"


def test_blocked_or_failed_review_holds_source_with_audit(kanban_home: Path) -> None:
    with kb.connect() as conn:
        source, review, _ = _create_active_gate(conn)
        review_claim = kb.claim_task(conn, review)
        assert kb.block_task(
            conn,
            review,
            reason="review cannot establish safety",
            kind="needs_input",
            expected_run_id=review_claim.current_run_id,
        )
        assert kb.get_task(conn, source).status == "blocked"
        assert kb.get_review_gate(
            conn, review_task_id=review
        )["reconciliation_reason"] == "review_blocked"

        # A new source/gate exercises the terminal failure breaker path.
        source2, review2, _ = _create_active_gate(conn)
        kb.claim_task(conn, review2)
        assert kb._record_task_failure(
            conn,
            review2,
            "review worker failed",
            outcome="spawn_failed",
            failure_limit=1,
            release_claim=True,
            end_run=True,
        )
        assert kb.get_task(conn, source2).status == "blocked"
        assert kb.get_review_gate(
            conn, review_task_id=review2
        )["reconciliation_reason"] == "review_failed"


def test_dispatch_chain_reconciles_without_restart(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    spawned: list[str] = []

    def spawn(task, _workspace):
        spawned.append(task.id)
        return None

    with kb.connect() as conn:
        source, review, _ = _create_active_gate(conn)

        first = kb.dispatch_once(conn, spawn_fn=spawn)
        assert [task_id for task_id, _, _ in first.spawned] == [review]
        assert spawned == [review]
        assert kb.get_task(conn, review).status == "running"
        review_run_id = kb.get_task(conn, review).current_run_id

        assert kb.complete_task(
            conn,
            review,
            summary="exact candidate approved",
            review_receipt=_exact_receipt(),
            expected_run_id=review_run_id,
        )
        assert kb.get_task(conn, source).status == "ready"

        second = kb.dispatch_once(conn, spawn_fn=spawn)
        assert [task_id for task_id, _, _ in second.spawned] == [source]
        assert spawned == [review, source]
        assert kb.get_task(conn, source).status == "running"

        third = kb.dispatch_once(conn, spawn_fn=spawn)
        assert third.spawned == []
        assert spawned == [review, source]
        assert [
            e.kind for e in kb.list_events(conn, source)
        ].count("review_gate_released") == 1


def test_legacy_database_gets_additive_review_gate_table(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-kanban.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, "
        "status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_by TEXT, "
        "created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER, "
        "workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT, "
        "claim_lock TEXT, claim_expires INTEGER)"
    )
    conn.execute(
        "CREATE TABLE task_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
        "kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy', 'sticky legacy card', 'blocked', 1)"
    )
    conn.commit()
    conn.close()

    kb._INITIALIZED_PATHS.clear()
    with kb.connect(db_path) as migrated:
        tables = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        indexes = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "review_gates" in tables
        assert "idx_review_gates_unresolved_source" in indexes
        assert kb.get_task(migrated, "legacy").status == "blocked"
        assert kb.get_review_gate(
            migrated, source_task_id="legacy", unresolved_only=True,
        ) is None
