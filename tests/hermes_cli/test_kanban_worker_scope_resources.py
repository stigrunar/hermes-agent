"""Strict resource argv and parallel fallback policy for Kanban workers."""

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


def _target():
    return kb._SystemdUserManagerTarget(
        1000, Path("/run/user/1000"), Path("/run/user/1000/bus")
    )


def test_scope_argv_carries_validated_resource_controls(monkeypatch):
    monkeypatch.setattr(kb.sys, "platform", "linux")
    task = SimpleNamespace(id="task", current_run_id=3)
    config = {
        "worker_scope": {
            "enabled": True,
            "slice": "ops-workers.slice",
            "memory_high": "1536M",
            "memory_max": "2G",
            "memory_swap_max": "256M",
            "tasks_max": 384,
            "oom_policy": "stop",
        }
    }

    argv, unit, target = kb._systemd_scope_argv(
        ["/bin/sleep", "10"],
        task,
        cgroup_path="/user.slice/user-1000.slice/user@1000.service/app.slice/hermes.service",
        manager_target=_target(),
        systemd_run="/usr/bin/systemd-run",
        user_manager_ready=True,
        kanban_cfg=config,
    )

    assert unit is not None and target is not None
    delimiter = argv.index("--")
    prefix = argv[:delimiter]
    assert "--slice=ops-workers.slice" in prefix
    assert "--property=MemoryHigh=1536M" in prefix
    assert "--property=MemoryMax=2G" in prefix
    assert "--property=MemorySwapMax=256M" in prefix
    assert "--property=TasksMax=384" in prefix
    assert "--property=OOMPolicy=stop" in prefix
    # systemd 255 rejects MemoryOOMGroup as an unknown assignment. OOMPolicy
    # plus exact scope stop/reaping is the supported unit-kill contract.
    assert not any(arg.startswith("--property=MemoryOOMGroup=") for arg in prefix)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("slice", "bad.service"),
        ("memory_high", "2G --property=Delegate=yes"),
        ("memory_max", "infinity"),
        ("memory_swap_max", "-1"),
        ("tasks_max", True),
        ("tasks_max", 0),
        ("oom_policy", "continue"),
        ("memory_high", "4G"),
    ],
)
def test_malformed_scope_resources_are_rejected(key, value):
    config = {key: value}
    if key == "memory_high" and value == "4G":
        config["memory_max"] = "3G"
    with pytest.raises(ValueError):
        kb._worker_scope_config({"worker_scope": config})


def test_parallel_dispatch_fails_closed_without_scope(monkeypatch):
    monkeypatch.setattr(kb.sys, "platform", "darwin")
    task = SimpleNamespace(id="task", current_run_id=3)

    with pytest.raises(RuntimeError, match="Kanban dispatch requires"):
        kb._systemd_scope_argv(
            ["/bin/sleep", "10"], task, require_scope=True,
        )

    direct, unit, target = kb._systemd_scope_argv(
        ["/bin/sleep", "10"], task, require_scope=False,
    )
    assert direct == ["/bin/sleep", "10"]
    assert unit is None and target is None


def test_host_required_scope_fails_closed_for_first_worker(monkeypatch):
    """A multi-board wrapper can require scopes before local overlap exists."""
    monkeypatch.setattr(kb.sys, "platform", "darwin")
    task = SimpleNamespace(id="task", current_run_id=3)

    with pytest.raises(RuntimeError, match="Kanban dispatch requires"):
        kb._systemd_scope_argv(
            ["/bin/sleep", "10"],
            task,
            require_scope=False,
            kanban_cfg={"worker_scope": {"required": True}},
        )


def test_required_scope_must_be_boolean():
    with pytest.raises(ValueError, match="required must be a boolean"):
        kb._worker_scope_config({"worker_scope": {"required": "yes"}})


def test_scope_resource_receipt_round_trips_and_rebuild_preserves_columns(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "kanban.db"
    target = _target()
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    with kb.connect(db_path) as conn:
        task_id = kb.create_task(conn, title="receipt", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        kb._set_worker_pid(
            conn,
            task_id,
            kb._WorkerLaunchPid(
                1234,
                launch_mode="systemd-user-scope",
                scope_unit=kb._systemd_scope_unit_name(
                    task_id, run_id, db_path=db_path
                ),
                verification_status="verified",
                manager_kind=kb._SYSTEMD_USER_MANAGER_KIND,
                manager_uid=os.getuid(),
                launch_acknowledged=True,
                scope_slice="ops-workers.slice",
                memory_high="1536M",
                memory_max="2G",
                memory_swap_max="256M",
                tasks_max=384,
                oom_policy="stop",
                control_group="/user.slice/user-1000.slice/worker.scope",
            ),
        )
        receipt = kb.get_run(conn, run_id)
        assert receipt is not None
        assert receipt.scope_slice == "ops-workers.slice"
        assert receipt.memory_high == "1536M"
        assert receipt.memory_max == "2G"
        assert receipt.memory_swap_max == "256M"
        assert receipt.tasks_max == 384
        assert receipt.oom_policy == "stop"
        assert receipt.control_group == "/user.slice/user-1000.slice/worker.scope"

    with kb.connect(db_path) as reopened:
        receipt = kb.get_run(reopened, run_id)
        assert receipt is not None
        assert receipt.scope_slice == "ops-workers.slice"
        assert receipt.memory_max == "2G"
        assert receipt.control_group.endswith("worker.scope")

    legacy = tmp_path / "legacy.db"
    raw = sqlite3.connect(legacy)
    raw.execute(
        "CREATE TABLE task_runs ("
        "id TEXT PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT, step_key TEXT,"
        "status TEXT NOT NULL, claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER,"
        "launch_mode TEXT, scope_unit TEXT, manager_kind TEXT, manager_uid INTEGER,"
        "launch_acknowledged INTEGER, verification_status TEXT, scope_slice TEXT,"
        "memory_high TEXT, memory_max TEXT, memory_swap_max TEXT, tasks_max INTEGER,"
        "oom_policy TEXT, control_group TEXT, terminal_action TEXT, terminal_payload TEXT,"
        "reap_state TEXT, reap_requested_at INTEGER, reap_completed_at INTEGER, reap_error TEXT,"
        "max_runtime_seconds INTEGER, last_heartbeat_at INTEGER, started_at INTEGER NOT NULL,"
        "ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT, error TEXT)"
    )
    raw.execute(
        "INSERT INTO task_runs (id, task_id, profile, step_key, status, scope_slice, "
        "memory_high, memory_max, memory_swap_max, tasks_max, oom_policy, control_group, "
        "started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-run", "task", "worker", "new-step", "running",
            "ops-workers.slice", "1536M", "2G", "256M", 384, "stop",
            "/user.slice/user-1000.slice/worker.scope", 1,
        ),
    )
    raw.commit()
    raw.close()

    with kb.connect(legacy) as migrated:
        columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(task_runs)")
        }
        assert "step_key" in columns
        assert {"scope_slice", "memory_high", "memory_max", "control_group"} <= columns
        id_column = next(
            row for row in migrated.execute("PRAGMA table_info(task_runs)")
            if row["name"] == "id"
        )
        assert id_column["type"].upper() == "INTEGER"
        row = migrated.execute(
            "SELECT step_key, scope_slice, memory_max, tasks_max, control_group "
            "FROM task_runs WHERE task_id='task'"
        ).fetchone()
        assert tuple(row) == (
            "new-step", "ops-workers.slice", "2G", 384,
            "/user.slice/user-1000.slice/worker.scope",
        )


def test_incomplete_or_inconsistent_scope_receipt_is_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: _target())
    with kb.connect(db_path) as conn:
        task_id = kb.create_task(conn, title="invalid receipt", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        receipt = kb._WorkerLaunchPid(
            1234,
            launch_mode="systemd-user-scope",
            scope_unit=kb._systemd_scope_unit_name(
                task_id, claimed.current_run_id, db_path=db_path
            ),
            verification_status="verified",
            manager_kind=kb._SYSTEMD_USER_MANAGER_KIND,
            manager_uid=1000,
            launch_acknowledged=True,
            scope_slice="ops-workers.slice",
            memory_high="3G",
            memory_max="2G",
            memory_swap_max="256M",
            tasks_max=384,
            oom_policy="stop",
            control_group=None,
        )
        with pytest.raises(RuntimeError, match="receipt"):
            kb._set_worker_pid(conn, task_id, receipt)
        assert conn.execute(
            "SELECT worker_pid FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0] is None


@pytest.mark.parametrize(
    "failure",
    [PermissionError("denied"), "not-a-pid"],
    ids=["permission-error", "malformed"],
)
def test_cgroup_pid_probe_unknown_fails_closed(
    tmp_path, monkeypatch, failure
):
    target = _target()
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    original_read_text = Path.read_text

    def failed_cgroup_read(path, *args, **kwargs):
        if path.name == "cgroup.procs":
            if isinstance(failure, BaseException):
                raise failure
            return failure
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failed_cgroup_read)
    assert kb._systemd_scope_process_ids("/worker.scope") is None

    db_path = tmp_path / "kanban.db"
    with kb.connect(db_path) as conn:
        task_id = kb.create_task(conn, title="unknown cgroup", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        kb._set_worker_pid(
            conn,
            task_id,
            kb._WorkerLaunchPid(
                1234,
                launch_mode="systemd-user-scope",
                scope_unit=kb._systemd_scope_unit_name(
                    task_id, claimed.current_run_id, db_path=db_path
                ),
                verification_status="verified",
                manager_kind=kb._SYSTEMD_USER_MANAGER_KIND,
                manager_uid=target.uid,
                launch_acknowledged=True,
                scope_slice="ops-workers.slice",
                memory_high="1536M",
                memory_max="2G",
                memory_swap_max="256M",
                tasks_max=384,
                oom_policy="stop",
                control_group="/worker.scope",
            ),
        )
        monkeypatch.setattr(kb, "_systemd_scope_state", lambda *args: "inactive")
        stop_attempts = []
        monkeypatch.setattr(
            kb,
            "_stop_systemd_scope",
            lambda *args: stop_attempts.append(args) or False,
        )

        assert not kb._stop_persisted_scope_for_release(
            conn, task_id, claimed.current_run_id,
        )
        assert not kb.reclaim_task(conn, task_id, reason="unknown cgroup")
        assert stop_attempts == []
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, claimed.current_run_id)

    assert task is not None and task.status == "running"
    assert task.current_run_id == claimed.current_run_id
    assert run is not None and run.ended_at is None
