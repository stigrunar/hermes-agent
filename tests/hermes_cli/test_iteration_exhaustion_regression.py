"""Focused canaries for bounded iteration-exhaustion terminalization."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.kanban_watchers import _resolve_kanban_notification_events
from hermes_cli import kanban_db as kb
from hermes_cli.execution_state import BlockerType, ResumePolicy
from tools import kanban_tools as kt


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    profile_dir = home / "profiles" / "dollycode"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("model:\n  default: gpt-test\n")
    return home


def test_c1_bounded_feature_goal_mode_is_normalized_one_shot_with_receipt(isolated_home):
    result = json.loads(
        kt._handle_create(
            {
                "title": "bounded code patch",
                "assignee": "dollycode",
                "quality_mode": "FEATURE",
                "work_kind": "implementation_code_patch",
                "goal_mode": True,
                "goal_max_turns": 40,
            }
        )
    )
    assert result["ok"] is True
    assert result["goal_mode"] is False
    assert result["goal_mode_normalized"] is True
    assert result["goal_mode_receipt"]["reason"] == "missing_explicit_long_running_goal_contract"

    with kb.connect() as conn:
        task = kb.get_task(conn, result["task_id"])
        assert task.goal_mode is False
        assert task.goal_max_turns is None
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='goal_mode_normalized'",
            (task.id,),
        ).fetchone()
        assert event is not None
        assert json.loads(event["payload"])["effective_goal_mode"] is False


def test_c2_explicit_long_running_goal_remains_goal_mode(isolated_home):
    result = json.loads(
        kt._handle_create(
            {
                "title": "unattended multi-phase delivery",
                "assignee": "dollycode",
                "quality_mode": "FEATURE",
                "work_kind": "multi_phase_goal",
                "goal_mode": True,
                "goal_max_turns": 40,
                "goal_mode_reason": "same worker must preserve end-state ownership across delivery phases",
                "stop_when": "all named phases are complete and the end-state receipt is durable",
            }
        )
    )
    assert result["ok"] is True
    assert result["goal_mode"] is True
    assert result["goal_mode_normalized"] is False
    assert result["goal_mode_receipt"]["reason"] == "explicit_long_running_goal_contract"
    with kb.connect() as conn:
        task = kb.get_task(conn, result["task_id"])
        assert task is not None
        assert task.goal_mode is True
        assert task.goal_max_turns == 40
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='goal_mode_admitted'",
            (task.id,),
        ).fetchone()
        assert event is not None
        payload = json.loads(event["payload"])
        assert payload["goal_mode_reason"].startswith("same worker")
        assert payload["stop_when"].startswith("all named phases")


def test_c3_c8_iteration_exhaustion_is_terminal_preserves_checkpoint_and_cannot_dispatch(
    isolated_home: Path, tmp_path: Path
):
    workspace = tmp_path / "preserved-worktree"
    workspace.mkdir()
    checkpoint = workspace / "useful.patch"
    checkpoint.write_text("dirty checkpoint evidence\n")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="bounded implementation",
            assignee="dollycode",
            body="contract_id: iter-c3\nrevision: r1",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        claimed = kb.claim_task(conn, tid, claimer="test-worker")
        assert claimed is not None
        run_id = claimed.current_run_id
        assert run_id is not None

        closed = kb._record_iteration_exhaustion(
            conn, tid, budget_used=40, budget_max=40
        )
        assert closed == run_id
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "iteration_exhausted"
        assert task.max_retries == 0
        assert task.current_run_id is None
        assert task.worker_pid is None
        assert checkpoint.read_text() == "dirty checkpoint evidence\n"

        run = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["status"] == "iteration_exhausted"
        assert run["outcome"] == "iteration_exhausted"
        metadata = json.loads(run["metadata"])
        assert metadata["checkpoint_required"] is True
        assert metadata["retryable"] is False
        assert metadata["resume_policy"] == "never"
        assert metadata["workspace_path"] == str(workspace)

        state = kb.get_reconciled_execution_state(conn, tid, failure_limit=5)
        assert state is not None
        assert state.blocker_type is BlockerType.ITERATION_EXHAUSTED
        assert "Iteration budget exhausted (40/40)" in state.blocked_reason
        assert state.resume_policy is ResumePolicy.NEVER
        assert state.executable is False
        assert kb.reconcile_execution_states(conn, failure_limit=5, now=10**12) == []
        assert kb.recompute_ready(conn, failure_limit=5) == 0
        preview = kb.dispatch_once(conn, dry_run=True, failure_limit=5)
        assert preview.promoted == 0

        # C8 / defensive fence: even a stale status write cannot make this
        # revision claimable or dispatchable because the semantic blocker wins.
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
        assert kb.claim_task(conn, tid, claimer="must-not-run") is None
        spawned: list[str] = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace, board=None: spawned.append(task.id) or 12345,
            max_spawn=1,
            failure_limit=5,
        )
        assert spawned == []
        assert tid in result.skipped_nonspawnable


def test_c4_transient_current_contract_gets_exactly_one_retry(isolated_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="machine transient",
            assignee="dollycode",
            body="contract_id: iter-c4\nrevision: r1",
        )
        first = kb.claim_task(conn, tid, claimer="test-worker")
        assert first is not None
        assert kb.block_task(
            conn,
            tid,
            reason="temporary transport reset",
            kind="transient",
            expected_run_id=first.current_run_id,
        )
        conn.execute(
            "UPDATE task_events SET created_at=created_at-120 WHERE task_id=? AND kind='blocked'",
            (tid,),
        )
        conn.commit()
        assert kb.reconcile_execution_states(conn, failure_limit=5, now=10**12) == [tid]
        assert kb.get_task(conn, tid).status == "ready"

        second = kb.claim_task(conn, tid, claimer="test-worker-2")
        assert second is not None
        assert kb.block_task(
            conn,
            tid,
            reason="temporary transport reset",
            kind="transient",
            expected_run_id=second.current_run_id,
        )
        conn.execute(
            "UPDATE task_events SET created_at=created_at-120 WHERE task_id=? AND kind='blocked'",
            (tid,),
        )
        conn.commit()
        assert kb.reconcile_execution_states(conn, failure_limit=5, now=10**12) == []
        state = kb.get_reconciled_execution_state(conn, tid, failure_limit=5)
        assert state.resume_policy is ResumePolicy.NEVER


def test_c5_c6_terminal_notification_never_reuses_old_run_as_running():
    terminal_event = SimpleNamespace(
        id=10,
        kind="iteration_exhausted",
        payload={"budget_used": 40, "budget_max": 40},
        run_id=7,
    )
    terminal_task = SimpleNamespace(status="blocked")
    terminal_run = SimpleNamespace(id=7, outcome="iteration_exhausted")
    selected = _resolve_kanban_notification_events(
        terminal_task,
        [terminal_event],
        terminal_run,
        terminal_statuses={"done", "archived"},
    )
    assert selected == [terminal_event]

    # A real replacement run is distinct current state; the old terminal event
    # is not replayed as if it described the new running run.
    running_task = SimpleNamespace(status="running")
    replacement_run = SimpleNamespace(id=8, outcome=None)
    selected = _resolve_kanban_notification_events(
        running_task,
        [terminal_event],
        replacement_run,
        terminal_statuses={"done", "archived"},
    )
    assert selected == []


def test_c7_native_notification_cursor_claims_terminal_event_once(isolated_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="dedupe terminal event",
            assignee="dollycode",
            body="contract_id: iter-c7\nrevision: r1",
        )
        claimed = kb.claim_task(conn, tid, claimer="test-worker")
        assert claimed is not None
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="-1001",
            thread_id="87",
        )
        kb._record_iteration_exhaustion(conn, tid, budget_used=40, budget_max=40)

        _, _, first = kb.claim_unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="-1001",
            thread_id="87",
            kinds=["iteration_exhausted"],
        )
        _, _, second = kb.claim_unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="-1001",
            thread_id="87",
            kinds=["iteration_exhausted"],
        )
        assert [event.kind for event in first] == ["iteration_exhausted"]
        assert second == []


def test_c9_explicit_one_shot_replacement_can_reuse_preserved_workspace(
    isolated_home: Path, tmp_path: Path
):
    workspace = tmp_path / "continued-worktree"
    workspace.mkdir()
    artifact = workspace / "fix.py"
    artifact.write_text("candidate = 1\n")

    with kb.connect() as conn:
        old = kb.create_task(
            conn,
            title="old goal attempt",
            assignee="dollycode",
            body="contract_id: iter-c9\nrevision: r1",
            workspace_kind="dir",
            workspace_path=str(workspace),
            goal_mode=True,
        )
        old_run = kb.claim_task(conn, old, claimer="old-worker")
        assert old_run is not None
        kb._record_iteration_exhaustion(conn, old, budget_used=40, budget_max=40)
        assert artifact.exists()

        replacement = kb.create_task(
            conn,
            title="explicit one-shot continuation",
            assignee="dollycode",
            body=f"contract_id: iter-c9\nrevision: r2\ncontinuation_of: {old}",
            workspace_kind="dir",
            workspace_path=str(workspace),
            goal_mode=False,
            max_retries=0,
        )
        replacement_task = kb.get_task(conn, replacement)
        assert replacement_task.goal_mode is False
        assert replacement_task.workspace_path == str(workspace)
        assert replacement_task.max_retries == 0
        new_run = kb.claim_task(conn, replacement, claimer="one-shot-worker")
        assert new_run is not None
        assert new_run.current_run_id != old_run.current_run_id

        artifact.write_text("candidate = 2\n")
        assert kb.complete_task(
            conn,
            replacement,
            summary="targeted test green; diff read back; commit-ready",
            expected_run_id=new_run.current_run_id,
        )
        assert kb.get_task(conn, replacement).status == "done"
        assert artifact.read_text() == "candidate = 2\n"
