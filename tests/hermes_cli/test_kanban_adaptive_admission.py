"""Focused proof for explicit Kanban worker capability admission."""

from __future__ import annotations

import os

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def test_missing_capability_rejected_before_claim(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kbc.init_db()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "worker")
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "ok")

    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="requires terminal", assignee="worker",
            required_capabilities=["workspace", "terminal", "local_file_hash"],
        )
        result = kbd.dispatch_once(
            conn, spawn_fn=lambda *_a, **_k: 101,
            worker_toolsets=["file"], max_spawn=1,
        )
        task = kb.get_task(conn, task_id)

    assert result.spawned == []
    assert result.capability_rejections[0]["missing_capabilities"] == [
        "local_file_hash", "terminal"
    ]
    assert task is not None and task.status == "ready"
    assert task.claim_lock is None and task.current_run_id is None


def test_capability_override_reaches_spawn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kbc.init_db()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "worker")
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "ok")
    seen = {}

    def spawn(task, workspace, *, worker_toolsets=None):
        seen["toolsets"] = worker_toolsets
        return 102

    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="requires terminal", assignee="worker",
            required_capabilities=["terminal"],
        )
        result = kbd.dispatch_once(
            conn, spawn_fn=spawn, worker_toolsets=["terminal"], max_spawn=1,
        )

    assert [item[0] for item in result.spawned] == [task_id]
    assert seen["toolsets"] == ["terminal"]


def test_serial_zero_new_spawn_budget_leaves_ready_and_review_unclaimed(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kbc.init_db()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    spawn_calls = []
    with kbc.connect_closing() as conn:
        ready_id = kb.create_task(conn, title="ready", assignee="alice")
        review_id = kb.create_task(conn, title="review", assignee="alice")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review_id,))
        conn.commit()
        result = kbd.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawn_calls.append(task.id) or os.getpid(),
            max_spawn=1,
            max_in_progress=1,
            max_new_spawns=0,
        )
        assert result.spawned == []
        assert spawn_calls == []
        for task_id, status in ((ready_id, "ready"), (review_id, "review")):
            task = kb.get_task(conn, task_id)
            assert task is not None and task.status == status
            assert task.claim_lock is None and task.current_run_id is None


def test_serial_positive_new_spawn_budget_caps_ready_and_review_together(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kbc.init_db()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    spawn_calls = []
    with kbc.connect_closing() as conn:
        ready_id = kb.create_task(conn, title="ready", assignee="alice")
        review_id = kb.create_task(conn, title="review", assignee="alice")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review_id,))
        conn.commit()
        result = kbd.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawn_calls.append(task.id) or os.getpid(),
            max_spawn=1,
            max_in_progress=1,
            max_new_spawns=1,
        )
        assert len(result.spawned) == 1
        assert spawn_calls == [review_id]
        assert kb.get_task(conn, ready_id).status == "ready"
        assert kb.get_task(conn, review_id).status == "running"
