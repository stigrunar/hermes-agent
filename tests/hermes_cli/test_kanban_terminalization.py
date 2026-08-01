"""Behavioral coverage for exact-run Kanban terminalization state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.mark.parametrize("action", ["complete", "block"])
def test_exact_current_run_terminal_intent_is_read_only_authority(kanban_home, action):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="terminal authority", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        payload = {"action": action, "summary": "done"}
        conn.execute(
            "UPDATE task_runs SET terminal_payload=?, terminal_requested_at=123, "
            "reap_state='terminal_requested' WHERE id=?",
            (json.dumps(payload), run_id),
        )
        conn.commit()
        before = conn.total_changes

        assert kb.accepted_terminal_intent(conn, task_id, run_id) == payload
        assert conn.total_changes == before


@pytest.mark.parametrize(
    ("payload", "requested_at", "reap_state"),
    [
        ("{bad", 123, "terminal_requested"),
        (json.dumps({"action": "complete"}), None, "terminal_requested"),
        (json.dumps({"action": "complete"}), 123, None),
        (json.dumps({"action": "timed_out"}), 123, "terminal_requested"),
    ],
)
def test_absent_malformed_rejected_or_nonworker_intent_is_nonterminal(
    kanban_home, payload, requested_at, reap_state
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="not accepted", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        conn.execute(
            "UPDATE task_runs SET terminal_payload=?, terminal_requested_at=?, "
            "reap_state=? WHERE id=?",
            (payload, requested_at, reap_state, run_id),
        )
        conn.commit()

        assert kb.accepted_terminal_intent(conn, task_id, run_id) is None
        assert kb.accepted_terminal_intent(conn, task_id, run_id + 1) is None


def _close_attempt(conn, task_id: str, *, error: str, outcome: str = "timed_out") -> int:
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None and claimed.current_run_id is not None
    run_id = claimed.current_run_id
    kb._record_task_failure(
        conn,
        task_id,
        error,
        outcome=outcome,
        release_claim=True,
        end_run=True,
        expected_run_id=run_id,
    )
    return run_id


def test_timeout_accounting_is_fenced_to_the_exact_open_run(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="run fence", assignee="worker")
        first_run = _close_attempt(
            conn,
            task_id,
            error="Iteration budget exhausted (90/90)",
        )
        retry = kb.claim_task(conn, task_id)
        assert retry is not None and retry.current_run_id is not None
        retry_run = retry.current_run_id
        before = kb.get_task(conn, task_id)
        assert before is not None
        failures_before = before.consecutive_failures

        kb._record_task_failure(
            conn,
            task_id,
            "stale predecessor timeout",
            outcome="timed_out",
            release_claim=True,
            end_run=True,
            expected_run_id=first_run,
        )

        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.current_run_id == retry_run
        assert current.status == "running"
        assert current.consecutive_failures == failures_before
        current_run = kb.get_run(conn, retry_run)
        assert current_run is not None and current_run.ended_at is None


def test_iteration_timeout_retry_renders_continuation_retry_v1(kanban_home, tmp_path):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="resume closeout",
            assignee="worker",
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "worktree"),
        )
        conn.execute("UPDATE tasks SET branch_name='feature/terminalize' WHERE id=?", (task_id,))
        conn.commit()
        run_id = _close_attempt(
            conn,
            task_id,
            error="Iteration budget exhausted (90/90) — task could not complete",
        )
        kb.add_comment(conn, task_id, "worker", "preserved proof")

        context = kb.build_worker_context(conn, task_id)

        assert "## continuation_retry_v1" in context
        assert f"Source run: {run_id}" in context
        assert f"Source workspace: {tmp_path / 'worktree'}" in context
        assert "Source branch: feature/terminalize" in context
        assert "task comments" in context
        assert "before any setup, discovery, browser use" in context
        assert "exactly one concrete expected-vs-actual mismatch" in context


@pytest.mark.parametrize(
    ("goal_mode", "outcome", "error"),
    [
        (True, "timed_out", "Iteration budget exhausted (90/90)"),
        (False, "crashed", "Iteration budget exhausted (90/90)"),
        (False, "timed_out", "elapsed 100s > limit 90s"),
    ],
)
def test_normal_goal_or_runtime_retries_lack_continuation_mode(
    kanban_home, goal_mode, outcome, error
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="ordinary retry",
            assignee="worker",
            goal_mode=goal_mode,
        )
        _close_attempt(conn, task_id, error=error, outcome=outcome)

        assert "continuation_retry_v1" not in kb.build_worker_context(conn, task_id)
