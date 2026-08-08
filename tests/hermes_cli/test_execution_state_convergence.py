"""Focused acceptance for the Hermes execution-state convergence contract."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.execution_state import BlockerType, ResumePolicy


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claim_and_block(conn, tid: str, *, kind: str, reason: str):
    claimed = kb.claim_task(conn, tid, claimer="test-worker")
    assert claimed is not None
    assert kb.block_task(
        conn,
        tid,
        reason=reason,
        kind=kind,
        expected_run_id=claimed.current_run_id,
    )
    return kb.get_task(conn, tid)


def _age_latest_block(conn, tid: str, *, age: int = 120):
    conn.execute(
        "UPDATE task_events SET created_at=created_at-? WHERE task_id=? AND kind='blocked'",
        (age, tid),
    )
    conn.commit()


def test_c1_dependency_wait_auto_resumes_when_parent_is_terminal(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="dependency", assignee="dollycode")
        child = kb.create_task(
            conn,
            title="dependent",
            assignee="dollycode",
            parents=[parent],
            body="contract_id: esc-c1\nrevision: r1",
        )
        assert kb.get_task(conn, child).status == "todo"
        before = kb.get_reconciled_execution_state(conn, child)
        assert before.blocker_type is BlockerType.DEPENDENCY
        assert before.resume_policy is ResumePolicy.AUTO_WHEN_RESOLVED

        assert kb.complete_task(conn, parent, summary="dependency done")
        # completion already performs the scoped recompute; an extra tick is idempotent.
        assert kb.get_task(conn, child).status == "ready"
        assert kb.recompute_ready(conn) == 0


def test_c3_manual_block_is_sticky_and_never_auto_resumed(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="manual decision",
            assignee="dollycode",
            body="contract_id: esc-c3\nrevision: r1",
        )
        _claim_and_block(conn, tid, kind="needs_input", reason="Stig must choose A or B")
        _age_latest_block(conn, tid)
        state = kb.get_reconciled_execution_state(conn, tid)
        assert state.blocker_type is BlockerType.MANUAL
        assert state.resume_policy is ResumePolicy.MANUAL_ONCE
        assert kb.reconcile_execution_states(conn, now=10**12) == []
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, tid).status == "blocked"
        # The original blocked event is the one visible request; reconciliation
        # creates no repeated blocker/user-question events.
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='blocked'",
            (tid,),
        ).fetchone()[0] == 1



def test_manual_needs_input_keeps_manual_authority_even_with_old_failure_count(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="manual after worker failures",
            assignee="dollycode",
            body="contract_id: esc-manual-failures\nrevision: r1",
        )
        _claim_and_block(conn, tid, kind="needs_input", reason="operator must choose recovery")
        conn.execute("UPDATE tasks SET consecutive_failures=9 WHERE id=?", (tid,))
        conn.commit()
        state = kb.get_reconciled_execution_state(conn, tid, failure_limit=2)
        assert state.blocker_type is BlockerType.MANUAL
        assert state.resume_policy is ResumePolicy.MANUAL_ONCE
        assert state.resume_action == "wait_for_owner_once"
        assert kb.reconcile_execution_states(conn, now=10**12) == []


def test_c4_iteration_exhaustion_is_terminal_for_automation(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="bounded worker",
            assignee="dollycode",
            body="contract_id: esc-c4\nrevision: r1",
        )
        _claim_and_block(
            conn,
            tid,
            kind="transient",
            reason="iteration budget exhausted at 40/40",
        )
        _age_latest_block(conn, tid)
        state = kb.get_reconciled_execution_state(conn, tid)
        assert state.blocker_type is BlockerType.ITERATION_EXHAUSTED
        assert state.resume_policy is ResumePolicy.NEVER
        assert state.resume_action == "do_not_resume"
        assert kb.reconcile_execution_states(conn, now=10**12) == []
        assert kb.get_task(conn, tid).status == "blocked"


def test_machine_transient_gets_exactly_one_same_fingerprint_retry(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="transient worker",
            assignee="dollycode",
            body="contract_id: esc-machine\nrevision: r7",
        )
        _claim_and_block(conn, tid, kind="transient", reason="temporary transport reset")
        _age_latest_block(conn, tid)
        state = kb.get_reconciled_execution_state(conn, tid)
        assert state.blocker_type is BlockerType.MACHINE
        assert state.resume_policy is ResumePolicy.BOUNDED_RETRY
        first_fp = state.blocker_fingerprint

        assert kb.reconcile_execution_states(conn, now=10**12) == [tid]
        assert kb.get_task(conn, tid).status == "ready"
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='execution_state_auto_resumed' ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert json.loads(event["payload"])["blocker_fingerprint"] == first_fp

        _claim_and_block(conn, tid, kind="transient", reason="temporary transport reset")
        _age_latest_block(conn, tid)
        second = kb.get_reconciled_execution_state(conn, tid)
        assert second.blocker_fingerprint == first_fp
        assert second.blocker_type is BlockerType.TERMINAL
        assert second.resume_policy is ResumePolicy.NEVER
        assert kb.reconcile_execution_states(conn, now=10**12) == []
        assert kb.get_task(conn, tid).status == "triage"


def test_c5_superseded_or_obsolete_is_non_executable_at_every_boundary(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="old revision",
            assignee="dollycode",
            body="contract_id: esc-c5\nrevision: r1",
        )
        kb.mark_task_for_hygiene(
            conn,
            tid,
            classification="obsolete",
            reason="replacement revision is authoritative",
            actor="test",
        )
        state = kb.get_reconciled_execution_state(conn, tid)
        assert state.blocker_type is BlockerType.TERMINAL
        assert state.executable is False
        assert kb.claim_task(conn, tid, claimer="should-not-run") is None
        assert kb.get_task(conn, tid).status == "ready"
        ok, reason = kb.promote_task(conn, tid, actor="test")
        assert not ok and "non-executable" in reason

        profile_dir = kanban_home / "profiles" / "dollycode"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model:\n  default: gpt-5.6-luna\n")

        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace, board=None: spawned.append(task.id),
            max_spawn=3,
        )
        assert spawned == []
        assert result.spawned == []


def test_c8_obsolete_todo_and_triage_never_become_ready(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="dollycode")
        todo = kb.create_task(conn, title="old todo", assignee="dollycode", parents=[parent])
        triage = kb.create_task(conn, title="old triage", assignee="dollycode", triage=True)
        for tid in (todo, triage):
            kb.mark_task_for_hygiene(
                conn, tid, classification="obsolete", reason="historical", actor="test"
            )
        assert kb.complete_task(conn, parent, summary="done")
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, todo).status == "todo"
        assert kb.get_task(conn, triage).status == "triage"


def test_c9_dispatcher_reconciles_machine_block_without_user_kanban_action(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="machine retry",
            assignee="dollycode",
            body="contract_id: esc-c9\nrevision: r1",
        )
        _claim_and_block(conn, tid, kind="transient", reason="temporary network reset")
        _age_latest_block(conn, tid)
        profile_dir = kanban_home / "profiles" / "dollycode"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model:\n  default: gpt-5.6-luna\n")

        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace, board=None: spawned.append(task.id) or 4242,
            max_spawn=1,
        )
        assert result.auto_resumed == [tid]
        assert spawned == [tid]
        assert kb.get_task(conn, tid).status == "running"


def test_c6_c7_direct_codex_and_hermes_share_repo_canon_and_reconciler_projects_it(
    kanban_home, tmp_path
):
    from hermes_cli.execution_state import (
        read_repo_execution_states,
        upsert_repo_execution_state,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    tasks_md = repo / "TASKS.md"
    tasks_md.write_text("# Tasks\n")

    # C6 shape: the direct-Codex writer and C7 shape: Hermes use the exact same
    # durable repository marker, not separate board/prose state stores.
    upsert_repo_execution_state(
        str(tasks_md),
        {
            "contract_id": "esc-canon",
            "revision": "r1",
            "status": "active",
            "updated_by": "direct_codex",
        },
    )
    assert read_repo_execution_states(str(tasks_md))["esc-canon"]["updated_by"] == "direct_codex"

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="durable projection",
            assignee="dollycode",
            body=(
                f"contract_id: esc-canon\nrevision: r1\n"
                f"canon_path: {tasks_md}\n"
            ),
        )
        assert kb.reconcile_repo_canon_projection(conn) == []
        assert kb.get_task(conn, tid).hygiene_class is None

        upsert_repo_execution_state(
            str(tasks_md),
            {
                "contract_id": "esc-canon",
                "revision": "r2",
                "status": "active",
                "updated_by": "hermes",
            },
        )
        repo_state = read_repo_execution_states(str(tasks_md))["esc-canon"]
        assert repo_state["updated_by"] == "hermes"
        assert repo_state["revision"] == "r2"

        assert kb.reconcile_repo_canon_projection(conn) == [tid]
        projected = kb.get_task(conn, tid)
        assert projected.hygiene_class == "obsolete"
        assert kb.claim_task(conn, tid, claimer="must-not-run-old-r1") is None
