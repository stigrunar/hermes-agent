"""Functional Kanban handoffs preserve caller payloads exactly.

Credential-shaped strings, signed URLs, and continuation tokens can be real
task output. Redaction belongs only on typed diagnostic sinks, not comments,
completion results/summaries/metadata, or human-readable blocker reasons.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="worker-test",
            assignee="test-worker",
        )
        kb.claim_task(conn, task_id)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    return task_id


def _functional_marker() -> str:
    return (
        "sk-syntheticfunctionalvalue123456789 "
        "https://demo-user:demo-pass@example.invalid/private/report"
        "?token=functional-token&code=resume-code résumé 日本語 🔐"
    )


def test_kanban_comment_preserves_functional_payload(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    body = f"comment::{_functional_marker()}\r\ntrailing  "
    result = kt._handle_comment({"task_id": worker_env, "body": body})
    assert json.loads(result)["ok"] is True

    with kb.connect_closing() as conn:
        stored = kb.list_comments(conn, worker_env)[-1].body
    assert stored == body


def test_kanban_complete_preserves_result_summary_and_metadata(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    marker = _functional_marker()
    summary = f"summary::{marker}\r\n"
    result_payload = f"result::{marker}\n  "
    metadata = {
        "signed": marker,
        "nested": ["token=functional-token", "résumé 日本語 🔐"],
    }
    result = kt._handle_complete(
        {
            "summary": summary,
            "result": result_payload,
            "metadata": metadata,
        }
    )
    assert json.loads(result)["ok"] is True

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, worker_env)
        run = kb.latest_run(conn, worker_env)
    assert task.result == result_payload
    assert run.summary == summary
    assert run.metadata["signed"] == metadata["signed"]
    assert run.metadata["nested"] == metadata["nested"]


def test_kanban_block_preserves_human_reason(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    reason = f"blocked::{_functional_marker()}\r\nkeep trailing  "
    result = kt._handle_block({"reason": reason, "kind": "needs_input"})
    assert json.loads(result)["ok"] is True

    with kb.connect_closing() as conn:
        run = kb.latest_run(conn, worker_env)
    assert run.summary == reason
