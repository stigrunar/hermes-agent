"""Focused canaries for terminal owner-replan durability."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()


def _terminal_task(conn, *, body_extra: str = "") -> tuple[str, int]:
    tid = kb.create_task(
        conn,
        title="owner replan fixture",
        body=("contract_id: owner-replan\nrevision: r1\n" + body_extra).strip(),
        assignee="dollycode",
        tenant="fixture-project",
        workspace_kind="dir",
        workspace_path="/tmp/owner-replan-fixture",
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
    rows = conn.execute(
        "SELECT id, run_id, payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
        (tid, kind),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "run_id": row["run_id"],
            "payload": json.loads(row["payload"] or "{}"),
        }
        for row in rows
    ]


def _semantic_task(
    conn,
    *,
    body_extra: str = "",
    project_id: str = "adopted-project",
    add_route: bool = True,
) -> tuple[str, int]:
    tid, run_id = _terminal_task(
        conn,
        body_extra=(
            "topic_target: telegram:-1001:87\n" + body_extra
        ),
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET project_id=? WHERE id=?", (project_id, tid))
        if not add_route:
            conn.execute(
                "DELETE FROM kanban_notify_subs WHERE task_id=?", (tid,)
            )
    return tid, run_id


def _complete_semantic(conn, tid: str, run_id: int, metadata: dict) -> bool:
    return kb.complete_task(
        conn,
        tid,
        expected_run_id=run_id,
        summary="terminal worker left a preserved artifact",
        metadata=metadata,
    )


def test_completed_semantic_envelope_appends_one_owner_intent_in_close_txn(isolated_home):
    with kb.connect() as conn:
        tid, run_id = _semantic_task(conn)
        metadata = {
            "owner_replan": {
                "owner": "default",
                "action": "inspect the preserved patch and materialize one current revision",
                "authority": "agent_internal",
                "needs_user_decision": False,
            },
            "topic_target": "telegram:-1001:87",
        }
        assert _complete_semantic(conn, tid, run_id, metadata)
        [intent] = _events(conn, tid, "needs_owner_replan")
        assert intent["run_id"] == run_id
        payload = intent["payload"]
        assert payload["semantic_outcome"] == "completed"
        assert payload["project_id"] == "adopted-project"
        assert payload["topic_target"] == "telegram:-1001:87"
        assert payload["continuation_of"] == tid
        assert payload["owner_route"]["platform"] == "telegram"
        assert payload["owner_replan"]["authority"] == "agent_internal"
        assert payload["owner_replan"]["needs_user_decision"] is False
        assert len(_events(conn, tid, "completed")) == 1
        assert len(_events(conn, tid, "needs_owner_replan")) == 1


@pytest.mark.parametrize(
    "metadata",
    [
        {"outcome": "completed", "next_step": "Dolly/default: inspect patch"},
        {"outcome": "changes_requested", "next_step": "inspect patch", "next_owner": "reviewer"},
        {"outcome": "rolled_back", "next_step": "inspect patch"},
        {"outcome": "completed", "owner_replan": {"owner": "default", "action": "inspect", "authority": "human", "needs_user_decision": False}},
        {"owner_replan": {"owner": "default", "action": "inspect", "authority": "agent_internal", "needs_user_decision": True}},
        {"owner_replan": {"owner": "default", "action": "wait for human approval", "authority": "agent_internal", "needs_user_decision": False}},
    ],
)
def test_semantic_completion_fails_closed_without_explicit_safe_envelope(
    isolated_home, metadata,
):
    with kb.connect() as conn:
        tid, run_id = _semantic_task(conn)
        assert _complete_semantic(conn, tid, run_id, metadata)
        assert _events(conn, tid, "needs_owner_replan") == []


def test_legacy_semantic_bridge_requires_explicit_default_owner(isolated_home):
    with kb.connect() as conn:
        accepted, accepted_run = _semantic_task(conn)
        assert _complete_semantic(
            conn,
            accepted,
            accepted_run,
            {
                "outcome": "rolled_back",
                "next_step": "restore the preserved patch",
                "next_owner": "default",
            },
        )
        [intent] = _events(conn, accepted, "needs_owner_replan")
        assert intent["payload"]["semantic_outcome"] == "rolled_back"

        anchored, anchored_run = _semantic_task(conn)
        assert _complete_semantic(
            conn,
            anchored,
            anchored_run,
            {
                "outcome": "changes_requested",
                "next_step": "Dolly/default: inspect the preserved patch",
            },
        )
        [intent] = _events(conn, anchored, "needs_owner_replan")
        assert intent["payload"]["semantic_outcome"] == "changes_requested"


@pytest.mark.parametrize(
    "outcome,next_step",
    [
        ("rolled_back", "Dolly/default: manual action required"),
        ("changes_requested", "manual-only follow-up required"),
    ],
)
def test_explicit_manual_next_step_stays_manual_without_intent_or_wake(
    isolated_home, outcome, next_step,
):
    with kb.connect() as conn:
        tid, run_id = _semantic_task(conn)
        assert _complete_semantic(
            conn,
            tid,
            run_id,
            {
                "outcome": outcome,
                "next_step": next_step,
                "next_owner": "default",
            },
        )
        assert _events(conn, tid, "needs_owner_replan") == []
        assert kb.claim_owner_replan_for_route(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="-1001",
            thread_id="87",
        ) is None
        assert _events(conn, tid, "owner_replan_wake_claimed") == []


def test_autonomous_manual_noun_next_step_remains_intent_eligible(isolated_home):
    with kb.connect() as conn:
        tid, run_id = _semantic_task(conn)
        assert _complete_semantic(
            conn,
            tid,
            run_id,
            {
                "outcome": "rolled_back",
                "next_step": "update the operator manual and rerun checks",
                "next_owner": "default",
            },
        )
        [intent] = _events(conn, tid, "needs_owner_replan")
        assert intent["payload"]["action"] == "update the operator manual and rerun checks"


def test_missing_project_topic_or_owner_route_is_ineligible(isolated_home):
    with kb.connect() as conn:
        missing_project, run_id = _semantic_task(conn, project_id="")
        assert _complete_semantic(
            conn,
            missing_project,
            run_id,
            {"owner_replan": {"owner": "default", "action": "inspect", "authority": "agent_internal", "needs_user_decision": False}},
        )
        assert _events(conn, missing_project, "needs_owner_replan") == []

        missing_topic, run_id = _semantic_task(conn, body_extra="topic_target:")
        assert _complete_semantic(
            conn,
            missing_topic,
            run_id,
            {"owner_replan": {"owner": "default", "action": "inspect", "authority": "agent_internal", "needs_user_decision": False}},
        )
        assert _events(conn, missing_topic, "needs_owner_replan") == []

        missing_route, run_id = _semantic_task(conn, add_route=False)
        assert _complete_semantic(
            conn,
            missing_route,
            run_id,
            {"owner_replan": {"owner": "default", "action": "inspect", "authority": "agent_internal", "needs_user_decision": False}},
        )
        assert _events(conn, missing_route, "needs_owner_replan") == []


def test_semantic_owner_intent_is_not_retried_or_duplicated(isolated_home):
    with kb.connect() as conn:
        tid, run_id = _semantic_task(conn)
        metadata = {
            "owner_replan": {
                "owner": "default",
                "action": "inspect the preserved patch",
                "authority": "agent_internal",
                "needs_user_decision": False,
            }
        }
        assert _complete_semantic(conn, tid, run_id, metadata)
        assert kb.complete_task(conn, tid, metadata=metadata) is False
        assert len(_events(conn, tid, "needs_owner_replan")) == 1
        first = kb.claim_owner_replan_for_route(
            conn, task_id=tid, platform="telegram", chat_id="-1001", thread_id="87",
        )
        second = kb.claim_owner_replan_for_route(
            conn, task_id=tid, platform="telegram", chat_id="-1001", thread_id="87",
        )
        assert first is not None
        assert second is None


def test_iteration_exhaustion_is_terminal_and_one_shot(isolated_home):
    with kb.connect() as conn:
        tid, run_id = _terminal_task(conn)
        assert kb._record_task_failure(
            conn,
            tid,
            error="budget exhausted",
            outcome="timed_out",
            release_claim=True,
            end_run=True,
            event_payload_extra={"budget_used": 60, "budget_max": 60},
        )
        assert kb._record_task_failure(
            conn,
            tid,
            error="budget exhausted",
            outcome="timed_out",
            release_claim=True,
            end_run=True,
            event_payload_extra={"budget_used": 60, "budget_max": 60},
        )
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "iteration_exhausted"
        assert task.max_retries == 0
        assert task.current_run_id is None
        assert kb.claim_task(conn, tid, claimer="must-not-retry") is None
        assert kb.unblock_task(conn, tid) is False
        assert kb.promote_task(conn, tid, actor="operator")[0] is False
        assert len(_events(conn, tid, "iteration_exhausted")) == 1
        [intent] = _events(conn, tid, "needs_owner_replan")
        assert intent["run_id"] == run_id
        assert intent["payload"]["resume_policy"] == "never"
        assert intent["payload"]["retryable"] is False
        first = kb.claim_owner_replan_for_route(
            conn, task_id=tid, platform="telegram", chat_id="-1001", thread_id="87",
        )
        second = kb.claim_owner_replan_for_route(
            conn, task_id=tid, platform="telegram", chat_id="-1001", thread_id="87",
        )
        assert first is not None
        assert second is None
        assert len(_events(conn, tid, "owner_replan_wake_claimed")) == 1


def test_iteration_exhaustion_does_not_rewrite_completed_task(isolated_home):
    with kb.connect() as conn:
        tid, run_id = _terminal_task(conn)
        assert kb.complete_task(conn, tid, expected_run_id=run_id, result="done")

        assert kb._record_iteration_exhaustion(
            conn,
            tid,
            budget_used=60,
            budget_max=60,
            expected_run_id=run_id,
        ) is None

        task = kb.get_task(conn, tid)
        assert task.status == "done"
        run = conn.execute(
            "SELECT outcome FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert run["outcome"] == "completed"
        assert _events(conn, tid, "iteration_exhausted") == []


def test_stale_iteration_finalizer_cannot_close_successor_run(isolated_home):
    with kb.connect() as conn:
        tid, first_run_id = _terminal_task(conn)
        assert kb.reclaim_task(
            conn, tid, reason="replace worker", signal_fn=lambda *_args: None,
        )
        successor = kb.claim_task(conn, tid, claimer="successor-worker")
        assert successor is not None and successor.current_run_id is not None
        second_run_id = int(successor.current_run_id)
        assert second_run_id != first_run_id

        assert kb._record_iteration_exhaustion(
            conn,
            tid,
            budget_used=60,
            budget_max=60,
            expected_run_id=first_run_id,
        ) is None

        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.current_run_id == second_run_id
        runs = conn.execute(
            "SELECT id, status FROM task_runs WHERE id IN (?, ?)",
            (first_run_id, second_run_id),
        ).fetchall()
        statuses = {int(row["id"]): row["status"] for row in runs}
        assert statuses[first_run_id] == "reclaimed"
        assert statuses[second_run_id] == "running"


def test_successor_and_superseded_metadata_suppress_intent(isolated_home):
    with kb.connect() as conn:
        old, _ = _terminal_task(conn)
        replacement = kb.create_task(
            conn,
            title="replacement",
            body="contract_id: owner-replan\nrevision: r2",
            assignee="default",
            tenant="fixture-project",
        )
        assert kb.claim_task(conn, replacement, claimer="owner") is not None
        # The successor is present before terminalization, so no intent is
        # created for the obsolete revision.
        assert kb._record_iteration_exhaustion(conn, old, budget_used=60, budget_max=60)
        assert _events(conn, old, "needs_owner_replan") == []

        stale, _ = _terminal_task(
            conn, body_extra=f"hygiene_class: superseded\nsuperseded_by: {replacement}",
        )
        assert kb._record_iteration_exhaustion(conn, stale, budget_used=60, budget_max=60)
        assert _events(conn, stale, "needs_owner_replan") == []

        obsolete, _ = _terminal_task(
            conn, body_extra="hygiene_class: obsolete\nhygiene_reason: old revision",
        )
        assert kb._record_iteration_exhaustion(conn, obsolete, budget_used=60, budget_max=60)
        assert _events(conn, obsolete, "needs_owner_replan") == []


def test_needs_user_decision_is_manual_without_owner_intent(isolated_home):
    with kb.connect() as conn:
        tid, run_id = _terminal_task(conn)
        snapshot = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        with kb.write_txn(conn):
            assert kb._ensure_owner_replan_event(
                conn,
                snapshot,
                terminal_run_id=run_id,
                end_reason="needs_user_decision",
            ) is None
        assert _events(conn, tid, "needs_owner_replan") == []


def test_default_ack_is_durable_and_failure_is_manual_only(isolated_home):
    with kb.connect() as conn:
        tid, _ = _terminal_task(conn)
        kb._record_iteration_exhaustion(conn, tid, budget_used=60, budget_max=60)
        [intent] = _events(conn, tid, "needs_owner_replan")
        claimed = kb.claim_owner_replan_for_route(
            conn, task_id=tid, platform="telegram", chat_id="-1001", thread_id="87",
        )
        assert claimed is not None
        assert kb.mark_owner_replan_failed(
            conn,
            tid,
            fingerprint=intent["payload"]["fingerprint"],
            replan_event_id=intent["id"],
            error="wake failed",
        )
        assert kb.claim_owner_replan_for_route(
            conn, task_id=tid, platform="telegram", chat_id="-1001", thread_id="87",
        ) is None
        [failure] = _events(conn, tid, "owner_replan_failed")
        assert failure["payload"]["resume_policy"] == "manual"
        assert failure["payload"]["retryable"] is False

    with kb.connect() as conn:
        tid, _ = _terminal_task(conn)
        kb._record_iteration_exhaustion(conn, tid, budget_used=60, budget_max=60)
        [intent] = _events(conn, tid, "needs_owner_replan")
        fingerprint = intent["payload"]["fingerprint"]
        kb.add_comment(conn, tid, author="worker", body=f"owner_replan_ack: {fingerprint}")
        assert _events(conn, tid, "owner_replan_acknowledged") == []
        kb.add_comment(conn, tid, author="default", body=f"owner_replan_ack: {fingerprint}")
        assert len(_events(conn, tid, "owner_replan_acknowledged")) == 1
        assert kb.claim_owner_replan_for_route(
            conn, task_id=tid, platform="telegram", chat_id="-1001", thread_id="87",
        ) is None


def test_successor_created_after_intent_suppresses_claim(isolated_home):
    with kb.connect() as conn:
        tid, _ = _terminal_task(conn)
        kb._record_iteration_exhaustion(conn, tid, budget_used=60, budget_max=60)
        replacement = kb.create_task(
            conn,
            title="replacement after terminal event",
            body="contract_id: owner-replan\nrevision: r2",
            assignee="default",
            tenant="fixture-project",
        )
        assert kb.claim_task(conn, replacement, claimer="owner") is not None
        assert kb.claim_owner_replan_for_route(
            conn, task_id=tid, platform="telegram", chat_id="-1001", thread_id="87",
        ) is None
        assert len(_events(conn, tid, "owner_replan_suppressed")) == 1
