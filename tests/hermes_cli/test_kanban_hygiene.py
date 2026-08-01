"""Focused runtime coverage for explicit, preview-first Kanban hygiene."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def hygiene_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.init_db()
    return home, db_path


def _task(conn, title: str, status: str = "todo") -> str:
    task_id = kb.create_task(conn, title=title, triage=True)
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
    conn.commit()
    return task_id


def _mark_obsolete(conn, task_id: str, reason: str = "operator retired it"):
    return kb.mark_task_for_hygiene(
        conn,
        task_id,
        classification="obsolete",
        reason=reason,
        actor="operator",
    )


def _run_cli(*argv: str) -> int:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command")
    kanban_cli.build_parser(sub)
    args = root.parse_args(["kanban", *argv])
    return kanban_cli.kanban_command(args)


def test_preview_is_read_only_and_uses_no_title_or_age_heuristic(hygiene_board):
    _, db_path = hygiene_board
    with kb.connect_closing(db_path) as conn:
        marked = _task(conn, "ordinary card")
        _mark_obsolete(conn, marked)
        heuristic_bait = _task(conn, "OLD recut v2 v3 superseded obsolete")
        conn.execute(
            "UPDATE tasks SET created_at=1, body='replacement is t_fake' WHERE id=?",
            (heuristic_bait,),
        )
        conn.commit()
        before = conn.total_changes
        preview = kb.preview_hygiene(conn)
        assert conn.total_changes == before
        assert [item["id"] for item in preview] == [marked]
        assert preview[0]["eligible"] is True
        assert kb.get_task(conn, marked).status == "todo"
        assert kb.get_task(conn, heuristic_bait).status == "todo"


def test_cli_preview_bypasses_auto_init_and_uses_readonly_database(
    hygiene_board, monkeypatch, capsys
):
    _, db_path = hygiene_board
    before = db_path.read_bytes()
    sidecars_before = sorted(path.name for path in db_path.parent.iterdir())

    def unexpected_init(*_args, **_kwargs):
        raise AssertionError("preview must not initialize or migrate the board")

    monkeypatch.setattr(kb, "init_db", unexpected_init)
    assert _run_cli("hygiene", "reconcile", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "preview"
    assert db_path.read_bytes() == before
    assert sorted(path.name for path in db_path.parent.iterdir()) == sidecars_before


def test_terminal_replacement_candidate_and_nonterminal_or_missing_skip(hygiene_board):
    _, db_path = hygiene_board
    with kb.connect_closing(db_path) as conn:
        done_replacement = _task(conn, "accepted replacement", "done")
        good = _task(conn, "old implementation")
        kb.mark_task_for_hygiene(
            conn,
            good,
            classification="superseded",
            replacement_id=done_replacement,
            reason="accepted replacement landed",
            actor="operator",
        )
        archived_replacement = _task(conn, "archived replacement", "archived")
        archived_good = _task(conn, "old archived-path implementation")
        kb.mark_task_for_hygiene(
            conn,
            archived_good,
            classification="superseded",
            replacement_id=archived_replacement,
            reason="replacement was already archived after acceptance",
            actor="operator",
        )
        open_replacement = _task(conn, "unfinished replacement", "todo")
        waiting = _task(conn, "waiting source")
        kb.mark_task_for_hygiene(
            conn,
            waiting,
            classification="superseded",
            replacement_id=open_replacement,
            reason="replacement is intended",
            actor="operator",
        )
        missing = _task(conn, "missing source")
        conn.execute(
            "UPDATE tasks SET hygiene_class='superseded', hygiene_reason='lost', "
            "hygiene_marked_by='operator', superseded_by='t_missing' WHERE id=?",
            (missing,),
        )
        conn.commit()

        by_id = {item["id"]: item for item in kb.preview_hygiene(conn)}
        assert by_id[good]["eligible"] is True
        assert by_id[good]["replacement_status"] == "done"
        assert by_id[archived_good]["eligible"] is True
        assert by_id[archived_good]["replacement_status"] == "archived"
        assert by_id[waiting]["skipped_safety_reason"] == "replacement_not_terminal"
        assert by_id[missing]["skipped_safety_reason"] == "replacement_missing"

        with pytest.raises(ValueError, match="unknown replacement"):
            kb.mark_task_for_hygiene(
                conn,
                missing,
                classification="superseded",
                replacement_id="t_absent",
                reason="still absent",
                actor="operator",
            )
        with pytest.raises(ValueError, match="non-empty durable"):
            kb.mark_task_for_hygiene(
                conn,
                missing,
                classification="obsolete",
                reason="   ",
                actor="operator",
            )
        with pytest.raises(ValueError, match="cannot have a replacement"):
            kb.mark_task_for_hygiene(
                conn,
                missing,
                classification="obsolete",
                replacement_id=done_replacement,
                reason="explicit obsolete reason",
                actor="operator",
            )


@pytest.mark.parametrize(
    ("status", "active_sql", "expected"),
    [
        ("running", None, "source_status_running"),
        ("ready", None, "source_status_ready"),
        ("todo", "UPDATE tasks SET claim_lock='claim' WHERE id=?", "active_claim_or_worker"),
        ("todo", "UPDATE tasks SET worker_pid=123 WHERE id=?", "active_claim_or_worker"),
        ("todo", "UPDATE tasks SET current_run_id=99 WHERE id=?", "active_claim_or_worker"),
    ],
)
def test_running_ready_and_active_claim_fields_skip(
    hygiene_board, status, active_sql, expected
):
    _, db_path = hygiene_board
    with kb.connect_closing(db_path) as conn:
        task_id = _task(conn, "unsafe source", status)
        _mark_obsolete(conn, task_id)
        if active_sql:
            conn.execute(active_sql, (task_id,))
            conn.commit()
        item = kb.preview_hygiene(conn)[0]
        assert item["eligible"] is False
        assert item["skipped_safety_reason"] == expected


def test_running_task_run_and_protected_review_handoff_skip(hygiene_board):
    _, db_path = hygiene_board
    with kb.connect_closing(db_path) as conn:
        active_run = _task(conn, "run source")
        _mark_obsolete(conn, active_run)
        conn.execute(
            "INSERT INTO task_runs(task_id, status, started_at) VALUES (?, 'running', 1)",
            (active_run,),
        )

        protected = _task(conn, "review source")
        review = _task(conn, "review gate", "review")
        _mark_obsolete(conn, protected)
        conn.execute(
            "INSERT INTO review_handoffs(source_task_id, review_task_id, state, "
            "created_at, updated_at) VALUES (?, ?, 'active', 1, 1)",
            (protected, review),
        )
        conn.commit()

        by_id = {item["id"]: item for item in kb.preview_hygiene(conn)}
        assert by_id[active_run]["skipped_safety_reason"] == "running_task_run"
        assert by_id[protected]["skipped_safety_reason"] == "protected_review_handoff"


def test_child_release_hazard_skips_but_safe_same_batch_chain_applies(hygiene_board):
    _, db_path = hygiene_board
    with kb.connect_closing(db_path) as conn:
        unsafe_parent = _task(conn, "unsafe parent")
        open_child = _task(conn, "open child", "todo")
        conn.execute(
            "INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)",
            (unsafe_parent, open_child),
        )
        conn.commit()
        _mark_obsolete(conn, unsafe_parent)
        unsafe = kb.preview_hygiene(conn)[0]
        assert unsafe["skipped_safety_reason"] == f"would_release_open_child:{open_child}"

        safe_parent = _task(conn, "safe parent")
        safe_child = _task(conn, "safe child", "todo")
        conn.execute(
            "INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)",
            (safe_parent, safe_child),
        )
        conn.commit()
        _mark_obsolete(conn, safe_parent)
        _mark_obsolete(conn, safe_child)
        result = kb.apply_hygiene(conn, actor="batch-operator")
        assert result["applied_count"] == 2
        assert kb.get_task(conn, safe_parent).status == "archived"
        assert kb.get_task(conn, safe_child).status == "archived"
        assert kb.get_task(conn, unsafe_parent).status == "todo"


def test_apply_rechecks_stale_preview_under_transaction(hygiene_board, monkeypatch):
    _, db_path = hygiene_board
    with kb.connect_closing(db_path) as conn:
        task_id = _task(conn, "racy source")
        _mark_obsolete(conn, task_id)

        def make_stale(connection, _preview):
            connection.execute(
                "UPDATE tasks SET status='ready' WHERE id=?", (task_id,)
            )
            connection.commit()

        monkeypatch.setattr(kb, "_hygiene_before_apply_txn", make_stale)
        result = kb.apply_hygiene(conn, actor="operator")
        item = result["candidates"][0]
        assert result["applied_count"] == 0
        assert item["skipped_safety_reason"] == "source_status_ready"
        assert kb.get_task(conn, task_id).status == "ready"


def test_apply_creates_backup_and_structured_silent_audit(hygiene_board):
    _, db_path = hygiene_board
    with kb.connect_closing(db_path) as conn:
        task_id = _task(conn, "archive me")
        _mark_obsolete(conn, task_id, "policy explicitly retired this")
        result = kb.apply_hygiene(conn, actor="nightly-hygiene")
        assert result["applied_count"] == 1
        backup_path = Path(result["backup_path"])
        assert backup_path.exists()
        with sqlite3.connect(backup_path) as backup:
            assert backup.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()[0] == "todo"

        events = kb.list_events(conn, task_id)
        archived = [event for event in events if event.kind == "hygiene_archived"]
        assert len(archived) == 1
        payload = archived[0].payload
        assert payload == {
            "actor": "nightly-hygiene",
            "closure_class": "obsolete",
            "reason": "policy explicitly retired this",
            "replacement": None,
            "batch_id": result["batch_id"],
            "prior_status": "todo",
        }
        assert not {"archived", "completed"} & {event.kind for event in events}
        comment = kb.list_comments(conn, task_id)[-1]
        assert comment.author == "nightly-hygiene"
        assert result["batch_id"] in comment.body
        assert "prior_status" in comment.body


def test_second_apply_is_idempotent_noop_without_another_backup(hygiene_board):
    _, db_path = hygiene_board
    with kb.connect_closing(db_path) as conn:
        task_id = _task(conn, "once only")
        _mark_obsolete(conn, task_id)
        first = kb.apply_hygiene(conn, actor="operator")
        backups_before = set(db_path.parent.glob("*.hygiene-*.bak"))
        second = kb.apply_hygiene(conn, actor="operator")
        assert first["applied_count"] == 1
        assert second["applied_count"] == 0
        assert second["batch_id"] is None
        assert second["candidates"][0]["skipped_safety_reason"] == "already_archived"
        assert set(db_path.parent.glob("*.hygiene-*.bak")) == backups_before


def test_cli_json_contract_and_healthy_apply_noop_is_silent(
    hygiene_board, capsys
):
    _, db_path = hygiene_board
    with kb.connect_closing(db_path) as conn:
        task_id = _task(conn, "machine candidate")

    assert _run_cli(
        "hygiene",
        "mark-obsolete",
        task_id,
        "--reason",
        "machine reason",
        "--actor",
        "operator",
        "--json",
    ) == 0
    mark_payload = json.loads(capsys.readouterr().out)
    assert mark_payload["classification"] == "obsolete"
    assert mark_payload["reason"] == "machine reason"

    assert _run_cli("hygiene", "reconcile", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "preview"
    candidate = payload["candidates"][0]
    assert candidate == {
        "id": task_id,
        "title": "machine candidate",
        "status": "todo",
        "classification": "obsolete",
        "replacement_id": None,
        "replacement_status": None,
        "reason": "machine reason",
        "marked_by": "operator",
        "eligible": True,
        "applied": False,
        "skipped_safety_reason": None,
    }

    assert _run_cli("hygiene", "reconcile", "--apply") == 0
    applied_output = capsys.readouterr().out
    assert len(applied_output.strip().splitlines()) == 1
    assert task_id not in applied_output
    assert _run_cli("hygiene", "reconcile", "--apply") == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_cli_mark_superseded_persists_replacement_and_reason(
    hygiene_board, capsys
):
    _, db_path = hygiene_board
    with kb.connect_closing(db_path) as conn:
        replacement = _task(conn, "replacement", "done")
        source = _task(conn, "source")

    assert _run_cli(
        "hygiene",
        "mark-superseded",
        source,
        replacement,
        "--reason",
        "replacement accepted",
        "--actor",
        "operator",
        "--json",
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["replacement_id"] == replacement
    with kb.connect_closing(db_path) as conn:
        task = kb.get_task(conn, source)
        assert task.hygiene_class == "superseded"
        assert task.hygiene_reason == "replacement accepted"
        assert task.superseded_by == replacement
