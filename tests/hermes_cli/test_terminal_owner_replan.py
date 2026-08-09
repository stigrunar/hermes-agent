"""Focused deterministic canaries for terminal non-retryable owner replan."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _terminal_task(
    conn,
    *,
    contract: str = "owner-replan",
    revision: str = "r1",
    body_extra: str = "",
    workspace: str = "/tmp/owner-replan-fixture",
) -> tuple[str, int]:
    body = f"contract_id: {contract}\nrevision: {revision}\n{body_extra}".strip()
    tid = kb.create_task(
        conn,
        title=f"owner replan {revision}",
        body=body,
        assignee="dollycode",
        tenant="fixture-project",
        workspace_kind="dir",
        workspace_path=workspace,
        max_retries=0,
    )
    claimed = kb.claim_task(conn, tid, claimer="fixture-worker")
    assert claimed is not None and claimed.current_run_id is not None
    kb.add_notify_sub(
        conn,
        task_id=tid,
        platform="telegram",
        chat_id="-1001",
        chat_type="group",
        thread_id="87",
        notifier_profile="default",
    )
    return tid, int(claimed.current_run_id)


def _events(conn, tid: str, kind: str) -> list[dict]:
    out = []
    for row in conn.execute(
        "SELECT id, run_id, payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
        (tid, kind),
    ).fetchall():
        payload = json.loads(row["payload"]) if row["payload"] else {}
        out.append({"id": row["id"], "run_id": row["run_id"], "payload": payload})
    return out


def test_c1_c2_iteration_exhaustion_creates_one_owner_replan_and_zero_retry(isolated_home):
    with kb.connect() as conn:
        tid, run_id = _terminal_task(conn)
        assert kb._record_iteration_exhaustion(conn, tid, budget_used=60, budget_max=60) == run_id
        # Repeated terminal finalization is idempotent and must not create a second event.
        assert kb._record_iteration_exhaustion(conn, tid, budget_used=60, budget_max=60) == run_id

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "iteration_exhausted"
        assert task.max_retries == 0
        assert task.current_run_id is None
        assert kb.claim_task(conn, tid, claimer="must-not-retry") is None
        assert kb.dispatch_once(conn, dry_run=True).spawned == []

        [replan] = _events(conn, tid, "needs_owner_replan")
        payload = replan["payload"]
        assert payload["owner"] == "default"
        assert payload["project"] == "fixture-project"
        assert payload["board"] == "default"
        assert payload["topic"] == "telegram:-1001:87"
        assert payload["terminal_run_id"] == run_id
        assert payload["contract_id"] == "owner-replan"
        assert payload["revision"] == "r1"
        assert payload["end_reason"] == "iteration_exhausted"
        assert payload["resume_policy"] == "never"
        assert payload["retryable"] is False
        assert payload["artifact_state"] == "unknown"

        first = kb.claim_owner_replan_for_route(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="-1001",
            thread_id="87",
        )
        second = kb.claim_owner_replan_for_route(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="-1001",
            thread_id="87",
        )
        assert first is not None
        assert first["fingerprint"] == payload["fingerprint"]
        assert second is None
        assert len(_events(conn, tid, "owner_replan_wake_claimed")) == 1
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == 1


def test_c3_active_same_contract_replacement_suppresses_owner_replan(isolated_home):
    with kb.connect() as conn:
        old, _ = _terminal_task(conn, contract="same-contract", revision="r1")
        replacement = kb.create_task(
            conn,
            title="replacement r2",
            body="contract_id: same-contract\nrevision: r2",
            assignee="default",
            tenant="fixture-project",
        )
        claimed = kb.claim_task(conn, replacement, claimer="owner")
        assert claimed is not None

        kb._record_iteration_exhaustion(conn, old, budget_used=60, budget_max=60)
        assert _events(conn, old, "needs_owner_replan") == []
        assert kb.get_task(conn, replacement).status == "running"


def test_c3_legacy_explicit_continuation_suppresses_owner_replan(isolated_home):
    with kb.connect() as conn:
        old = kb.create_task(
            conn,
            title="legacy old revision",
            assignee="dollycode",
            tenant="fixture-project",
            max_retries=0,
        )
        claimed = kb.claim_task(conn, old, claimer="legacy-worker")
        assert claimed is not None
        kb.add_notify_sub(
            conn,
            task_id=old,
            platform="telegram",
            chat_id="-1001",
            chat_type="group",
            thread_id="87",
            notifier_profile="default",
        )
        replacement = kb.create_task(
            conn,
            title="legacy explicit replacement",
            body=f"continuation_of: {old}",
            assignee="default",
            tenant="fixture-project",
        )
        assert kb.claim_task(conn, replacement, claimer="owner") is not None
        kb._record_iteration_exhaustion(conn, old, budget_used=60, budget_max=60)
        assert _events(conn, old, "needs_owner_replan") == []


def test_c4_repo_canon_complete_suppresses_owner_replan(isolated_home, tmp_path: Path):
    canon = tmp_path / "TASKS.md"
    canon.write_text(
        '<!-- HERMES_EXECUTION_STATE {"contract_id":"canon-contract","revision":"r1","status":"done"} -->\n',
        encoding="utf-8",
    )
    with kb.connect() as conn:
        tid, _ = _terminal_task(
            conn,
            contract="canon-contract",
            revision="r1",
            body_extra=f"canon_path: {canon}",
        )
        kb._record_iteration_exhaustion(conn, tid, budget_used=60, budget_max=60)
        assert _events(conn, tid, "needs_owner_replan") == []


def test_c5_superseded_revision_suppresses_owner_replan_and_stays_nonexec(isolated_home):
    with kb.connect() as conn:
        tid, _ = _terminal_task(conn, contract="sup-contract", revision="r1")
        conn.execute("UPDATE tasks SET superseded_by='t_replacement' WHERE id=?", (tid,))
        conn.commit()
        kb._record_iteration_exhaustion(conn, tid, budget_used=60, budget_max=60)
        assert _events(conn, tid, "needs_owner_replan") == []
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "iteration_exhausted"
        assert kb.claim_task(conn, tid, claimer="must-not-run") is None


def test_c6_needs_user_decision_remains_manual_without_owner_event(isolated_home):
    with kb.connect() as conn:
        tid, run_id = _terminal_task(conn, contract="manual-contract", revision="r1")
        snapshot = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        with kb.write_txn(conn):
            fingerprint = kb._ensure_owner_replan_event(
                conn,
                snapshot,
                terminal_run_id=run_id,
                end_reason="needs_user_decision",
            )
        assert fingerprint is None
        assert _events(conn, tid, "needs_owner_replan") == []


def test_c7_ack_comment_is_durable_and_default_owner_only(isolated_home):
    with kb.connect() as conn:
        tid, _ = _terminal_task(conn, contract="ack-contract", revision="r1")
        kb._record_iteration_exhaustion(conn, tid, budget_used=60, budget_max=60)
        [replan] = _events(conn, tid, "needs_owner_replan")
        fingerprint = replan["payload"]["fingerprint"]
        claimed = kb.claim_owner_replan_for_route(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="-1001",
            thread_id="87",
        )
        assert claimed is not None
        assert kb.mark_owner_replan_delivered(
            conn,
            tid,
            fingerprint=fingerprint,
            replan_event_id=replan["id"],
        )
        kb.add_comment(conn, tid, author="worker", body=f"owner_replan_ack: {fingerprint}")
        assert _events(conn, tid, "owner_replan_acknowledged") == []
        kb.add_comment(conn, tid, author="default", body=f"owner_replan_ack: {fingerprint}")
        assert len(_events(conn, tid, "owner_replan_acknowledged")) == 1


def test_c9_owner_wake_failure_is_manual_once_and_never_reclaimed(isolated_home):
    with kb.connect() as conn:
        tid, _ = _terminal_task(conn, contract="fail-contract", revision="r1")
        kb._record_iteration_exhaustion(conn, tid, budget_used=60, budget_max=60)
        [replan] = _events(conn, tid, "needs_owner_replan")
        first = kb.claim_owner_replan_for_route(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="-1001",
            thread_id="87",
        )
        assert first is not None
        assert kb.mark_owner_replan_failed(
            conn,
            tid,
            fingerprint=first["fingerprint"],
            replan_event_id=replan["id"],
            error="simulated owner continuation failure",
        )
        assert kb.claim_owner_replan_for_route(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="-1001",
            thread_id="87",
        ) is None
        [failed] = _events(conn, tid, "owner_replan_failed")
        assert failed["payload"]["resume_policy"] == "manual"
        assert failed["payload"]["retryable"] is False
        assert len(_events(conn, tid, "owner_replan_wake_claimed")) == 1
