"""Focused proof for explicit Kanban worker capability admission."""

from __future__ import annotations

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
