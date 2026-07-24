"""Explicit review-handoff lifecycle regressions."""

from __future__ import annotations

import concurrent.futures
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _registered_chain(conn):
    source = kb.create_task(conn, title="implement", assignee="builder")
    review = kb.create_task(
        conn, title="verify", assignee="reviewer", parents=[source],
    )
    nxt = kb.create_task(conn, title="deploy gate", assignee="operator")
    kb.register_review_handoff(conn, source, review, next_task_id=nxt)
    return source, review, nxt


def _activate(conn, source: str) -> str:
    claimed = kb.claim_task(conn, source, claimer="builder:1")
    assert claimed is not None
    assert kb.block_task(
        conn,
        source,
        reason="review-required: evidence is ready",
        expected_run_id=claimed.current_run_id,
    )
    return kb.list_review_handoffs(conn)[0]["review_task_id"]


def test_register_is_idempotent_and_rejects_ambiguous_graph(kanban_home):
    with kb.connect() as conn:
        source, review, nxt = _registered_chain(conn)
        first = kb.register_review_handoff(conn, source, review, next_task_id=nxt)
        second = kb.register_review_handoff(conn, source, review, next_task_id=nxt)
        assert first == second
        assert kb.get_task(conn, review).status == "todo"
        assert kb.get_task(conn, nxt).status == "todo"

        other = kb.create_task(conn, title="other")
        source2 = kb.create_task(conn, title="source two")
        other_review = kb.create_task(conn, title="ambiguous", parents=[source2, other])
        with pytest.raises(ValueError, match="additional parents"):
            kb.register_review_handoff(conn, source2, other_review)

        source3 = kb.create_task(conn, title="source three")
        review3 = kb.create_task(conn, title="review three", parents=[source3])
        ordinary_child = kb.create_task(conn, title="ordinary child")
        kb.link_tasks(conn, source3, ordinary_child)
        with pytest.raises(ValueError, match="ordinary downstream children"):
            kb.register_review_handoff(conn, source3, review3)


def test_concurrent_registration_collapses_to_one_relationship(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source")
        review = kb.create_task(conn, title="review", parents=[source])

    def register_once(_):
        with kb.connect_closing() as thread_conn:
            return kb.register_review_handoff(thread_conn, source, review)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(register_once, range(8)))
    assert {row["source_task_id"] for row in rows} == {source}
    with kb.connect() as conn:
        assert len(kb.list_review_handoffs(conn)) == 1
        registered = [
            event for event in kb.list_events(conn, source)
            if event.kind == "review_handoff_registered"
        ]
        assert len(registered) == 1


def test_review_required_block_atomically_releases_exact_gate(kanban_home):
    with kb.connect() as conn:
        source, review, nxt = _registered_chain(conn)
        _activate(conn, source)
        assert kb.get_task(conn, source).status == "blocked"
        assert kb.get_task(conn, review).status == "review"
        assert kb.get_task(conn, nxt).status == "todo"
        assert (source, review) not in {
            (row["parent_id"], row["child_id"])
            for row in conn.execute("SELECT * FROM task_links")
        }
        with pytest.raises(ValueError, match="cannot reattach"):
            kb.link_tasks(conn, source, review)
        handoff = kb.list_review_handoffs(conn)[0]
        assert handoff["state"] == "active"
        assert kb.unblock_task(conn, source) is False
        assert kb.archive_task(conn, source) is False
        assert kb.delete_task(conn, review) is False
        assert kb.get_task(conn, source).status == "blocked"

        # Delivery retries do not create/release another gate.
        assert kb.block_task(
            conn, source, reason="review-required: evidence is ready",
        )
        assert len(kb.list_review_handoffs(conn)) == 1

        with pytest.raises(ValueError, match="explicit approved or changes_requested"):
            kb.complete_task(conn, review, summary="looks good")


def test_unsafe_legacy_parent_gate_is_rejected_without_mutation(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source")
        review = kb.create_task(conn, title="review", parents=[source])
        claimed = kb.claim_task(conn, source)
        with pytest.raises(ValueError, match="no explicit review relationship"):
            kb.block_task(
                conn,
                source,
                reason="review-required: ready",
                expected_run_id=claimed.current_run_id,
            )
        assert kb.get_task(conn, source).status == "running"
        assert kb.get_task(conn, review).status == "todo"


def test_review_required_without_children_is_fail_safe(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source")
        claimed = kb.claim_task(conn, source)
        assert claimed is not None
        with pytest.raises(ValueError, match="no registered review_handoff"):
            kb.block_task(
                conn,
                source,
                reason="review-required: ready",
                expected_run_id=claimed.current_run_id,
            )
        assert kb.get_task(conn, source).status == "running"
        assert kb.list_review_handoffs(conn) == []


def test_review_worker_context_keeps_detached_source_handoff(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(
            conn,
            title="implement feature",
            body="Source acceptance criteria and implementation details.",
            assignee="builder",
        )
        review = kb.create_task(
            conn, title="verify feature", assignee="reviewer", parents=[source],
        )
        kb.register_review_handoff(conn, source, review)
        claimed = kb.claim_task(conn, source, claimer="builder:1")
        assert claimed is not None
        assert kb.block_task(
            conn,
            source,
            reason="review-required: inspect the attached evidence",
            expected_run_id=claimed.current_run_id,
        )
        kb.add_comment(conn, source, "builder", "Evidence: tests passed and diff is ready.")
        source_run = kb.latest_run(conn, source)
        assert source_run is not None
        conn.execute(
            "UPDATE task_runs SET summary=?, metadata=? WHERE id=?",
            ("Source handoff summary", '{"changed_files":["feature.py"]}', source_run.id),
        )
        conn.commit()

        context = kb.build_worker_context(conn, review)
        assert f"Source task identity: {source} — implement feature" in context
        assert "Source task body" in context
        assert "Source acceptance criteria" in context
        assert "Source handoff summary" in context
        assert "changed_files" in context
        assert "Evidence: tests passed and diff is ready." in context
        assert "kanban_complete(review_verdict='approved'|'changes_requested', ...)" in context
        assert "Parent task results" not in context


def test_explicit_registration_reconciles_only_named_legacy_residue(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="historical source")
        review = kb.create_task(conn, title="historical review", parents=[source])
        nxt = kb.create_task(conn, title="historical deploy", parents=[review])
        unrelated = kb.create_task(conn, title="unrelated")
        claimed = kb.claim_task(conn, source)
        assert claimed is not None
        # Reproduce a pre-fix persisted shape without invoking the new guard.
        conn.execute(
            "UPDATE tasks SET status='blocked', claim_lock=NULL, claim_expires=NULL, "
            "worker_pid=NULL WHERE id=?",
            (source,),
        )
        kb._end_run(
            conn, source, outcome="blocked", status="blocked", summary="historical",
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', ?, ?)",
            (source, '{"reason":"review-required: historical"}', int(time.time())),
        )
        conn.commit()

        handoff = kb.register_review_handoff(
            conn, source, review, next_task_id=nxt,
        )
        assert handoff["state"] == "active"
        assert kb.get_task(conn, source).status == "blocked"
        assert kb.get_task(conn, review).status == "review"
        assert kb.get_task(conn, nxt).status == "todo"
        links = {
            (row["parent_id"], row["child_id"])
            for row in conn.execute("SELECT parent_id, child_id FROM task_links")
        }
        assert (review, nxt) not in links
        assert (source, nxt) in links
        assert kb.get_task(conn, unrelated).status == "ready"


def test_changes_requested_recuts_then_approved_releases_only_next(kanban_home):
    with kb.connect() as conn:
        source, review, nxt = _registered_chain(conn)
        _activate(conn, source)
        claimed_review = kb.claim_review_task(conn, review, claimer="reviewer:1")
        assert claimed_review is not None
        assert kb.submit_review_verdict(
            conn,
            review,
            verdict="changes_requested",
            summary="add regression coverage",
            expected_run_id=claimed_review.current_run_id,
        )
        assert kb.get_task(conn, source).status == "ready"
        assert kb.get_task(conn, review).status == "done"
        assert kb.get_task(conn, nxt).status == "todo"
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, nxt).status == "todo"
        # Identical verdict delivery is idempotent.
        assert kb.submit_review_verdict(
            conn, review, verdict="changes_requested",
        )

        source_claim = kb.claim_task(conn, source, claimer="builder:2")
        assert source_claim is not None
        assert kb.block_task(
            conn,
            source,
            reason="review-required: recut ready",
            expected_run_id=source_claim.current_run_id,
        )
        assert kb.get_task(conn, review).status == "review"
        second_review = kb.claim_review_task(conn, review, claimer="reviewer:2")
        assert second_review is not None
        assert kb.submit_review_verdict(
            conn,
            review,
            verdict="approved",
            summary="verified",
            expected_run_id=second_review.current_run_id,
        )
        assert kb.get_task(conn, source).status == "done"
        assert kb.get_task(conn, review).status == "done"
        assert kb.get_task(conn, nxt).status == "ready"
        assert kb.latest_run(conn, source).metadata["review_verdict"] == "approved"
        assert kb.submit_review_verdict(conn, review, verdict="approved")


def test_review_claim_reclaim_returns_to_review_lane(kanban_home, monkeypatch):
    with kb.connect() as conn:
        source, review, _ = _registered_chain(conn)
        _activate(conn, source)
        claimed = kb.claim_review_task(conn, review, ttl_seconds=1, claimer="remote:1")
        assert claimed is not None
        conn.execute(
            "UPDATE tasks SET claim_expires=? WHERE id=?",
            (int(time.time()) - 1, review),
        )
        conn.commit()
        assert kb.release_stale_claims(conn, process_effects=False) == 1
        assert kb.get_task(conn, review).status == "review"
        assert kb.claim_review_task(conn, review, claimer="remote:2") is not None


def test_active_review_reclaim_unblock_and_promote_stay_in_review_lane(kanban_home):
    with kb.connect() as conn:
        source, review, _ = _registered_chain(conn)
        _activate(conn, source)

        first_claim = kb.claim_review_task(conn, review, claimer="reviewer:1")
        assert first_claim is not None
        assert kb.reclaim_task(conn, review, reason="review worker interrupted")
        assert kb.get_task(conn, review).status == "review"
        assert kb.get_task(conn, source).status == "blocked"

        second_claim = kb.claim_review_task(conn, review, claimer="reviewer:2")
        assert second_claim is not None
        assert kb.block_task(
            conn,
            review,
            reason="review worker needs more evidence",
            kind="needs_input",
            expected_run_id=second_claim.current_run_id,
        )
        assert kb.get_task(conn, review).status == "blocked"
        promoted, reason = kb.promote_task(
            conn, review, actor="operator", force=True,
        )
        assert promoted is False
        assert "review lane" in reason
        assert kb.recompute_ready(conn) == 0
        assert kb.unblock_task(conn, review)
        assert kb.get_task(conn, review).status == "review"
        assert kb.get_task(conn, source).status == "blocked"


def test_legacy_invalid_active_review_gate_is_parked_once_across_ticks(
    kanban_home, all_assignees_spawnable,
):
    board = "review-worktree-resolution-failure"
    with kb.connect() as conn:
        source = kb.create_task(conn, title="implement", assignee="builder")
        review = kb.create_task(
            conn,
            title="verify",
            assignee="reviewer",
            parents=[source],
        )
        # Reproduce a legacy row that predates creation/registration guards:
        # the task persisted as a pathless worktree and the one-to-one handoff
        # row was inserted directly by the old control plane.
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=NULL WHERE id=?",
            (review,),
        )
        now = int(time.time())
        conn.execute(
            "INSERT INTO review_handoffs "
            "(source_task_id, review_task_id, next_task_id, state, verdict, created_at, updated_at) "
            "VALUES (?, ?, NULL, 'waiting', NULL, ?, ?)",
            (source, review, now, now),
        )
        conn.commit()
        assert _activate(conn, source) == review
        assert kb.get_task(conn, review).status == "review"

        first = kb.dispatch_once(conn, board=board, failure_limit=2)
        assert first.auto_blocked == []
        assert kb.get_task(conn, review).status == "blocked"
        assert kb.get_task(conn, review).consecutive_failures == 0
        assert kb.list_runs(conn, review) == []
        invalid_before = [
            event for event in kb.list_events(conn, review)
            if event.kind == "review_gate_invalid_workspace"
        ]
        assert len(invalid_before) == 1

        second = kb.dispatch_once(conn, board=board, failure_limit=2)
        assert second.auto_blocked == []
        assert kb.get_task(conn, review).status == "blocked"
        events_before = kb.list_events(conn, review)
        review_claims_before = [
            event for event in events_before if event.kind == "claimed"
        ]
        invalid_before = [
            event for event in events_before
            if event.kind == "review_gate_invalid_workspace"
        ]
        assert len(review_claims_before) == 0
        assert len(invalid_before) == 1
        assert invalid_before[0].payload["board"] == board
        runs_before = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (review,)
        ).fetchone()[0]

        for _ in range(3):
            tick = kb.dispatch_once(conn, board=board, failure_limit=2)
            assert tick.spawned == []
            assert tick.auto_blocked == []
            assert kb.get_task(conn, review).status == "blocked"

        events_after = kb.list_events(conn, review)
        assert [
            event for event in events_after if event.kind == "claimed"
        ] == review_claims_before
        assert [
            event for event in events_after
            if event.kind == "review_gate_invalid_workspace"
        ] == invalid_before
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (review,)
        ).fetchone()[0] == runs_before
        handoff = kb.list_review_handoffs(conn)[0]
        assert handoff["state"] == "active"
        assert handoff["verdict"] is None


def test_dry_run_classifies_invalid_review_gate_without_mutation(
    kanban_home, all_assignees_spawnable,
):
    board = "review-dry-run-invalid"
    with kb.connect(board=board) as conn:
        source = kb.create_task(conn, title="implement", assignee="builder")
        review = kb.create_task(
            conn, title="verify", assignee="reviewer", parents=[source],
        )
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=NULL WHERE id=?",
            (review,),
        )
        now = int(time.time())
        conn.execute(
            "INSERT INTO review_handoffs "
            "(source_task_id, review_task_id, next_task_id, state, verdict, created_at, updated_at) "
            "VALUES (?, ?, NULL, 'waiting', NULL, ?, ?)",
            (source, review, now, now),
        )
        conn.commit()
        assert _activate(conn, source) == review
        db_path = kb.kanban_db_path(board=board)
        before_bytes = db_path.read_bytes()
        before_tasks = [tuple(row) for row in conn.execute(
            "SELECT id, status, result, claim_lock, current_run_id "
            "FROM tasks ORDER BY id"
        )]
        before_events = [tuple(row) for row in conn.execute(
            "SELECT task_id, kind, payload, created_at FROM task_events ORDER BY id"
        )]
        before_runs = [tuple(row) for row in conn.execute(
            "SELECT id, task_id, status, outcome FROM task_runs ORDER BY id"
        )]

        result = kb.dispatch_once(conn, board=board, dry_run=True)

        assert review not in [task_id for task_id, _, _ in result.spawned]
        assert review in result.skipped_nonspawnable
        assert [tuple(row) for row in conn.execute(
            "SELECT id, status, result, claim_lock, current_run_id "
            "FROM tasks ORDER BY id"
        )] == before_tasks
        assert [tuple(row) for row in conn.execute(
            "SELECT task_id, kind, payload, created_at FROM task_events ORDER BY id"
        )] == before_events
        assert [tuple(row) for row in conn.execute(
            "SELECT id, task_id, status, outcome FROM task_runs ORDER BY id"
        )] == before_runs
        assert db_path.read_bytes() == before_bytes


@pytest.mark.parametrize("capacity", ["max_spawn", "max_in_progress"])
def test_dry_run_classifies_invalid_review_gate_before_capacity_exit(
    kanban_home, all_assignees_spawnable, capacity,
):
    board = f"review-capacity-{capacity.replace('_', '-')}"
    with kb.connect(board=board) as conn:
        source = kb.create_task(conn, title="implement", assignee="builder")
        review = kb.create_task(
            conn, title="verify", assignee="reviewer", parents=[source],
        )
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=NULL WHERE id=?",
            (review,),
        )
        if capacity == "max_in_progress":
            running = kb.create_task(conn, title="already running", assignee="builder")
            ready = kb.create_task(conn, title="waiting ready", assignee="builder")
            assert kb.claim_task(conn, running, claimer="builder:capacity") is not None
            assert kb.get_task(conn, ready).status == "ready"
        else:
            kb.create_task(conn, title="waiting ready", assignee="builder")
        now = int(time.time())
        conn.execute(
            "INSERT INTO review_handoffs "
            "(source_task_id, review_task_id, next_task_id, state, verdict, created_at, updated_at) "
            "VALUES (?, ?, NULL, 'waiting', NULL, ?, ?)",
            (source, review, now, now),
        )
        conn.commit()
        assert _activate(conn, source) == review

        db_path = kb.kanban_db_path(board=board)
        before_bytes = db_path.read_bytes()
        before_tasks = [tuple(row) for row in conn.execute(
            "SELECT id, status, result, claim_lock, current_run_id "
            "FROM tasks ORDER BY id"
        )]
        before_events = [tuple(row) for row in conn.execute(
            "SELECT task_id, kind, payload, created_at FROM task_events ORDER BY id"
        )]
        before_runs = [tuple(row) for row in conn.execute(
            "SELECT id, task_id, status, outcome FROM task_runs ORDER BY id"
        )]

        kwargs = {"max_spawn": 0} if capacity == "max_spawn" else {
            "max_in_progress": 1,
        }
        result = kb.dispatch_once(conn, board=board, dry_run=True, **kwargs)

        assert review not in [task_id for task_id, _, _ in result.spawned]
        assert review in result.skipped_nonspawnable
        assert [tuple(row) for row in conn.execute(
            "SELECT id, status, result, claim_lock, current_run_id "
            "FROM tasks ORDER BY id"
        )] == before_tasks
        assert [tuple(row) for row in conn.execute(
            "SELECT task_id, kind, payload, created_at FROM task_events ORDER BY id"
        )] == before_events
        assert [tuple(row) for row in conn.execute(
            "SELECT id, task_id, status, outcome FROM task_runs ORDER BY id"
        )] == before_runs
        assert db_path.read_bytes() == before_bytes


def test_dry_run_invalid_project_linked_gate_does_not_initialize_projects_db(
    kanban_home,
):
    board = "review-project-preview"
    projects_path = kanban_home / "projects.db"
    assert not projects_path.exists()
    with kb.connect(board=board) as conn:
        source = kb.create_task(conn, title="implement", assignee="builder")
        review = kb.create_task(
            conn, title="verify", assignee="reviewer", parents=[source],
        )
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=NULL, "
            "project_id='p_legacy_missing' WHERE id=?",
            (review,),
        )
        now = int(time.time())
        conn.execute(
            "INSERT INTO review_handoffs "
            "(source_task_id, review_task_id, next_task_id, state, verdict, created_at, updated_at) "
            "VALUES (?, ?, NULL, 'waiting', NULL, ?, ?)",
            (source, review, now, now),
        )
        conn.commit()
        assert _activate(conn, source) == review
        db_path = kb.kanban_db_path(board=board)
        before_bytes = db_path.read_bytes()
        before_names = sorted(path.name for path in kanban_home.iterdir())

        result = kb.dispatch_once(conn, board=board, dry_run=True, max_spawn=0)

        assert review in result.skipped_nonspawnable
        assert review not in [task_id for task_id, _, _ in result.spawned]
        assert not projects_path.exists()
        assert sorted(path.name for path in kanban_home.iterdir()) == before_names
        assert db_path.read_bytes() == before_bytes


def test_active_review_nonstructural_spawn_failure_keeps_bounded_breaker(
    kanban_home, all_assignees_spawnable, tmp_path,
):
    def fail_spawn(task, workspace):
        raise RuntimeError("review worker unavailable")

    with kb.connect() as conn:
        source = kb.create_task(conn, title="implement", assignee="builder")
        review = kb.create_task(
            conn,
            title="verify",
            assignee="reviewer",
            parents=[source],
            workspace_kind="dir",
            workspace_path=str(tmp_path / "review-workspace"),
        )
        kb.register_review_handoff(conn, source, review)
        assert _activate(conn, source) == review

        first = kb.dispatch_once(conn, spawn_fn=fail_spawn, failure_limit=2)
        assert first.auto_blocked == []
        assert kb.get_task(conn, review).status == "review"
        assert kb.get_task(conn, review).consecutive_failures == 1

        second = kb.dispatch_once(conn, spawn_fn=fail_spawn, failure_limit=2)
        assert second.auto_blocked == [review]
        assert kb.get_task(conn, review).status == "blocked"
        assert any(e.kind == "gave_up" for e in kb.list_events(conn, review))


def test_registration_rejects_legacy_invalid_worktree_before_mutation(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source")
        review = kb.create_task(conn, title="legacy review", parents=[source])
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=NULL WHERE id=?",
            (review,),
        )
        conn.commit()
        with pytest.raises(ValueError, match="resolvable worktree source"):
            kb.register_review_handoff(conn, source, review)
        assert kb.list_review_handoffs(conn) == []
        assert (source, review) in {
            (row["parent_id"], row["child_id"])
            for row in conn.execute("SELECT parent_id, child_id FROM task_links")
        }


def test_review_gate_repair_relinks_active_gate_without_verdict(
    kanban_home, tmp_path,
):
    with kb.connect() as conn:
        source, old, nxt = _registered_chain(conn)
        _activate(conn, source)
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=NULL WHERE id=?",
            (old,),
        )
        replacement = kb.create_task(
            conn,
            title="valid replacement",
            assignee="reviewer",
            parents=[source],
            workspace_kind="dir",
            workspace_path=str(tmp_path / "replacement"),
        )
        conn.commit()

        handoff = kb.link_tasks(
            conn,
            source,
            replacement,
            relationship="review_gate",
            replace_review_gate_id=old,
            repair_reason="legacy review gate had no resolvable repository",
        )

        row = kb.list_review_handoffs(conn)[0]
        assert row["source_task_id"] == source
        assert row["review_task_id"] == replacement
        assert row["next_task_id"] == nxt
        assert row["state"] == "active"
        assert row["verdict"] is None
        assert kb.get_task(conn, source).status == "blocked"
        assert kb.get_task(conn, old).status == "archived"
        assert kb.get_task(conn, replacement).status == "review"
        assert kb.list_runs(conn, replacement) == []
        for task_id, kinds in {
            source: {"review_gate_repaired"},
            old: {"review_gate_replaced", "archived"},
            replacement: {
                "review_gate_replacement_registered",
                "review_gate_replacement_activated",
            },
        }.items():
            assert kinds <= {event.kind for event in kb.list_events(conn, task_id)}


def test_review_gate_repair_rejects_stale_valid_claimed_invalid_and_ambiguous(
    kanban_home, tmp_path,
):
    with kb.connect() as conn:
        source, old, _ = _registered_chain(conn)
        _activate(conn, source)
        valid_replacement = kb.create_task(
            conn,
            title="valid replacement",
            parents=[source],
            workspace_kind="dir",
            workspace_path=str(tmp_path / "valid"),
        )
        with pytest.raises(ValueError, match="stale replace_review_gate_id"):
            kb.link_tasks(
                conn, source, valid_replacement, relationship="review_gate",
                replace_review_gate_id="t_stale", repair_reason="repair",
            )
        with pytest.raises(ValueError, match="repair_reason is required"):
            kb.link_tasks(
                conn, source, valid_replacement, relationship="review_gate",
                replace_review_gate_id=old, repair_reason="",
            )
        with pytest.raises(ValueError, match="structurally invalid"):
            kb.link_tasks(
                conn, source, valid_replacement, relationship="review_gate",
                replace_review_gate_id=old, repair_reason="repair",
            )

        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=NULL WHERE id=?",
            (old,),
        )
        conn.commit()
        claimed = kb.claim_review_task(conn, old, claimer="reviewer:repair-test")
        assert claimed is not None
        with pytest.raises(ValueError, match="running or claimed"):
            kb.link_tasks(
                conn, source, valid_replacement, relationship="review_gate",
                replace_review_gate_id=old, repair_reason="repair",
            )

        # The old gate is still active/claimed; an ambiguous replacement is
        # rejected before it can alter the one-to-one row.
        assert kb.list_review_handoffs(conn)[0]["review_task_id"] == old


def test_review_gate_repair_rejects_extra_old_outgoing_edge_atomically(
    kanban_home, tmp_path,
):
    with kb.connect() as conn:
        source, old, nxt = _registered_chain(conn)
        _activate(conn, source)
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=NULL WHERE id=?",
            (old,),
        )
        unrelated = kb.create_task(conn, title="unrelated child", parents=[old])
        replacement = kb.create_task(
            conn,
            title="valid replacement",
            parents=[source],
            workspace_kind="dir",
            workspace_path=str(tmp_path / "replacement-extra-edge"),
        )
        conn.commit()

        before_handoff = [tuple(row) for row in conn.execute(
            "SELECT * FROM review_handoffs ORDER BY source_task_id"
        )]
        before_tasks = [tuple(row) for row in conn.execute(
            "SELECT id, status, result, completed_at, block_kind "
            "FROM tasks ORDER BY id"
        )]
        before_edges = [tuple(row) for row in conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
        )]
        before_event_counts = [tuple(row) for row in conn.execute(
            "SELECT task_id, kind, COUNT(*) FROM task_events "
            "GROUP BY task_id, kind ORDER BY task_id, kind"
        )]

        with pytest.raises(ValueError, match="old gate has ordinary downstream children"):
            kb.link_tasks(
                conn, source, replacement, relationship="review_gate",
                replace_review_gate_id=old, repair_reason="remove invalid gate",
            )

        assert [tuple(row) for row in conn.execute(
            "SELECT * FROM review_handoffs ORDER BY source_task_id"
        )] == before_handoff
        assert [tuple(row) for row in conn.execute(
            "SELECT id, status, result, completed_at, block_kind "
            "FROM tasks ORDER BY id"
        )] == before_tasks
        assert [tuple(row) for row in conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
        )] == before_edges
        assert [tuple(row) for row in conn.execute(
            "SELECT task_id, kind, COUNT(*) FROM task_events "
            "GROUP BY task_id, kind ORDER BY task_id, kind"
        )] == before_event_counts
        assert kb.get_task(conn, unrelated).status == "todo"


@pytest.mark.parametrize("state", ["active", "changes_requested"])
def test_review_successor_cannot_be_force_promoted_before_approval(kanban_home, state):
    with kb.connect() as conn:
        source, _, nxt = _registered_chain(conn)
        _activate(conn, source)
        if state == "changes_requested":
            review = kb.list_review_handoffs(conn)[0]["review_task_id"]
            claimed = kb.claim_review_task(conn, review, claimer="reviewer:1")
            assert claimed is not None
            assert kb.submit_review_verdict(
                conn,
                review,
                verdict="changes_requested",
                summary="please recut",
                expected_run_id=claimed.current_run_id,
            )

        promoted, reason = kb.promote_task(
            conn, nxt, actor="operator", force=True,
        )
        assert promoted is False
        assert "successor gate" in reason
        assert kb.get_task(conn, nxt).status == "todo"


@pytest.mark.parametrize("state", ["active", "changes_requested"])
def test_review_successor_blocks_all_ordinary_mutations(kanban_home, state):
    with kb.connect() as conn:
        source, _, nxt = _registered_chain(conn)
        _activate(conn, source)
        if state == "changes_requested":
            review = kb.list_review_handoffs(conn)[0]["review_task_id"]
            claimed = kb.claim_review_task(conn, review, claimer="reviewer:1")
            assert claimed is not None
            assert kb.submit_review_verdict(
                conn,
                review,
                verdict="changes_requested",
                summary="please recut",
                expected_run_id=claimed.current_run_id,
            )

        for operation in (
            lambda: kb.complete_task(conn, nxt, result="must not complete"),
            lambda: kb.block_task(conn, nxt, reason="must not block"),
            lambda: kb.schedule_task(conn, nxt, reason="must not schedule"),
            lambda: kb.unblock_task(conn, nxt),
        ):
            with pytest.raises(ValueError, match="successor gate"):
                operation()
            assert kb.get_task(conn, nxt).status == "todo"

        if state == "changes_requested":
            source_claim = kb.claim_task(conn, source, claimer="builder:2")
            assert source_claim is not None
            assert kb.block_task(
                conn,
                source,
                reason="review-required: recut ready",
                expected_run_id=source_claim.current_run_id,
            )
            assert kb.get_task(conn, source).status == "blocked"
            assert kb.get_task(conn, nxt).status == "todo"


@pytest.mark.parametrize("state", ["waiting", "active", "changes_requested"])
def test_review_lifecycle_protects_all_three_task_roles(kanban_home, state):
    with kb.connect() as conn:
        source, review, nxt = _registered_chain(conn)
        if state in ("active", "changes_requested"):
            _activate(conn, source)
        if state == "changes_requested":
            claimed = kb.claim_review_task(conn, review, claimer="reviewer:1")
            assert claimed is not None
            assert kb.submit_review_verdict(
                conn,
                review,
                verdict="changes_requested",
                summary="please recut",
                expected_run_id=claimed.current_run_id,
            )
        for task_id in (source, review, nxt):
            assert kb.archive_task(conn, task_id) is False
            assert kb.delete_task(conn, task_id) is False


def test_delete_archived_task_protects_nonterminal_review_lifecycle(kanban_home):
    with kb.connect() as conn:
        source, review, _ = _registered_chain(conn)
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (source,))
        conn.commit()
        assert kb.delete_archived_task(conn, source) is False
        assert kb.get_task(conn, source) is not None


def test_completion_rechecks_review_guard_inside_write_transaction(kanban_home, monkeypatch):
    with kb.connect() as conn, kb.connect_closing() as racer:
        source = kb.create_task(conn, title="source")
        review = kb.create_task(conn, title="review", parents=[source])
        original_merge = kb._merge_completion_prose_artifacts

        def register_before_update(db_conn, task_id, metadata, **kwargs):
            kb.register_review_handoff(racer, source, review)
            return original_merge(db_conn, task_id, metadata, **kwargs)

        monkeypatch.setattr(kb, "_merge_completion_prose_artifacts", register_before_update)
        with pytest.raises(ValueError, match="registered review gate"):
            kb.complete_task(conn, source, result="must not win the race")
        assert kb.get_task(conn, source).status == "ready"
        assert kb.list_review_handoffs(conn)[0]["state"] == "waiting"


def test_review_approval_preserves_ordinary_next_task_fan_in(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source", assignee="builder")
        review = kb.create_task(
            conn, title="review", assignee="reviewer", parents=[source],
        )
        other_parent = kb.create_task(conn, title="other parent")
        nxt = kb.create_task(conn, title="next", parents=[other_parent])
        kb.register_review_handoff(conn, source, review, next_task_id=nxt)
        _activate(conn, source)
        claimed = kb.claim_review_task(conn, review, claimer="reviewer:1")
        assert claimed is not None
        assert kb.submit_review_verdict(
            conn,
            review,
            verdict="approved",
            summary="approved",
            expected_run_id=claimed.current_run_id,
        )
        assert kb.get_task(conn, nxt).status == "todo"

        assert kb.complete_task(conn, other_parent, result="other parent done")
        assert kb.get_task(conn, nxt).status == "ready"
        assert kb.recompute_ready(conn) == 0


def test_temp_db_dispatch_canary_spawns_one_review_worker(
    kanban_home, all_assignees_spawnable,
):
    spawned = []

    def capture(task, workspace, board=None):
        spawned.append((task.id, list(task.skills or [])))
        return 4242

    with kb.connect() as conn:
        source, review, _ = _registered_chain(conn)
        _activate(conn, source)
        first = kb.dispatch_once(conn, spawn_fn=capture, max_spawn=1)
        second = kb.dispatch_once(conn, spawn_fn=capture, max_spawn=1)
        assert [item[0] for item in first.spawned] == [review]
        assert second.spawned == []
        assert spawned == [(review, [])]
        assert kb.get_task(conn, source).status == "blocked"
