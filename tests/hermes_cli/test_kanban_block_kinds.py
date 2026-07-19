"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking.
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-cause re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``unblock_task`` deliberately does NOT reset ``block_recurrences`` (the
  amnesia that let the loop run unbounded).
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    """Create a task and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------


def test_first_typed_block_lands_in_blocked(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="which key?", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind == "needs_input"
        assert t.block_recurrences == 1


def test_unblock_does_not_reset_recurrence_counter(kanban_home: Path) -> None:
    """The crux of the fix: unblock must preserve the loop counter."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="needs_input")
        assert kb.get_task(conn, tid).block_recurrences == 1
        assert kb.unblock_task(conn, tid)
        t = kb.get_task(conn, tid)
        assert t.status == "ready"
        assert t.block_recurrences == 1  # NOT reset to 0
        assert t.block_kind == "needs_input"  # kind preserved for comparison


def test_same_cause_reblock_routes_to_triage(kanban_home: Path) -> None:
    """Dale's loop: block → unblock → re-block same kind → triage."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="need creds", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="still need creds", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "triage"
        assert t.block_recurrences == 2


def test_untyped_block_loop_also_protected(kanban_home: Path) -> None:
    """Legacy un-typed blocks (kind=None) still trip the breaker."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="a")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="a again")
        assert kb.get_task(conn, tid).status == "triage"


def test_different_kinds_do_not_compound(kanban_home: Path) -> None:
    """A re-block for a DIFFERENT reason resets the counter to 1."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="a", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="b", kind="capability")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_recurrences == 1


def test_block_loop_detected_event_emitted(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
        assert events, "expected a block_loop_detected event"
        payload = events[-1].payload or {}
        assert payload.get("recurrences") == 2
        assert payload.get("kind") == "capability"


# ---------------------------------------------------------------------------
# Dependency routing
# ---------------------------------------------------------------------------


def test_dependency_block_routes_to_todo(kanban_home: Path) -> None:
    """Dependency waits never enter the human 'blocked' bucket."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="unfinished parent", assignee="worker")
        tid = _running_task(conn)
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
        assert kb.block_task(conn, tid, reason="need X first", kind="dependency")
        t = kb.get_task(conn, tid)
        assert t.status == "todo"
        assert t.block_kind == "dependency"


def test_dependency_wait_does_not_repromote_until_named_task_changes(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        upstream = kb.create_task(conn, title="upstream", assignee=None)
        child = _running_task(conn, title="child")
        assert kb.block_task(conn, child, reason=f"waiting for {upstream}",
                             kind="dependency", dependency_task_id=upstream)
        spawned = []
        for _ in range(3):
            result = kb.dispatch_once(
                conn, spawn_fn=lambda task, workspace: spawned.append(task.id),
                max_spawn=2,
            )
            assert child not in result.spawned
            assert spawned == []
            assert kb.get_task(conn, child).status == "todo"
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET result = ? WHERE id = ?", ("approved", upstream))
        assert kb.recompute_ready(conn) == 1
        assert kb.get_task(conn, child).status == "ready"
        assert kb.recompute_ready(conn) == 0


def test_claim_rejects_stale_ready_dependency_wait(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        upstream = kb.create_task(conn, title="upstream", assignee="reviewer")
        child = _running_task(conn, title="child")
        assert kb.block_task(conn, child, reason="wait", kind="dependency",
                             dependency_task_id=upstream)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        conn.commit()
        assert kb.claim_task(conn, child) is None
        assert kb.get_task(conn, child).status == "todo"


def test_dependency_block_without_unfinished_parent_is_atomic(kanban_home: Path) -> None:
    """A dependency wait must not create a promotion/retry loop or mutate a run."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        before_events = kb.list_events(conn, tid)
        before_runs = kb.list_runs(conn, tid)
        current_run_id = kb.get_task(conn, tid).current_run_id

        with pytest.raises(ValueError, match="dependency_wait_requires_unfinished_parent"):
            kb.block_task(conn, tid, reason="nothing to wait for", kind="dependency")

        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.current_run_id == current_run_id
        assert kb.list_events(conn, tid) == before_events
        after_runs = kb.list_runs(conn, tid)
        assert len(after_runs) == len(before_runs)
        assert after_runs[-1].ended_at is None
        assert after_runs[-1].outcome is None
        assert not [e for e in after_runs if e.outcome == "blocked"]


def test_dependency_block_with_only_terminal_parent_is_atomic(kanban_home: Path) -> None:
    """A linked done parent is not a valid dependency release condition."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="finished parent", assignee="worker")
        assert kb.complete_task(conn, parent, result="finished")
        tid = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
        before_events = kb.list_events(conn, tid)
        before_runs = kb.list_runs(conn, tid)
        current_run_id = kb.get_task(conn, tid).current_run_id

        with pytest.raises(ValueError, match="dependency_wait_requires_unfinished_parent"):
            kb.block_task(
                conn,
                tid,
                reason="parent is already done",
                kind="dependency",
                dependency_task_id=parent,
            )

        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.current_run_id == current_run_id
        assert kb.list_events(conn, tid) == before_events
        assert kb.list_runs(conn, tid) == before_runs


def test_cli_dependency_block_rejection_is_concise(kanban_home: Path, capsys) -> None:
    """The selectable CLI kind reports the DB guard without a traceback."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="finished parent", assignee="worker")
        assert kb.complete_task(conn, parent, result="finished")
        tid = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=tid)

    result = kanban_cli._cmd_block(SimpleNamespace(
        task_id=tid,
        reason=["parent", "is", "done"],
        kind="dependency",
        ids=None,
    ))
    captured = capsys.readouterr()
    assert result == 1
    assert f"cannot block {tid}: dependency_wait_requires_unfinished_parent" in captured.err
    assert "Traceback" not in captured.err
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running"
        assert kb.list_comments(conn, tid) == []


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


def test_completion_clears_block_memory(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).block_recurrences == 1
        kb.complete_task(conn, tid, result="done")
        t = kb.get_task(conn, tid)
        assert t.status == "done"
        assert t.block_recurrences == 0
        assert t.block_kind is None


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------


def test_invalid_kind_rejected(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        with pytest.raises(ValueError):
            kb.block_task(conn, tid, reason="x", kind="bogus")


def test_block_without_kind_is_backward_compatible(kanban_home: Path) -> None:
    """Existing callers that pass no kind keep the old single-block behaviour."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="legacy")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind is None
