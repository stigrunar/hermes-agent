"""Focused lifecycle and fallback coverage for Kanban worker scopes."""

from __future__ import annotations

import os
import hashlib
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def isolated_scope_home(tmp_path, monkeypatch):
    """Give scope lifecycle regressions an explicit isolated DB/home pair."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    return home


def _task(task_id="task-1", run_id=7):
    return SimpleNamespace(id=task_id, current_run_id=run_id)


def _scoped_receipt(task_id, run_id, pid=4321):
    return kb._WorkerLaunchPid(
        pid,
        launch_mode="systemd-user-scope",
        scope_unit=kb._systemd_scope_unit_name(task_id, run_id),
        verification_status="verified",
        manager_kind=kb._SYSTEMD_USER_MANAGER_KIND,
        manager_uid=os.getuid(),
        launch_acknowledged=True,
        scope_slice="hermes-kanban-workers.slice",
        memory_high="2G",
        memory_max="3G",
        memory_swap_max="512M",
        tasks_max=512,
        oom_policy="stop",
        control_group="/user.slice/user-1000.slice/hermes-kanban-worker.scope",
    )


def _mock_confirmed_scope_stop(monkeypatch):
    stopped = []
    state = {"absent": False}
    monkeypatch.setattr(
        kb,
        "_systemd_scope_state",
        lambda *args, **kwargs: "not-found" if state["absent"] else "active",
    )
    monkeypatch.setattr(kb, "_systemd_scope_process_ids", lambda path: ())

    def stop(unit, manager):
        stopped.append((unit, manager))
        state["absent"] = True
        return True

    monkeypatch.setattr(kb, "_stop_systemd_scope", stop)
    return stopped


def _tree_snapshot(root: Path) -> dict[str, tuple]:
    """Capture entries plus stable metadata/content for no-write assertions."""
    snapshot = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        info = path.lstat()
        digest = None
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshot[rel] = (
            info.st_mode,
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            digest,
        )
    return snapshot


def test_scope_unit_identity_is_canonical_board_and_run_bound(monkeypatch):
    monkeypatch.setattr(
        kb,
        "kanban_db_path",
        lambda board=None: Path("/srv/hermes") / (board or "default") / "kanban.db",
    )

    first = kb._systemd_scope_unit_name("task with text", 11, board="alpha")
    same = kb._systemd_scope_unit_name("task with text", 11, board="alpha")
    other_run = kb._systemd_scope_unit_name("task with text", 12, board="alpha")
    other_board = kb._systemd_scope_unit_name("task with text", 11, board="beta")

    assert first == same
    assert first != other_run
    assert first != other_board
    assert kb._SYSTEMD_WORKER_SCOPE_RE.fullmatch(first)
    assert "task with text" not in first


@pytest.mark.parametrize(
    ("platform", "cgroup", "run_id", "runner", "ready"),
    [
        ("darwin", "/user.slice/user-1000.slice/user@1000.service/hermes.service", 1, "/usr/bin/systemd-run", True),
        ("linux", "/system.slice/hermes.service", 1, "/usr/bin/systemd-run", True),
        ("linux", "/user.slice/user-1000.slice/user@1000.service", 1, "/usr/bin/systemd-run", True),
        ("linux", "/user.slice/user-1000.slice/user@1000.service/app.slice/app.scope", 1, "/usr/bin/systemd-run", True),
        ("linux", "/user.slice/user-1000.slice/user@1000.service/hermes.service", None, "/usr/bin/systemd-run", True),
        ("linux", "/user.slice/user-1000.slice/user@1000.service/hermes.service", 1, None, True),
        ("linux", "/user.slice/user-1000.slice/user@1000.service/hermes.service", 1, "/usr/bin/systemd-run", False),
    ],
)
def test_scope_capability_fail_open(
    monkeypatch, platform, cgroup, run_id, runner, ready
):
    monkeypatch.setattr(kb.sys, "platform", platform)
    target = kb._SystemdUserManagerTarget(
        os.getuid(), Path("/run/user") / str(os.getuid()), Path("/run/user") / str(os.getuid()) / "bus"
    )
    monkeypatch.setattr(
        kb,
        "_systemd_user_manager_target_for_cgroup",
        lambda _: target
        if cgroup.endswith("hermes.service") and "/system.slice/" not in cgroup
        else None,
    )
    monkeypatch.setattr(kb.shutil, "which", lambda name: runner if name == "systemd-run" else "/usr/bin/systemctl")

    original = ["/usr/bin/hermes", "chat", "-q", "work"]
    scoped, unit, manager = kb._systemd_scope_argv(
        original,
        _task(run_id=run_id),
        cgroup_path=cgroup,
        user_manager_ready=ready,
    )

    assert scoped is original
    assert unit is None
    assert manager is None


def test_scope_verification_failure_stops_exact_unit(monkeypatch):
    unit = "hermes-kanban-worker-0123456789abcdef0123456789abcdef.scope"
    target = kb._SystemdUserManagerTarget(
        os.getuid(), Path("/run/user") / str(os.getuid()), Path("/run/user") / str(os.getuid()) / "bus"
    )

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("systemd-run", timeout)
            return 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    proc = FakeProc()
    monkeypatch.setattr(kb, "_SYSTEMD_SCOPE_VERIFY_TIMEOUT", 0.01)
    monkeypatch.setattr(
        kb,
        "_systemd_scope_properties",
        lambda *args, **kwargs: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "ControlGroup": "/expected.scope",
        },
    )
    monkeypatch.setattr(kb, "_systemd_scope_process_ids", lambda path: (4242,))
    monkeypatch.setattr(kb, "_process_cgroup_path", lambda pid: "/expected.scope")
    monkeypatch.setattr(kb, "_process_command_argv", lambda pid: ("/wrong",))
    with pytest.raises(RuntimeError, match="could not verify systemd scope"):
        kb._verify_systemd_scope_worker_pid(proc, unit, target, ["/bin/sleep", "30"])

    stop_calls = []
    monkeypatch.setattr(kb.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(
        kb.subprocess,
        "run",
        lambda argv, **kwargs: stop_calls.append((argv, kwargs)) or SimpleNamespace(returncode=0, stdout=""),
    )
    kb._cleanup_systemd_scope_launch(proc, unit, target)
    assert stop_calls[0][0] == ["/usr/bin/systemctl", "--user", "stop", unit]
    assert getattr(proc, "terminated", False)


def test_scope_verification_returns_matching_worker_not_launcher(monkeypatch):
    unit = "hermes-kanban-worker-0123456789abcdef0123456789abcdef.scope"
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    proc = SimpleNamespace(pid=111, poll=lambda: None)
    monkeypatch.setattr(
        kb,
        "_systemd_scope_properties",
        lambda *args, **kwargs: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "ControlGroup": "/expected.scope",
        },
    )
    monkeypatch.setattr(kb, "_systemd_scope_process_ids", lambda path: (111, 222))
    monkeypatch.setattr(kb, "_process_cgroup_path", lambda pid: "/expected.scope")
    monkeypatch.setattr(
        kb,
        "_process_command_argv",
        lambda pid: ("/usr/bin/systemd-run",) if pid == 111 else ("/bin/sleep", "30"),
    )

    assert (
        kb._verify_systemd_scope_worker_pid(
            cast(subprocess.Popen, proc),
            unit,
            target,
            ["/bin/sleep", "30"],
        )
        == 222
    )


def test_process_command_matches_shebang_interpreter(monkeypatch):
    monkeypatch.setattr(
        kb,
        "_process_command_argv",
        lambda pid: ("/usr/bin/python3", "/usr/local/bin/hermes", "chat", "-q", "work"),
    )

    assert getattr(kb, "_process_command_matches")(
        222,
        ["/usr/local/bin/hermes", "chat", "-q", "work"],
    )


def test_default_spawn_receipt_reaches_spawned_event(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(), Path("/run/user") / str(os.getuid()), Path("/run/user") / str(os.getuid()) / "bus"
    )
    captured = {}

    class FakeProc:
        pid = 6789

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return FakeProc()

    def fake_scope_argv(cmd, task, **kwargs):
        unit = kb._systemd_scope_unit_name(task.id, task.current_run_id)
        captured["unit"] = unit
        return ["/usr/bin/systemd-run", "--user", "--scope", "--unit=" + unit, "--", *cmd], unit, target

    monkeypatch.setattr(kb, "_systemd_scope_argv", fake_scope_argv)
    monkeypatch.setattr(
        kb,
        "_verify_systemd_scope_worker_pid",
        lambda *args: kb._VerifiedWorkerPid(
            2468, control_group="/user.slice/user-1000.slice/hermes-kanban-worker.scope"
        ),
    )
    monkeypatch.setattr(kb.subprocess, "Popen", fake_popen)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="scoped", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=kb._default_spawn)
        event = next(event for event in kb.list_events(conn, task_id) if event.kind == "spawned")
        run = kb.latest_run(conn, task_id)

    assert result.spawned and result.spawned[0][0] == task_id
    assert event.payload == {
        "pid": 2468,
        "launch_mode": "systemd-user-scope",
        "scope_unit": captured["unit"],
        "verification_status": "verified",
        "manager_kind": "systemd-user",
        "manager_uid": os.getuid(),
        "launch_acknowledged": True,
        "scope_slice": "hermes-kanban-workers.slice",
        "memory_high": "2G",
        "memory_max": "3G",
        "memory_swap_max": "512M",
        "tasks_max": 512,
        "oom_policy": "stop",
        "control_group": "/user.slice/user-1000.slice/hermes-kanban-worker.scope",
    }
    assert isinstance(kb._WorkerLaunchPid(6789), int)
    assert captured["cmd"][0] == "/usr/bin/systemd-run"
    assert run is not None
    assert run.worker_pid == 2468
    assert run.launch_mode == "systemd-user-scope"
    assert run.scope_unit == captured["unit"]
    assert run.manager_kind == kb._SYSTEMD_USER_MANAGER_KIND
    assert run.manager_uid == os.getuid()
    assert run.launch_acknowledged is True
    assert run.verification_status == "verified"
    assert run.scope_slice == "hermes-kanban-workers.slice"
    assert run.memory_high == "2G"
    assert run.memory_max == "3G"
    assert run.memory_swap_max == "512M"
    assert run.tasks_max == 512
    assert run.oom_policy == "stop"
    assert run.control_group == "/user.slice/user-1000.slice/hermes-kanban-worker.scope"


def test_native_scope_fences_fast_terminal_before_popen(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    observed = {}
    active_conn = None

    def fake_scope_argv(cmd, task, **kwargs):
        unit = kb._systemd_scope_unit_name(task.id, task.current_run_id)
        return ["/usr/bin/systemd-run", "--", *cmd], unit, target

    class FakeProc:
        pid = 6789

    def fake_popen(cmd, **kwargs):
        assert active_conn is not None
        task = active_conn.execute(
            "SELECT id, current_run_id FROM tasks WHERE status='running'"
        ).fetchone()
        assert task is not None
        observed["mode"] = kb._persisted_worker_scope(
            active_conn, task["id"], task["current_run_id"]
        ).mode.value
        observed["completed"] = kb.complete_task(
            active_conn,
            task["id"],
            result="too fast",
            expected_run_id=task["current_run_id"],
        )
        return FakeProc()

    monkeypatch.setattr(kb, "_systemd_scope_argv", fake_scope_argv)
    monkeypatch.setattr(
        kb, "_systemd_user_manager_target_for_uid", lambda uid: target,
    )
    _mock_confirmed_scope_stop(monkeypatch)
    monkeypatch.setattr(
        kb,
        "_verify_systemd_scope_worker_pid",
        lambda *args: kb._VerifiedWorkerPid(2468, control_group="/verified.scope"),
    )
    monkeypatch.setattr(kb.subprocess, "Popen", fake_popen)

    with kb.connect() as conn:
        active_conn = conn
        task_id = kb.create_task(conn, title="fast terminal", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=kb._default_spawn)
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert run is not None and run.reap_state == "terminal_requested"
        reconciled = kb.reconcile_worker_scope_terminals(conn)
        final = kb.get_task(conn, task_id)

    assert observed == {"mode": "launching", "completed": True}
    assert result.spawned and result.spawned[0][0] == task_id
    assert task is not None and task.status == "running"
    assert task.current_run_id == run.id
    assert run is not None and run.verification_status == "verified"
    assert run.worker_pid == 2468
    assert reconciled == [task_id]
    assert final is not None and final.status == "done"


def test_launching_terminal_request_is_idempotent_and_conflicts_fail(
    isolated_scope_home, monkeypatch,
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    config = kb._WorkerScopeConfig(
        enabled=True,
        required=False,
        slice="hermes-kanban-workers.slice",
        memory_high="2G",
        memory_max="3G",
        memory_swap_max="512M",
        tasks_max=512,
        oom_policy="stop",
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="launching terminal", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)
        unit = kb._systemd_scope_unit_name(task_id, run_id)
        kb._set_worker_launching(
            conn,
            task_id,
            scope_unit=unit,
            target=target,
            scope_config=config,
        )

        assert kb.complete_task(
            conn, task_id, result="done", expected_run_id=run_id,
        )
        assert kb.complete_task(
            conn, task_id, result="done", expected_run_id=run_id,
        )
        assert not kb.block_task(
            conn, task_id, reason="conflicting", expected_run_id=run_id,
        )

        run = kb.get_run(conn, run_id)
        assert run is not None
        assert run.terminal_action == "complete"
        assert run.terminal_payload == {
            "result": "done",
            "summary": None,
            "metadata": None,
            "created_cards": [],
            "fire_lifecycle_hook": True,
        }
        assert run.reap_state == "terminal_requested"
        assert len([
            event for event in kb.list_events(conn, task_id)
            if event.kind == "terminal_requested"
        ]) == 1


@pytest.mark.parametrize("terminal", ["complete", "block"])
def test_scoped_terminal_waits_for_exact_scope_reap(
    kanban_home, all_assignees_spawnable, monkeypatch, terminal
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    states = iter(["active", "active", "not-found"])
    monkeypatch.setattr(kb, "_systemd_scope_state", lambda *args, **kwargs: next(states))
    monkeypatch.setattr(kb, "_systemd_scope_process_ids", lambda path: (5432,))
    stopped = []
    monkeypatch.setattr(
        kb,
        "_stop_systemd_scope",
        lambda unit, manager: stopped.append((unit, manager)) or True,
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=terminal, assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        unit = kb._systemd_scope_unit_name(task_id, claimed.current_run_id)
        receipt = _scoped_receipt(task_id, claimed.current_run_id)
        kb._set_worker_pid(conn, task_id, receipt)

        if terminal == "complete":
            assert kb.complete_task(
                conn, task_id, result="done", expected_run_id=claimed.current_run_id,
            )
        else:
            assert kb.block_task(
                conn, task_id, reason="blocked", expected_run_id=claimed.current_run_id,
            )

        pending = kb.get_task(conn, task_id)
        run = kb.get_run(conn, claimed.current_run_id)
        assert pending is not None and pending.status == "running"
        assert run is not None and run.reap_state == "terminal_requested"
        assert kb.reconcile_worker_scope_terminals(conn) == [task_id]
        final = kb.get_task(conn, task_id)
        assert final is not None
        assert final.status == ("done" if terminal == "complete" else "blocked")

    assert stopped == [(unit, target)]


@pytest.mark.parametrize("terminal", ["complete", "block"])
def test_conflicting_scoped_terminal_request_does_not_finalize_before_reap(
    kanban_home, all_assignees_spawnable, monkeypatch, terminal
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    stopped = _mock_confirmed_scope_stop(monkeypatch)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=terminal, assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        unit = kb._systemd_scope_unit_name(task_id, run_id)
        kb._set_worker_pid(
            conn,
            task_id,
            _scoped_receipt(task_id, run_id),
        )

        if terminal == "complete":
            assert kb.complete_task(
                conn, task_id, result="first", expected_run_id=run_id,
            )
            assert not kb.complete_task(
                conn, task_id, result="second", expected_run_id=run_id,
            )
        else:
            assert kb.block_task(
                conn, task_id, reason="first", expected_run_id=run_id,
            )
            assert not kb.block_task(
                conn, task_id, reason="second", expected_run_id=run_id,
            )

        pending = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert pending is not None and pending.status == "running"
        assert pending.current_run_id == run_id
        assert run is not None and run.ended_at is None
        assert run.reap_state == "terminal_requested"
        assert kb.reconcile_worker_scope_terminals(conn) == [task_id]

        final = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert final is not None
        assert final.status == ("done" if terminal == "complete" else "blocked")
        assert run is not None and run.ended_at is not None
        assert run.reap_state == "reaped"
        assert run.reap_completed_at is not None

    assert stopped == [(unit, target)]


def test_scoped_changes_requested_waits_for_exact_scope_reap(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    stopped = _mock_confirmed_scope_stop(monkeypatch)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="reviewed", assignee="builder")
        implementation = kb.claim_task(conn, task_id, claimer="builder:1")
        assert implementation is not None and implementation.current_run_id is not None
        assert kb.request_review(
            conn,
            task_id,
            reviewer="reviewer",
            summary="ready for review",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id, claimer="reviewer:1")
        assert review is not None and review.current_run_id is not None
        run_id = review.current_run_id
        unit = kb._systemd_scope_unit_name(task_id, run_id)
        kb._set_worker_pid(
            conn,
            task_id,
            _scoped_receipt(task_id, run_id),
        )

        assert kb.request_changes(
            conn,
            task_id,
            reason="Add the missing regression.",
            expected_run_id=run_id,
        ) == (True, "builder")

        pending = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert pending is not None
        assert pending.status == "running"
        assert pending.current_run_id == run_id
        assert run is not None
        assert run.ended_at is None
        assert run.terminal_action == "changes_requested"
        assert run.terminal_payload == {"reason": "Add the missing regression."}
        assert run.reap_state == "terminal_requested"

        assert kb.reconcile_worker_scope_terminals(conn) == [task_id]
        final = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert final is not None
        assert final.status == "ready"
        assert final.assignee == "builder"
        assert final.current_run_id is None
        assert run is not None and run.outcome == "changes_requested"

    assert stopped == [(unit, target)]


def test_scoped_request_review_waits_for_exact_scope_reap_and_rejects_conflict(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    stopped = _mock_confirmed_scope_stop(monkeypatch)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="review handoff", assignee="builder")
        implementation = kb.claim_task(conn, task_id, claimer="builder:1")
        assert implementation is not None
        assert kb.request_review(
            conn, task_id, reviewer="reviewer", summary="initial",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id, claimer="reviewer:1")
        assert review is not None and review.current_run_id is not None
        run_id = review.current_run_id
        unit = kb._systemd_scope_unit_name(task_id, run_id)
        kb._set_worker_pid(conn, task_id, _scoped_receipt(task_id, run_id))

        assert kb.request_review(
            conn,
            task_id,
            reviewer="reviewer",
            summary="first handoff",
            expected_run_id=run_id,
        )
        assert not kb.request_review(
            conn,
            task_id,
            reviewer="reviewer",
            summary="conflicting handoff",
            expected_run_id=run_id,
        )
        pending = kb.get_run(conn, run_id)
        assert pending is not None
        assert pending.reap_state == "terminal_requested"
        assert pending.terminal_action == "review_requested"
        assert kb.reconcile_worker_scope_terminals(conn) == [task_id]
        final = kb.get_task(conn, task_id)
        assert final is not None and final.status == "review"
        closed = kb.get_run(conn, run_id)
        assert closed is not None and closed.outcome == "review_requested"

    assert stopped == [(unit, target)]


def test_dead_scope_leader_with_active_descendant_scope_is_not_reaped(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    monkeypatch.setattr(kb, "_systemd_scope_state", lambda *args, **kwargs: "active")
    monkeypatch.setattr(kb, "_systemd_scope_process_ids", lambda path: (9876,))
    monkeypatch.setattr(kb, "_stop_systemd_scope", lambda *args, **kwargs: True)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="descendant", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        kb._set_worker_pid(
            conn, task_id, _scoped_receipt(task_id, claimed.current_run_id)
        )
        assert kb.detect_crashed_workers(conn) == []
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status == "running"


def test_dry_run_does_not_reconcile_pending_scoped_terminal(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    monkeypatch.setattr(kb, "_systemd_scope_state", lambda *args, **kwargs: "active")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    stop_calls = []
    monkeypatch.setattr(
        kb,
        "_stop_systemd_scope",
        lambda *args, **kwargs: stop_calls.append(args) or pytest.fail("dry-run stopped a scope"),
    )
    monkeypatch.setattr(
        kb,
        "reconcile_worker_scope_terminals",
        lambda conn: pytest.fail("dry-run reconciled a terminal intent"),
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="dry-run", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        kb._set_worker_pid(
            conn, task_id, _scoped_receipt(task_id, claimed.current_run_id)
        )
        assert kb.complete_task(
            conn, task_id, result="done", expected_run_id=claimed.current_run_id,
        )
        before = kb.get_run(conn, claimed.current_run_id)
        assert before is not None and before.reap_state == "terminal_requested"
        kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: None, dry_run=True)
        after = kb.get_run(conn, claimed.current_run_id)
        assert after is not None and after.reap_state == "terminal_requested"
        assert kb.get_task(conn, task_id).status == "running"
    assert stop_calls == []


def test_dry_run_orphan_reconciliation_preserves_scoped_identity(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """Dry-run repairs legacy orphans but never stops a persisted scope."""
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(
        kb, "_systemd_user_manager_target_for_uid", lambda uid: target,
    )
    stop_calls = []
    monkeypatch.setattr(
        kb,
        "_stop_systemd_scope",
        lambda *args, **kwargs: stop_calls.append(args) or True,
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="scoped orphan", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        kb._set_worker_pid(conn, task_id, _scoped_receipt(task_id, run_id))
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL WHERE id = ?",
                (task_id,),
            )

        result = kb.dispatch_once(
            conn, spawn_fn=lambda *args, **kwargs: None, dry_run=True,
        )

        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert result.reconciled_orphans == []
        assert task is not None and task.status == "running"
        assert task.current_run_id == run_id
        assert run is not None and run.ended_at is None

    assert stop_calls == []


def test_dry_run_recomputes_ready_before_enumeration_and_matches_live_spawn(
    isolated_scope_home, all_assignees_spawnable, monkeypatch
):
    """Dry-run promotion is visible without claiming or resolving a workspace."""
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "unknown")
    spawn_calls = []

    def recording_spawn(task, workspace, **kwargs):
        spawn_calls.append((task.id, workspace))
        return 4242

    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="planner")
        child_id = kb.create_task(
            conn,
            title="child",
            assignee="worker",
            parents=[parent_id],
        )
        assert kb.get_task(conn, child_id).status == "todo"
        assert kb.complete_task(conn, parent_id, result="parent done")
        assert kb.get_task(conn, parent_id).status == "done"
        # Keep the promotion under test in the dispatcher rather than relying
        # on the parent-completion writer's eager descendant update.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child_id,))
        assert kb.get_task(conn, child_id).status == "todo"

        dry_result = kb.dispatch_once(
            conn,
            spawn_fn=recording_spawn,
            dry_run=True,
        )

        child = kb.get_task(conn, child_id)
        assert dry_result.promoted == 1
        assert dry_result.spawned == [(child_id, "worker", "")]
        assert spawn_calls == []
        assert child is not None
        assert child.status == "ready"
        assert child.current_run_id is None
        assert child.worker_pid is None
        assert child.claim_lock is None
        assert child.claim_expires is None
        assert child.workspace_path is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (child_id,)
        ).fetchone()[0] == 0

        live_result = kb.dispatch_once(conn, spawn_fn=recording_spawn)
        live_child = kb.get_task(conn, child_id)

        assert [task_id for task_id, _assignee, _workspace in live_result.spawned] == [
            child_id
        ]
        assert spawn_calls and spawn_calls[0][0] == child_id
        assert live_child is not None
        assert live_child.status == "running"
        assert live_child.current_run_id is not None
        assert live_child.worker_pid == 4242
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (child_id,)
        ).fetchone()[0] == 1


def test_scoped_iteration_exhaustion_waits_for_exact_scope_reap(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    stopped = _mock_confirmed_scope_stop(monkeypatch)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="exhausted", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        unit = kb._systemd_scope_unit_name(task_id, run_id)
        kb._set_worker_pid(
            conn,
            task_id,
            _scoped_receipt(task_id, run_id, pid=7654),
        )

        assert kb._record_iteration_exhaustion(
            conn, task_id, budget_used=12, budget_max=12,
        ) == run_id

        pending = conn.execute(
            "SELECT status, claim_lock, current_run_id FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        run = kb.get_run(conn, run_id)
        assert pending is not None
        assert pending["status"] == "running"
        assert pending["claim_lock"] is not None
        assert pending["current_run_id"] == run_id
        assert run is not None
        assert run.ended_at is None
        assert run.terminal_action == "iteration_exhausted"
        assert run.reap_state == "terminal_requested"
        assert kb._record_iteration_exhaustion(
            conn, task_id, budget_used=12, budget_max=12,
        ) == run_id
        assert kb._record_iteration_exhaustion(
            conn, task_id, budget_used=11, budget_max=12,
        ) is None
        terminal_requests = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "terminal_requested"
        ]
        assert len(terminal_requests) == 1
        assert terminal_requests[0].payload["action"] == "iteration_exhausted"

        assert kb.reconcile_worker_scope_terminals(conn) == [task_id]

        final = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert final is not None
        assert final.status == "blocked"
        assert final.block_kind == "iteration_exhausted"
        assert final.claim_lock is None
        assert final.current_run_id is None
        assert final.consecutive_failures == 1
        assert run is not None
        assert run.ended_at is not None
        assert run.outcome == "iteration_exhausted"
        assert run.metadata == {
            "budget_used": 12,
            "budget_max": 12,
            "checkpoint_required": True,
            "workspace_path": "",
            "retryable": False,
            "resume_policy": "never",
        }
        event = next(
            event for event in kb.list_events(conn, task_id)
            if event.kind == "iteration_exhausted"
        )
        assert event.run_id == run_id
        assert event.payload["terminal_run_id"] == run_id
        assert event.payload["retryable"] is False

    assert stopped == [(unit, target)]


def test_untracked_iteration_exhaustion_remains_immediate(
    kanban_home, all_assignees_spawnable
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="direct exhaustion", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None

        assert kb._record_iteration_exhaustion(
            conn, task_id, budget_used=4, budget_max=4,
        ) == claimed.current_run_id

        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, claimed.current_run_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "iteration_exhausted"
        assert task.claim_lock is None
        assert task.current_run_id is None
        assert run is not None
        assert run.ended_at is not None
        assert run.outcome == "iteration_exhausted"
        assert run.terminal_action is None
        assert run.reap_state is None


@pytest.mark.parametrize("terminal", ["timeout", "crash", "cancel"])
def test_dispatcher_terminal_paths_stop_persisted_scope(
    kanban_home, all_assignees_spawnable, monkeypatch, terminal
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    stopped = _mock_confirmed_scope_stop(monkeypatch)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=terminal,
            assignee="worker",
            max_runtime_seconds=1 if terminal == "timeout" else None,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        unit = kb._systemd_scope_unit_name(task_id, claimed.current_run_id)
        kb._set_worker_pid(
            conn,
            task_id,
            _scoped_receipt(task_id, claimed.current_run_id, pid=5432),
        )
        if terminal == "timeout":
            conn.execute(
                "UPDATE task_runs SET started_at=? WHERE id=?",
                (int(kb.time.time()) - 2, claimed.current_run_id),
            )
            conn.commit()
            assert kb.enforce_max_runtime(conn) == [task_id]
        elif terminal == "crash":
            monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
            assert kb.detect_crashed_workers(conn) == [task_id]
        else:
            assert kb.reclaim_task(conn, task_id, reason="cancel")

    assert stopped == [(unit, target)]


@pytest.mark.parametrize(
    "operation", ["release", "reclaim", "timeout", "stale", "review-reclaim"],
)
def test_scoped_recovery_never_signals_worker_pid(
    isolated_scope_home, monkeypatch, operation,
):
    """A confirmed scope reap owns termination; the worker PID is never reused."""
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    _mock_confirmed_scope_stop(monkeypatch)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    signals = []

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=operation,
            assignee="worker",
            max_runtime_seconds=1 if operation == "timeout" else None,
        )
        if operation == "review-reclaim":
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
            claimed = kb.claim_review_task(conn, task_id)
        else:
            claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)
        kb._set_worker_pid(conn, task_id, _scoped_receipt(task_id, run_id, pid=5432))

        if operation == "release":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET claim_expires=? WHERE id=?",
                    (int(kb.time.time()) - 1, task_id),
                )
            assert kb.release_stale_claims(
                conn, signal_fn=lambda *args: signals.append(args),
            ) == 1
        elif operation in {"reclaim", "review-reclaim"}:
            assert kb.reclaim_task(
                conn, task_id, reason="test",
                signal_fn=lambda *args: signals.append(args),
            )
        elif operation == "timeout":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET started_at=? WHERE id=?",
                    (int(kb.time.time()) - 2, run_id),
                )
            assert kb.enforce_max_runtime(
                conn, signal_fn=lambda *args: signals.append(args),
            ) == [task_id]
        else:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET started_at=? WHERE id=?",
                    (int(kb.time.time()) - 2, run_id),
                )
            assert kb.detect_stale_running(
                conn,
                stale_timeout_seconds=1,
                signal_fn=lambda *args: signals.append(args),
            ) == [task_id]

    assert signals == []


@pytest.mark.parametrize(
    "operation", ["release", "reclaim", "timeout", "stale"],
)
def test_direct_recovery_still_signals_worker_pid(
    isolated_scope_home, monkeypatch, operation,
):
    """Direct/untracked launches retain the legacy PID termination seam."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    signals = []
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=operation,
            assignee="worker",
            max_runtime_seconds=1 if operation == "timeout" else None,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)
        kb._set_worker_pid(conn, task_id, 5432)
        signal_fn = lambda *args: signals.append(args)

        if operation == "release":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET claim_expires=? WHERE id=?",
                    (int(kb.time.time()) - 1, task_id),
                )
            assert kb.release_stale_claims(conn, signal_fn=signal_fn) == 1
        elif operation == "reclaim":
            assert kb.reclaim_task(
                conn, task_id, reason="test", signal_fn=signal_fn,
            )
        elif operation == "timeout":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET started_at=? WHERE id=?",
                    (int(kb.time.time()) - 2, run_id),
                )
            assert kb.enforce_max_runtime(conn, signal_fn=signal_fn) == [task_id]
        else:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET started_at=? WHERE id=?",
                    (int(kb.time.time()) - 2, run_id),
                )
            assert kb.detect_stale_running(
                conn, stale_timeout_seconds=1, signal_fn=signal_fn,
            ) == [task_id]

    assert signals and signals[0][0] == 5432


@pytest.mark.parametrize("operation", ["stale", "orphan", "crash"])
def test_scoped_recovery_ignores_recycled_host_pid_when_scope_absent(
    isolated_scope_home, monkeypatch, operation,
):
    """A live PID outside the exact scope cannot block scoped recovery."""
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    monkeypatch.setattr(kb, "_systemd_scope_state", lambda *args, **kwargs: "not-found")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=operation, assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)
        kb._set_worker_pid(conn, task_id, _scoped_receipt(task_id, run_id, pid=5432))
        if operation == "stale":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET claim_expires=? WHERE id=?",
                    (int(kb.time.time()) - 1, task_id),
                )
            assert kb.release_stale_claims(conn) == 1
        elif operation == "orphan":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET claim_lock=NULL, claim_expires=NULL WHERE id=?",
                    (task_id,),
                )
            assert kb.reconcile_orphaned_running(conn) == [task_id]
        else:
            monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
            monkeypatch.setattr(
                kb, "_classify_worker_exit", lambda pid: ("nonzero_exit", 1),
            )
            assert kb.detect_crashed_workers(conn) == [task_id]

        task = kb.get_task(conn, task_id)
        assert task is not None and task.status in {"ready", "review"}


@pytest.mark.parametrize("operation", ["stale", "orphan", "crash"])
@pytest.mark.parametrize("scope_state", ["active", "unknown"])
def test_scoped_recovery_fences_active_or_unknown_scope_before_pid(
    isolated_scope_home, monkeypatch, operation, scope_state,
):
    """Active descendants and unknown manager state defeat host-PID claims."""
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    monkeypatch.setattr(kb, "_systemd_scope_state", lambda *args, **kwargs: scope_state)
    cgroup_probes = []
    monkeypatch.setattr(
        kb,
        "_systemd_scope_process_ids",
        lambda control_group: cgroup_probes.append(control_group) or (9876,),
    )
    pid_probes = []
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid_probes.append(pid) or True)
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    monkeypatch.setattr(
        kb,
        "_classify_worker_exit",
        lambda pid: pytest.fail("unknown/active scope reached crash accounting"),
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=f"{operation}-{scope_state}", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)
        kb._set_worker_pid(conn, task_id, _scoped_receipt(task_id, run_id, pid=5432))
        if operation == "stale":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET claim_expires=? WHERE id=?",
                    (int(kb.time.time()) - 1, task_id),
                )
            assert kb.release_stale_claims(conn) == 0
        elif operation == "orphan":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET claim_lock=NULL, claim_expires=NULL WHERE id=?",
                    (task_id,),
                )
            assert kb.reconcile_orphaned_running(conn) == []
        else:
            assert kb.detect_crashed_workers(conn) == []

        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert task is not None and task.status == "running"
        assert task.current_run_id == run_id and task.worker_pid == 5432
        assert run is not None and run.ended_at is None

    assert pid_probes == []
    if scope_state == "active":
        assert cgroup_probes == [_scoped_receipt(task_id, run_id).control_group]
    else:
        assert cgroup_probes == []


@pytest.mark.parametrize(
    "operation", ["stale", "reclaim", "timeout", "orphan", "crash", "terminal", "release"],
)
def test_active_unknown_cgroup_fails_closed_for_all_scope_recovery_paths(
    isolated_scope_home, monkeypatch, operation,
):
    """An unreadable active cgroup never permits scope cleanup or release."""
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    monkeypatch.setattr(kb, "_systemd_scope_state", lambda *args, **kwargs: "active")
    cgroup_probes = []
    monkeypatch.setattr(
        kb,
        "_systemd_scope_process_ids",
        lambda control_group: cgroup_probes.append(control_group) or None,
    )
    stop_calls = []
    monkeypatch.setattr(
        kb,
        "_stop_systemd_scope",
        lambda *args, **kwargs: stop_calls.append((args, kwargs))
        or pytest.fail("unknown cgroup attempted scope stop"),
    )
    pid_probes = []
    monkeypatch.setattr(
        kb,
        "_pid_alive",
        lambda pid: pid_probes.append(pid)
        or pytest.fail("unknown cgroup probed host PID"),
    )
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    monkeypatch.setattr(
        kb,
        "_classify_worker_exit",
        lambda pid: pytest.fail("unknown cgroup reached crash accounting"),
    )

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"unknown-cgroup-{operation}",
            assignee="worker",
            max_runtime_seconds=1 if operation == "timeout" else None,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)
        kb._set_worker_pid(conn, task_id, _scoped_receipt(task_id, run_id, pid=5432))
        receipt = kb._persisted_worker_scope(conn, task_id, run_id)
        assert kb._worker_scope_runtime_status(receipt) == "unknown"

        if operation == "stale":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET claim_expires=? WHERE id=?",
                    (int(kb.time.time()) - 1, task_id),
                )
            assert kb.release_stale_claims(conn) == 0
        elif operation == "reclaim":
            assert not kb.reclaim_task(conn, task_id, reason="unknown cgroup")
        elif operation == "timeout":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET started_at=? WHERE id=?",
                    (int(kb.time.time()) - 2, run_id),
                )
            assert kb.enforce_max_runtime(conn) == []
        elif operation == "orphan":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET claim_lock=NULL, claim_expires=NULL WHERE id=?",
                    (task_id,),
                )
            assert kb.reconcile_orphaned_running(conn) == []
        elif operation == "crash":
            assert kb.detect_crashed_workers(conn) == []
        elif operation == "terminal":
            assert kb.complete_task(
                conn, task_id, result="done", expected_run_id=run_id,
            )
            assert kb.reconcile_worker_scope_terminals(conn) == []
        else:
            release = kb._scope_release_result(conn, task_id, run_id)
            assert not release.can_release
            assert release.cleanup == "unknown"

        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert task is not None and task.status == "running"
        assert task.current_run_id == run_id and task.worker_pid == 5432
        assert run is not None and run.ended_at is None

    assert cgroup_probes
    assert stop_calls == []
    assert pid_probes == []


def test_scope_release_uses_one_validated_receipt_snapshot(
    isolated_scope_home, monkeypatch,
):
    """Identity and cgroup cleanup are read from one task_run SELECT."""
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    monkeypatch.setattr(kb, "_systemd_scope_state", lambda *args, **kwargs: "inactive")
    observed_cgroups = []
    monkeypatch.setattr(
        kb,
        "_systemd_scope_process_ids",
        lambda control_group: observed_cgroups.append(control_group) or (),
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="snapshot", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)
        receipt = _scoped_receipt(task_id, run_id)
        kb._set_worker_pid(conn, task_id, receipt)
        statements = []
        conn.set_trace_callback(statements.append)
        release = kb._scope_release_result(conn, task_id, run_id)
        conn.set_trace_callback(None)

    receipt_selects = [
        sql for sql in statements
        if "FROM task_runs WHERE id=" in sql and "control_group" in sql
    ]
    assert len(receipt_selects) == 1
    assert observed_cgroups == [receipt.control_group]
    assert release.can_release
    assert not release.pid_signal_allowed


def test_missing_current_run_receipt_fails_closed_without_signal(
    isolated_scope_home, monkeypatch,
):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    signals = []
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="missing receipt", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        missing_run_id = int(claimed.current_run_id) + 100_000
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET current_run_id=?, worker_pid=? WHERE id=?",
                (missing_run_id, 5432, task_id),
            )

        assert not kb.reclaim_task(
            conn, task_id, reason="test", signal_fn=lambda *args: signals.append(args),
        )
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "running"
        assert task.current_run_id == missing_run_id
        assert task.worker_pid == 5432
    assert signals == []


@pytest.mark.parametrize(
    "receipt_update",
    [
        "scope_slice='stray.slice', control_group='/stray.scope'",
        "launch_mode='direct', verification_status='not-applicable', "
        "manager_kind='systemd-user'",
    ],
)
def test_partial_or_mixed_receipt_fails_closed_without_signal(
    isolated_scope_home, monkeypatch, receipt_update,
):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    signals = []
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="partial receipt", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)
        with kb.write_txn(conn):
            conn.execute(
                f"UPDATE task_runs SET worker_pid=5432, {receipt_update} WHERE id=?",
                (run_id,),
            )
            conn.execute("UPDATE tasks SET worker_pid=5432 WHERE id=?", (task_id,))

        assert not kb.reclaim_task(
            conn, task_id, reason="test", signal_fn=lambda *args: signals.append(args),
        )
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "running"
        assert task.current_run_id == run_id
        assert task.worker_pid == 5432
    assert signals == []


def test_canonical_legacy_all_null_receipt_keeps_pid_signaling(
    isolated_scope_home, monkeypatch,
):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    signals = []
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="legacy receipt", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid=5432 WHERE id=?",
                (task_id,),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid=5432 WHERE id=?",
                (int(claimed.current_run_id),),
            )

        assert kb.reclaim_task(
            conn, task_id, reason="test", signal_fn=lambda *args: signals.append(args),
        )
    assert signals and signals[0][0] == 5432


def test_confirmed_scoped_reap_releases_without_pid_signal_permission(
    isolated_scope_home, monkeypatch,
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    monkeypatch.setattr(kb, "_systemd_scope_state", lambda *args, **kwargs: "not-found")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="confirmed reap", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)
        kb._set_worker_pid(conn, task_id, _scoped_receipt(task_id, run_id))
        release = kb._scope_release_result(conn, task_id, run_id)

    assert release.can_release
    assert not release.pid_signal_allowed


def test_dashboard_refused_running_to_ready_does_not_cleanup_or_mutate_identity(
    isolated_scope_home, monkeypatch,
):
    from plugins.kanban.dashboard import plugin_api as dashboard_api

    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent_id)
        task_id = kb.create_task(
            conn, title="child", assignee="worker", parents=[parent_id],
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        kb._set_worker_pid(conn, task_id, 5432)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (parent_id,))
        before = kb.get_task(conn, task_id)
        monkeypatch.setattr(
            kb,
            "_scope_release_result",
            lambda *args, **kwargs: pytest.fail("refused move attempted cleanup"),
        )

        assert not dashboard_api._set_status_direct(conn, task_id, "ready")
        after = kb.get_task(conn, task_id)

    assert before is not None and after is not None
    assert (after.status, after.current_run_id, after.worker_pid, after.claim_lock) == (
        before.status,
        before.current_run_id,
        before.worker_pid,
        before.claim_lock,
    )


def test_dashboard_running_noop_does_not_cleanup_or_mutate_identity(
    isolated_scope_home, monkeypatch,
):
    from plugins.kanban.dashboard import plugin_api as dashboard_api

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="noop", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        kb._set_worker_pid(conn, task_id, 5432)
        before = kb.get_task(conn, task_id)
        monkeypatch.setattr(
            kb,
            "_scope_release_result",
            lambda *args, **kwargs: pytest.fail("running no-op attempted cleanup"),
        )

        assert dashboard_api._set_status_direct(conn, task_id, "running")
        after = kb.get_task(conn, task_id)

    assert before is not None and after is not None
    assert (after.status, after.current_run_id, after.worker_pid, after.claim_lock) == (
        before.status,
        before.current_run_id,
        before.worker_pid,
        before.claim_lock,
    )


def test_dashboard_actual_running_to_ready_direct_keeps_pid_signal(
    isolated_scope_home, monkeypatch,
):
    from plugins.kanban.dashboard import plugin_api as dashboard_api

    terminations = []
    monkeypatch.setattr(
        kb,
        "_terminate_reclaimed_worker",
        lambda pid, lock: terminations.append((pid, lock)) or {"terminated": True},
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="direct move", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        kb._set_worker_pid(conn, task_id, 5432)

        assert dashboard_api._set_status_direct(conn, task_id, "ready")
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, int(claimed.current_run_id))

    assert task is not None and task.status == "ready"
    assert task.current_run_id is None and task.worker_pid is None
    assert run is not None and run.outcome == "reclaimed"
    assert terminations and terminations[0][0] == 5432


def test_dashboard_actual_running_to_ready_scoped_does_not_signal_pid(
    isolated_scope_home, monkeypatch,
):
    from plugins.kanban.dashboard import plugin_api as dashboard_api

    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    stopped = _mock_confirmed_scope_stop(monkeypatch)
    monkeypatch.setattr(
        kb,
        "_terminate_reclaimed_worker",
        lambda *args, **kwargs: pytest.fail("confirmed scope reap signaled PID"),
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="scoped move", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)
        kb._set_worker_pid(conn, task_id, _scoped_receipt(task_id, run_id, pid=5432))

        assert dashboard_api._set_status_direct(conn, task_id, "ready")
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)

    assert stopped
    assert task is not None and task.status == "ready"
    assert task.current_run_id is None and task.worker_pid is None
    assert run is not None and run.outcome == "reclaimed"


def test_scope_is_stopped_when_launch_receipt_cannot_be_persisted(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    hook_calls = []
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    stopped = _mock_confirmed_scope_stop(monkeypatch)
    monkeypatch.setattr(
        kb,
        "_fire_worker_spawned_hook",
        lambda *args, **kwargs: hook_calls.append((args, kwargs)),
    )

    def spawn(task, workspace):
        return _scoped_receipt(task.id, task.current_run_id, pid=6543)

    monkeypatch.setattr(
        kb,
        "_set_worker_pid",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("persist failed")),
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="persist failure", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=spawn)
        run = kb.latest_run(conn, task_id)

    assert result.spawned == []
    assert run is not None and run.outcome == "spawn_failed"
    assert len(stopped) == 1
    assert stopped[0][0].startswith(kb._SYSTEMD_WORKER_SCOPE_PREFIX)
    assert stopped[0][1] is target
    assert hook_calls == []


def test_receipt_persist_and_scope_stop_failure_keeps_run_fenced(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    stops = []
    monkeypatch.setattr(
        kb, "_systemd_user_manager_target_for_uid", lambda uid: target,
    )
    monkeypatch.setattr(kb, "_systemd_scope_state", lambda *args: "active")
    monkeypatch.setattr(
        kb,
        "_stop_systemd_scope",
        lambda unit, manager: stops.append((unit, manager)) or False,
    )

    def spawn(task, workspace):
        return _scoped_receipt(task.id, task.current_run_id, pid=6543)

    monkeypatch.setattr(
        kb,
        "_set_worker_pid",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("persist failed")),
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="uncertain cleanup", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=spawn)
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)

    assert result.spawned == []
    assert task is not None and task.status == "running"
    assert task.claim_lock is not None and task.current_run_id == run.id
    assert run is not None and run.ended_at is None and run.outcome is None
    assert run.reap_state == "launch_cleanup_pending"
    assert run.verification_status == "launching"
    assert run.scope_unit == stops[0][0]
    assert len(stops) == 1


def test_launching_terminal_promotion_failure_stays_fenced_until_absent(
    isolated_scope_home, monkeypatch,
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    state = {"value": "unknown"}
    monkeypatch.setattr(kb, "_systemd_scope_state", lambda *args: state["value"])
    monkeypatch.setattr(
        kb,
        "_stop_systemd_scope",
        lambda *args: pytest.fail("unknown cleanup must not stop an unconfirmed scope"),
    )
    config = kb._WorkerScopeConfig(
        enabled=True,
        required=False,
        slice="hermes-kanban-workers.slice",
        memory_high="2G",
        memory_max="3G",
        memory_swap_max="512M",
        tasks_max=512,
        oom_policy="stop",
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="promotion fence", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)
        unit = kb._systemd_scope_unit_name(task_id, run_id)
        kb._set_worker_launching(
            conn,
            task_id,
            scope_unit=unit,
            target=target,
            scope_config=config,
        )
        assert kb.complete_task(
            conn, task_id, result="done", expected_run_id=run_id,
        )

        launch = _scoped_receipt(task_id, run_id, pid=6543)
        launch = kb._WorkerLaunchPid(
            int(launch),
            launch_mode="systemd-user-scope",
            scope_unit=launch.scope_unit,
            verification_status="launching",
            manager_kind=launch.manager_kind,
            manager_uid=launch.manager_uid,
            launch_acknowledged=False,
            scope_slice=launch.scope_slice,
            memory_high=launch.memory_high,
            memory_max=launch.memory_max,
            memory_swap_max=launch.memory_swap_max,
            tasks_max=launch.tasks_max,
            oom_policy=launch.oom_policy,
            control_group=launch.control_group,
        )
        with pytest.raises(RuntimeError, match="unauthenticated worker scope receipt"):
            kb._set_worker_pid(conn, task_id, launch)
        kb._mark_worker_launch_cleanup_pending(
            conn, task_id, launch, "promotion failed",
        )

        pending = kb.get_run(conn, run_id)
        task = kb.get_task(conn, task_id)
        assert pending is not None and task is not None
        assert pending.terminal_action == "complete"
        assert pending.terminal_payload == {
            "result": "done",
            "summary": None,
            "metadata": None,
            "created_cards": [],
            "fire_lifecycle_hook": True,
        }
        assert pending.reap_state == "launch_cleanup_pending"
        assert pending.ended_at is None
        assert task.status == "running" and task.current_run_id == run_id
        assert kb.reconcile_worker_scope_terminals(conn) == []
        still_pending = kb.get_run(conn, run_id)
        still_running = kb.get_task(conn, task_id)
        assert still_pending is not None and still_pending.ended_at is None
        assert still_pending.reap_state == "launch_cleanup_pending"
        assert still_running is not None and still_running.current_run_id == run_id

        state["value"] = "not-found"
        assert kb.reconcile_worker_scope_terminals(conn) == []
        closed = kb.get_run(conn, run_id)
        released = kb.get_task(conn, task_id)

    assert closed is not None and closed.ended_at is not None
    assert closed.outcome == "spawn_failed"
    assert released is not None and released.current_run_id is None
    assert released.status == "ready"


@pytest.mark.parametrize("operation", [
    "archive",
    "delete",
    "delete_archived",
    "schedule",
])
def test_terminal_removal_paths_fail_closed_on_scope_cleanup_failure(
    kanban_home, all_assignees_spawnable, monkeypatch, operation
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(
        kb, "_systemd_user_manager_target_for_uid", lambda uid: target,
    )
    monkeypatch.setattr(kb, "_systemd_scope_state", lambda *args: "active")
    monkeypatch.setattr(kb, "_systemd_scope_process_ids", lambda path: (5432,))
    stops = []
    monkeypatch.setattr(
        kb,
        "_stop_systemd_scope",
        lambda unit, manager: stops.append((unit, manager)) or False,
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=operation, assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        kb._set_worker_pid(conn, task_id, _scoped_receipt(task_id, run_id))
        if operation == "delete_archived":
            conn.execute(
                "UPDATE tasks SET status='archived' WHERE id=?", (task_id,)
            )
            conn.commit()

        if operation == "archive":
            ok = kb.archive_task(conn, task_id)
        elif operation == "delete":
            ok = kb.delete_task(conn, task_id)
        elif operation == "delete_archived":
            ok = kb.delete_archived_task(conn, task_id)
        else:
            ok = kb.schedule_task(conn, task_id, expected_run_id=run_id)

        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)

    assert ok is False
    assert len(stops) == 1
    assert task is not None
    assert task.current_run_id == run_id
    assert task.status == ("archived" if operation == "delete_archived" else "running")
    assert run is not None and run.ended_at is None and run.outcome is None


def test_parent_reopen_invalidation_fails_closed_without_clearing_scope_identity(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(
        kb, "_systemd_user_manager_target_for_uid", lambda uid: target,
    )
    monkeypatch.setattr(kb, "_systemd_scope_state", lambda *args: "active")
    monkeypatch.setattr(kb, "_stop_systemd_scope", lambda *args: False)

    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
        assert kb.complete_task(conn, parent_id)
        child_id = kb.create_task(
            conn, title="running child", assignee="worker", parents=[parent_id],
        )
        claimed = kb.claim_task(conn, child_id)
        assert claimed is not None and claimed.current_run_id is not None
        kb._set_worker_pid(
            conn, child_id, _scoped_receipt(child_id, claimed.current_run_id),
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='todo', completed_at=NULL WHERE id=?",
                (parent_id,),
            )

        with pytest.raises(RuntimeError, match="scope cleanup"):
            kb.invalidate_descendants_for_parent_reopen(
                conn, parent_id, author="operator",
            )

        child = kb.get_task(conn, child_id)
        run = kb.get_run(conn, claimed.current_run_id)

    assert child is not None and child.status == "running"
    assert child.current_run_id == claimed.current_run_id
    assert run is not None and run.ended_at is None and run.outcome is None


def test_parent_reopen_scoped_descendant_uses_postcommit_scope_reap(
    isolated_scope_home, monkeypatch,
):
    """Standalone and composed parent reopen paths never PID-signal a scope."""
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    monkeypatch.setattr(kb, "_systemd_user_manager_target_for_uid", lambda uid: target)
    _mock_confirmed_scope_stop(monkeypatch)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kills = []
    monkeypatch.setattr(
        kb,
        "_terminate_reclaimed_worker",
        lambda *args, **kwargs: kills.append((args, kwargs)),
    )

    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
        assert kb.complete_task(conn, parent_id)
        child_id = kb.create_task(
            conn, title="scoped child", assignee="worker", parents=[parent_id],
        )
        claimed = kb.claim_task(conn, child_id)
        assert claimed is not None and claimed.current_run_id is not None
        kb._set_worker_pid(
            conn,
            child_id,
            _scoped_receipt(child_id, int(claimed.current_run_id), pid=9876),
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='todo', completed_at=NULL WHERE id=?",
                (parent_id,),
            )

        with kb.write_txn(conn):
            result = kb.invalidate_descendants_for_parent_reopen(
                conn, parent_id, author="dashboard",
            )
            assert result["terminations"] == []

        child = kb.get_task(conn, child_id)
        assert child is not None and child.status == "todo"
    assert kills == []


def test_native_systemd_scope_lifecycle_when_user_manager_is_available(kanban_home):
    """Exercise the real unit/control-group boundary, not just argv shape."""
    if not sys.platform.startswith("linux"):
        pytest.skip("native systemd user scopes require Linux")
    cgroup = kb._current_cgroup_path()
    target = kb._systemd_user_manager_target_for_cgroup(cgroup)
    if target is None:
        pytest.skip("dispatcher is not hosted in its own authenticated user-service cgroup")
    runner = kb.shutil.which("systemd-run")
    if runner is None:
        pytest.skip("systemd-run is unavailable")
    version = kb._systemd_run_version(runner)
    if version is None or version < kb._SYSTEMD_SCOPE_MIN_VERSION:
        pytest.skip("systemd-run lacks the required transient-scope support")
    if not kb._systemd_user_manager_reachable(target):
        pytest.skip("authenticated systemd user manager is unreachable")

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="native scope timeout",
            assignee="worker",
            max_runtime_seconds=1,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None

    scoped, unit, manager = kb._systemd_scope_argv(
        ["/bin/sleep", "30"],
        claimed,
        cgroup_path=cgroup,
        manager_target=target,
        systemd_run=runner,
        user_manager_ready=True,
    )
    assert unit is not None
    assert manager is target
    assert manager is not None
    native_env = dict(os.environ)
    native_env.update(kb._systemd_user_manager_environment(target))
    proc = subprocess.Popen(
        scoped,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=native_env,
    )
    try:
        worker_pid = kb._verify_systemd_scope_worker_pid(
            proc,
            unit,
            manager,
            ["/bin/sleep", "30"],
        )
        props = kb._systemd_scope_properties(unit, manager)
        assert props is not None
        control_group = props["ControlGroup"]
        assert worker_pid in kb._systemd_scope_process_ids(control_group)
        assert kb._process_cgroup_path(worker_pid) == control_group

        with kb.connect() as conn:
            config = kb._worker_scope_config()
            kb._set_worker_pid(
                conn,
                task_id,
                kb._WorkerLaunchPid(
                    int(worker_pid),
                    launch_mode="systemd-user-scope",
                    scope_unit=unit,
                    verification_status="verified",
                    manager_kind=kb._SYSTEMD_USER_MANAGER_KIND,
                    manager_uid=manager.uid,
                    launch_acknowledged=True,
                    scope_slice=config.slice,
                    memory_high=config.memory_high,
                    memory_max=config.memory_max,
                    memory_swap_max=config.memory_swap_max,
                    tasks_max=config.tasks_max,
                    oom_policy=config.oom_policy,
                    control_group=control_group,
                ),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? WHERE id = ?",
                (int(kb.time.time()) - 2, claimed.current_run_id),
            )
            conn.commit()
            assert kb.enforce_max_runtime(conn) == [task_id]
            timed_out_task = kb.get_task(conn, task_id)
            assert timed_out_task is not None
            assert timed_out_task.status == "ready"
        proc.wait(timeout=kb._SYSTEMD_SCOPE_CLEANUP_TIMEOUT)
    finally:
        kb._cleanup_systemd_scope_launch(proc, unit, manager)
    assert proc.poll() is not None
    props = kb._systemd_scope_properties(unit, manager)
    assert props is None or props.get("LoadState") == "not-found"


def test_native_systemd_scope_archive_cleanup_failure_keeps_identity(
    kanban_home, monkeypatch
):
    """A real manager-backed scope remains fenced when cleanup is unconfirmed."""
    if not sys.platform.startswith("linux"):
        pytest.skip("native systemd user scopes require Linux")
    cgroup = kb._current_cgroup_path()
    target = kb._systemd_user_manager_target_for_cgroup(cgroup)
    if target is None:
        pytest.skip("dispatcher is not hosted in its own authenticated user-service cgroup")
    runner = kb.shutil.which("systemd-run")
    if runner is None:
        pytest.skip("systemd-run is unavailable")
    version = kb._systemd_run_version(runner)
    if version is None or version < kb._SYSTEMD_SCOPE_MIN_VERSION:
        pytest.skip("systemd-run lacks the required transient-scope support")
    if not kb._systemd_user_manager_reachable(target):
        pytest.skip("authenticated systemd user manager is unreachable")

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="native archive cleanup", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None

    scoped, unit, manager = kb._systemd_scope_argv(
        ["/bin/sleep", "120"],
        claimed,
        cgroup_path=cgroup,
        manager_target=target,
        systemd_run=runner,
        user_manager_ready=True,
    )
    assert unit is not None and manager is target
    native_env = dict(os.environ)
    native_env.update(kb._systemd_user_manager_environment(target))
    real_stop = kb._stop_systemd_scope
    proc = subprocess.Popen(
        scoped,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=native_env,
    )
    try:
        worker_pid = kb._verify_systemd_scope_worker_pid(
            proc, unit, manager, ["/bin/sleep", "120"],
        )
        props = kb._systemd_scope_properties(unit, manager)
        assert props is not None
        control_group = props["ControlGroup"]
        with kb.connect() as conn:
            config = kb._worker_scope_config()
            kb._set_worker_pid(
                conn,
                task_id,
                kb._WorkerLaunchPid(
                    int(worker_pid),
                    launch_mode="systemd-user-scope",
                    scope_unit=unit,
                    verification_status="verified",
                    manager_kind=kb._SYSTEMD_USER_MANAGER_KIND,
                    manager_uid=manager.uid,
                    launch_acknowledged=True,
                    scope_slice=config.slice,
                    memory_high=config.memory_high,
                    memory_max=config.memory_max,
                    memory_swap_max=config.memory_swap_max,
                    tasks_max=config.tasks_max,
                    oom_policy=config.oom_policy,
                    control_group=control_group,
                ),
            )
            monkeypatch.setattr(kb, "_stop_systemd_scope", lambda *args: False)
            assert not kb.archive_task(conn, task_id)
            task = kb.get_task(conn, task_id)
            run = kb.latest_run(conn, task_id)
            assert task is not None and task.status == "running"
            assert task.current_run_id == claimed.current_run_id
            assert run is not None and run.ended_at is None
            assert kb._systemd_scope_state(unit, manager) in {"active", "activating"}
            assert int(worker_pid) in kb._systemd_scope_process_ids(control_group)
            # Restore the production cleanup path and prove the same exact
            # run can now be archived only after its scope disappears.
            monkeypatch.setattr(kb, "_stop_systemd_scope", real_stop)
            assert kb.archive_task(conn, task_id)
            archived = kb.get_task(conn, task_id)
            ended = kb.get_run(conn, claimed.current_run_id)
            assert archived is not None
            assert archived.status == "archived"
            assert archived.current_run_id is None
            assert ended is not None
            assert ended.ended_at is not None
            assert ended.outcome == "reclaimed"
            final_state = kb._systemd_scope_state(unit, manager)
            assert final_state == "not-found" or (
                final_state == "inactive"
                and kb._systemd_scope_process_ids(control_group) == ()
            )
    finally:
        monkeypatch.setattr(kb, "_stop_systemd_scope", real_stop)
        kb._cleanup_systemd_scope_launch(proc, unit, manager)
    assert proc.poll() is not None
    props = kb._systemd_scope_properties(unit, manager)
    assert props is None or props.get("LoadState") == "not-found"


def test_required_native_dispatch_preflights_before_ready_or_review_claim(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    config = kb._WorkerScopeConfig(
        enabled=True,
        required=True,
        slice="hermes-kanban-workers.slice",
        memory_high="2G",
        memory_max="3G",
        memory_swap_max="512M",
        tasks_max=512,
        oom_policy="stop",
    )
    monkeypatch.setattr(kb, "_worker_scope_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(
        kb,
        "_systemd_scope_preflight",
        lambda **kwargs: (False, "manager unavailable", None),
    )
    monkeypatch.setattr(
        kb,
        "release_stale_claims",
        lambda conn: pytest.fail("required preflight must precede reclaim"),
    )
    with kb.connect() as conn:
        ready_id = kb.create_task(conn, title="ready", assignee="worker")
        review_id = kb.create_task(conn, title="review", assignee="reviewer")
        assert kb.request_review(conn, review_id, reviewer="reviewer")

        result = kb.dispatch_once(conn)

        assert result.spawned == []
        assert kb.get_task(conn, ready_id).status == "ready"
        assert kb.get_task(conn, review_id).status == "review"
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0


def test_serial_cap_keeps_direct_fallback_with_multiple_ready_rows(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A max_spawn=1 tick cannot overlap, so scope failure is not fatal."""
    preflight_calls = []
    spawned = []

    def failed_preflight(**kwargs):
        preflight_calls.append(kwargs)
        return False, "systemd unavailable", None

    def fake_default_spawn(
        task, workspace, *, board=None, require_scope=False, scope_config=None
    ):
        spawned.append((task.id, require_scope))
        return 9000 + len(spawned)

    monkeypatch.setattr(kb, "_systemd_scope_preflight", failed_preflight)
    monkeypatch.setattr(kb, "_default_spawn", fake_default_spawn)

    with kb.connect() as conn:
        first = kb.create_task(conn, title="first", assignee="worker")
        second = kb.create_task(conn, title="second", assignee="worker")

        result = kb.dispatch_once(conn, max_spawn=1)

        assert [item[0] for item in result.spawned] == [first]
        assert spawned == [(first, False)]
        assert preflight_calls == []
        assert kb.get_task(conn, first).status == "running"
        assert kb.get_task(conn, second).status == "ready"
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1


def test_per_profile_cap_counts_effective_native_candidates_for_serial_fallback(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A profile cap of one keeps a same-profile tick on the direct path."""
    preflight_calls = []
    spawned = []

    def failed_preflight(**kwargs):
        preflight_calls.append(kwargs)
        return False, "systemd unavailable", None

    def fake_default_spawn(
        task,
        workspace,
        *,
        board=None,
        require_scope=False,
        scope_config=None,
        **kwargs,
    ):
        spawned.append((task.id, require_scope))
        return 9100 + len(spawned)

    monkeypatch.setattr(kb, "_systemd_scope_preflight", failed_preflight)
    monkeypatch.setattr(kb, "_default_spawn", fake_default_spawn)

    with kb.connect() as conn:
        first = kb.create_task(conn, title="first", assignee="worker")
        second = kb.create_task(conn, title="second", assignee="worker")

        result = kb.dispatch_once(conn, max_in_progress_per_profile=1)

        assert [item[0] for item in result.spawned] == [first]
        assert spawned == [(first, False)]
        assert preflight_calls == []
        assert result.skipped_per_profile_capped == [(second, "worker", 1)]
        assert kb.get_task(conn, first).status == "running"
        assert kb.get_task(conn, second).status == "ready"
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1


def test_two_eligible_profiles_fail_closed_without_native_scope(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """Two genuinely eligible profiles still require an authenticated manager."""
    preflight_calls = []

    def failed_preflight(**kwargs):
        preflight_calls.append(kwargs)
        return False, "systemd unavailable", None

    monkeypatch.setattr(kb, "_systemd_scope_preflight", failed_preflight)

    with kb.connect() as conn:
        first = kb.create_task(conn, title="first", assignee="worker-a")
        second = kb.create_task(conn, title="second", assignee="worker-b")

        result = kb.dispatch_once(conn)

        assert result.spawned == []
        assert len(preflight_calls) == 1
        assert kb.get_task(conn, first).status == "ready"
        assert kb.get_task(conn, second).status == "ready"
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0


def test_cross_board_host_occupancy_requires_scope_before_claim(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """Dashboard admission cannot claim beside a running foreign worker."""
    kb.create_board("second")
    second_conn = kb.connect(board="second")
    try:
        other_task = kb.create_task(
            second_conn, title="other-board-running", assignee="worker"
        )
        assert kb.claim_task(second_conn, other_task) is not None
    finally:
        second_conn.close()

    preflight_calls = []

    def failed_preflight(**kwargs):
        preflight_calls.append(kwargs)
        return False, "systemd unavailable", None

    monkeypatch.setattr(kb, "_systemd_scope_preflight", failed_preflight)

    with kb.connect() as conn:
        first = kb.create_task(conn, title="default-first", assignee="worker")
        before = kb.get_task(conn, first)

        result = kb.dispatch_once(conn, board="default", max_spawn=8)

        assert result.spawned == []
        assert len(preflight_calls) == 1
        after = kb.get_task(conn, first)
        assert after.status == "ready"
        assert after.claim_lock == before.claim_lock
        assert after.current_run_id == before.current_run_id
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0


def test_concurrent_native_boards_serialize_host_admission(
    isolated_scope_home, all_assignees_spawnable, monkeypatch,
):
    """A max_in_progress=1 host decision cannot admit two native workers."""
    # Keep the fixture's explicit isolated HERMES_HOME/HERMES_KANBAN_HOME and
    # HERMES_KANBAN_DB, but split board-aware resolution after the default DB
    # has been initialized. This prevents the legacy env override from making
    # both board slugs aliases of one DB (and therefore one board dispatch
    # lock), which would not exercise the host-wide admission lock.
    db_paths = {
        "default": isolated_scope_home / "kanban.db",
        "second": isolated_scope_home / "kanban" / "boards" / "second" / "kanban.db",
    }

    def board_db_path(board=None):
        return db_paths[(board or "default").strip().lower()]

    monkeypatch.setattr(kb, "kanban_db_path", board_db_path)
    kb.create_board("second")
    assert kb.kanban_db_path(board="default") != kb.kanban_db_path(board="second")
    assert kb.kanban_db_path(board="default").with_name(
        "kanban.db.dispatch.lock"
    ) != kb.kanban_db_path(board="second").with_name(
        "kanban.db.dispatch.lock"
    )
    first_observed = threading.Event()
    allow_first_observation = threading.Event()
    second_observed = threading.Event()
    first_claimed = threading.Event()
    spawn_calls = []
    spawn_lock = threading.Lock()
    results = {}
    errors = []

    monkeypatch.setattr(
        kb,
        "_systemd_scope_preflight",
        lambda **kwargs: (True, "verified", None),
    )

    real_claim_task = kb.claim_task

    def recording_claim(conn, task_id, **kwargs):
        claimed = real_claim_task(conn, task_id, **kwargs)
        if claimed is not None:
            first_claimed.set()
        return claimed

    def strict_observation(board=None):
        if board == "default":
            first_observed.set()
            assert allow_first_observation.wait(timeout=2)
            return kb._OtherBoardsRunningObservation(0, True)
        second_observed.set()
        return kb._OtherBoardsRunningObservation(
            1 if first_claimed.is_set() else 0,
            True,
        )

    def fake_native_spawn(task, workspace, **kwargs):
        with spawn_lock:
            spawn_calls.append(task.id)
        return 9100 + len(spawn_calls)

    monkeypatch.setattr(kb, "claim_task", recording_claim)
    monkeypatch.setattr(kb, "observe_running_tasks_other_boards", strict_observation)
    monkeypatch.setattr(kb, "_default_spawn", fake_native_spawn)

    with kb.connect(board="default") as conn:
        first_id = kb.create_task(conn, title="first", assignee="worker")
    with kb.connect(board="second") as conn:
        second_id = kb.create_task(conn, title="second", assignee="worker")

    def dispatch(board):
        try:
            with kb.connect(board=board) as conn:
                results[board] = kb.dispatch_once(
                    conn,
                    board=board,
                    max_in_progress=1,
                )
        except BaseException as exc:  # surface worker-thread failures below
            errors.append(exc)

    first_thread = threading.Thread(target=dispatch, args=("default",))
    first_thread.start()
    assert first_observed.wait(timeout=2)

    second_thread = threading.Thread(target=dispatch, args=("second",))
    second_thread.start()
    # The first dispatcher owns the host lock from its zero snapshot through
    # claim and launch receipt. A second board must not even observe until it
    # has released that transition.
    assert not second_observed.wait(timeout=0.2)
    allow_first_observation.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert spawn_calls == [first_id]
    assert results["default"].spawned
    assert results["second"].spawned == []
    with kb.connect(board="default") as conn:
        assert kb.get_task(conn, first_id).status == "running"
    with kb.connect(board="second") as conn:
        assert kb.get_task(conn, second_id).status == "ready"


def test_native_admission_lock_open_failure_fails_closed_before_claim(
    isolated_scope_home, all_assignees_spawnable, monkeypatch,
):
    admission_path = isolated_scope_home / "kanban" / ".native-admission.lock"
    real_open = Path.open

    def unavailable(path, *args, **kwargs):
        if path == admission_path:
            raise PermissionError("admission lock unavailable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unavailable)
    monkeypatch.setattr(
        kb,
        "claim_task",
        lambda *args, **kwargs: pytest.fail("open failure claimed a native task"),
    )
    monkeypatch.setattr(
        kb,
        "_default_spawn",
        lambda *args, **kwargs: pytest.fail("open failure spawned a native worker"),
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="open failure", assignee="worker")
        before = kb.get_task(conn, task_id)
        result = kb.dispatch_once(conn, max_in_progress=1)
        after = kb.get_task(conn, task_id)

        assert result.spawned == []
        assert after is not None and before is not None
        assert after.status == before.status == "ready"
        assert after.claim_lock == before.claim_lock
        assert before.claim_lock is None
        assert after.workspace_path == before.workspace_path
        assert before.workspace_path is None
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
    assert not admission_path.exists()


def test_native_admission_lock_identity_mismatch_fails_closed_before_claim(
    isolated_scope_home, all_assignees_spawnable, monkeypatch,
):
    admission_path = isolated_scope_home / "kanban" / ".native-admission.lock"
    real_lstat = Path.lstat

    def replaced(path):
        info = real_lstat(path)
        if path == admission_path:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_dev=info.st_dev,
                st_ino=info.st_ino + 1,
            )
        return info

    monkeypatch.setattr(Path, "lstat", replaced)
    monkeypatch.setattr(
        kb,
        "claim_task",
        lambda *args, **kwargs: pytest.fail(
            "identity mismatch claimed a native task"
        ),
    )
    monkeypatch.setattr(
        kb,
        "resolve_workspace",
        lambda *args, **kwargs: pytest.fail(
            "identity mismatch resolved a native workspace"
        ),
    )
    monkeypatch.setattr(
        kb,
        "_default_spawn",
        lambda *args, **kwargs: pytest.fail(
            "identity mismatch spawned a native worker"
        ),
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="identity mismatch", assignee="worker")
        result = kb.dispatch_once(conn, max_in_progress=1)
        task = kb.get_task(conn, task_id)

        assert result.spawned == []
        assert task is not None and task.status == "ready"
        assert task.claim_lock is None
        assert task.workspace_path is None
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
    assert admission_path.is_file()


def test_native_admission_lock_is_not_used_by_dry_run(
    isolated_scope_home, all_assignees_spawnable, monkeypatch,
):
    admission_path = isolated_scope_home / "kanban" / ".native-admission.lock"
    monkeypatch.setattr(
        kb,
        "_native_admission_lock",
        lambda: pytest.fail("dry-run opened the native admission lock"),
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="dry-run lock", assignee="worker")
        result = kb.dispatch_once(
            conn,
            spawn_fn=kb._default_spawn,
            dry_run=True,
            max_in_progress=1,
        )
        task = kb.get_task(conn, task_id)

        assert result.spawned
        assert task is not None and task.status == "ready"
        assert task.claim_lock is None
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
    assert not admission_path.exists()


def test_native_admission_lock_is_not_used_by_custom_spawn(
    isolated_scope_home, all_assignees_spawnable, monkeypatch,
):
    admission_path = isolated_scope_home / "kanban" / ".native-admission.lock"
    monkeypatch.setattr(
        kb,
        "_native_admission_lock",
        lambda: pytest.fail("custom spawn opened the native admission lock"),
    )
    spawned = []

    def custom_spawn(task, workspace):
        spawned.append((task.id, workspace))
        return 9876

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="custom lock", assignee="worker")
        result = kb.dispatch_once(
            conn,
            spawn_fn=custom_spawn,
            max_in_progress=1,
        )
        task = kb.get_task(conn, task_id)

        assert result.spawned and result.spawned[0][0] == task_id
        assert spawned and spawned[0][0] == task_id
        assert task is not None and task.status == "running"
        assert task.worker_pid == 9876
    assert not admission_path.exists()


def test_idle_independent_board_requires_scope_before_dashboard_claim(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """Distinct idle DB locks still create a cross-board zero/zero race."""
    kb.create_board("second")
    preflight_calls = []
    spawned = []

    monkeypatch.setattr(
        kb,
        "_systemd_scope_preflight",
        lambda **kwargs: preflight_calls.append(kwargs)
        or (False, "systemd unavailable", None),
    )
    monkeypatch.setattr(
        kb,
        "_default_spawn",
        lambda task, workspace, **kwargs: spawned.append(task.id) or 9001,
    )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="default-ready", assignee="worker")
        before = kb.get_task(conn, task_id)

        result = kb.dispatch_once(conn, board="default", max_spawn=8)

        after = kb.get_task(conn, task_id)
        assert result.spawned == []
        assert spawned == []
        assert len(preflight_calls) == 1
        assert after.status == "ready"
        assert after.claim_lock == before.claim_lock
        assert after.current_run_id == before.current_run_id
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0


def test_unknown_cross_board_occupancy_fails_closed_before_native_claim(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Unknown foreign state allows maintenance but no native identity mutation."""
    spawned = []
    monkeypatch.setattr(
        kb, "observe_running_tasks_other_boards", lambda board=None: None,
    )
    monkeypatch.setattr(
        kb,
        "_default_spawn",
        lambda task, workspace, **kwargs: spawned.append(task.id) or 9001,
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="unknown occupancy", assignee="worker")
        maintenance_id = kb.create_task(
            conn, title="maintenance promotion", assignee="worker",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ?",
                (maintenance_id,),
            )
        before = kb.get_task(conn, task_id)
        result = kb.dispatch_once(conn, max_in_progress=None)

        after = kb.get_task(conn, task_id)
        assert result.promoted == 1
        assert kb.get_task(conn, maintenance_id).status == "ready"
        assert result.spawned == []
        assert spawned == []
        assert after.status == before.status == "ready"
        assert after.claim_lock == before.claim_lock
        assert after.current_run_id == before.current_run_id
        assert after.workspace_path == before.workspace_path
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0


def test_unknown_cross_board_occupancy_without_candidates_skips_observer_but_maintains(
    kanban_home, monkeypatch,
):
    """An empty post-maintenance queue needs no foreign-board observation."""
    observed = []
    maintenance_calls = []
    monkeypatch.setattr(
        kb,
        "observe_running_tasks_other_boards",
        lambda board=None: observed.append(board) or None,
    )
    monkeypatch.setattr(
        kb,
        "release_stale_claims",
        lambda conn: maintenance_calls.append("release_stale_claims") or 0,
    )

    with kb.connect() as conn:
        result = kb.dispatch_once(conn)

    assert result.spawned == []
    assert maintenance_calls == ["release_stale_claims"]
    assert observed == []


def test_exact_idle_host_keeps_serial_direct_fallback_without_global_cap(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """One candidate + exact idle host remains serial-compatible without a cap."""
    spawned = []
    preflight_calls = []
    monkeypatch.setattr(
        kb,
        "observe_running_tasks_other_boards",
        lambda board=None: kb._OtherBoardsRunningObservation(
            running_count=0,
            has_independent_db=False,
        ),
    )
    monkeypatch.setattr(
        kb,
        "_systemd_scope_preflight",
        lambda **kwargs: preflight_calls.append(kwargs)
        or (False, "manager unavailable", None),
    )

    def fake_spawn(task, workspace, *, require_scope=False, **kwargs):
        spawned.append((task.id, require_scope))
        return 9002

    monkeypatch.setattr(kb, "_default_spawn", fake_spawn)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="serial fallback", assignee="worker")
        result = kb.dispatch_once(conn)

    assert [item[0] for item in result.spawned] == [task_id]
    assert spawned == [(task_id, False)]
    assert preflight_calls == []


def test_same_file_board_aliases_are_not_foreign_concurrency(
    kanban_home, monkeypatch
):
    """A direct DB pin collapses every board slug onto the current lock."""
    kb.create_board("second")
    pinned = kb.kanban_db_path(board="default").resolve()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))

    observation = kb.observe_running_tasks_other_boards("default")

    assert observation == kb._OtherBoardsRunningObservation(
        running_count=0,
        has_independent_db=False,
    )


def test_idle_checkpointed_foreign_db_observation_is_strictly_read_only(
    kanban_home,
):
    """An idle checkpointed board is counted without creating sidecars."""
    kb.create_board("second")
    second_root = kb.board_dir("second")
    assert not list(second_root.glob("kanban.db-*"))
    before = _tree_snapshot(second_root)

    observation = kb.observe_running_tasks_other_boards("default")

    assert observation == kb._OtherBoardsRunningObservation(
        running_count=0,
        has_independent_db=True,
    )
    assert _tree_snapshot(second_root) == before


def test_nonempty_foreign_wal_fails_closed_without_filesystem_mutation(
    kanban_home,
):
    """A live WAL makes exact foreign occupancy unprovable and remains untouched."""
    kb.create_board("second")
    second_root = kb.board_dir("second")
    second_conn = kb.connect(board="second")
    task_id = kb.create_task(second_conn, title="wal-running", assignee="worker")
    assert kb.claim_task(second_conn, task_id) is not None
    wal_path = Path(f"{kb.kanban_db_path(board='second')}-wal")
    assert wal_path.is_file() and wal_path.stat().st_size > 0
    before = _tree_snapshot(second_root)

    try:
        assert kb.observe_running_tasks_other_boards("default") is None
        assert _tree_snapshot(second_root) == before
    finally:
        second_conn.close()


def test_legacy_other_board_count_retains_readable_partial_result(
    kanban_home,
):
    """The compatibility counter remains fail-open independently per DB."""
    kb.create_board("readable")
    kb.create_board("broken")
    with kb.connect(board="readable") as conn:
        task_id = kb.create_task(conn, title="running", assignee="worker")
        assert kb.claim_task(conn, task_id) is not None

    broken_path = kb.kanban_db_path(board="broken")
    broken_path.write_bytes(b"not a sqlite database")

    assert kb.count_running_tasks_other_boards("default") == 1


def test_native_dispatch_uses_one_scope_config_snapshot_for_argv_and_receipt(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """A config change between potential reads cannot split one launch receipt."""
    target = kb._SystemdUserManagerTarget(
        os.getuid(),
        Path("/run/user") / str(os.getuid()),
        Path("/run/user") / str(os.getuid()) / "bus",
    )
    first_config = kb._WorkerScopeConfig(
        enabled=True,
        required=False,
        slice="snapshot-one.slice",
        memory_high="1G",
        memory_max="2G",
        memory_swap_max="128M",
        tasks_max=123,
        oom_policy="stop",
    )
    second_config = kb._WorkerScopeConfig(
        enabled=True,
        required=False,
        slice="snapshot-two.slice",
        memory_high="4G",
        memory_max="5G",
        memory_swap_max="256M",
        tasks_max=456,
        oom_policy="kill",
    )
    config_reads = []
    captured = {}

    def changing_config(*args, **kwargs):
        config_reads.append(True)
        return first_config if len(config_reads) == 1 else second_config

    def fake_preflight(**kwargs):
        assert kwargs["scope_config"] is first_config
        return True, "ready", target

    class FakeProc:
        pid = 6789

    monkeypatch.setattr(kb, "_worker_scope_config", changing_config)
    monkeypatch.setattr(kb, "_systemd_scope_preflight", fake_preflight)
    monkeypatch.setattr(
        kb,
        "_systemd_user_manager_target_for_uid",
        lambda uid: target,
    )
    monkeypatch.setattr(
        kb,
        "_verify_systemd_scope_worker_pid",
        lambda *args: kb._VerifiedWorkerPid(2468, control_group="/snapshot.scope"),
    )

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(kb.subprocess, "Popen", fake_popen)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="snapshot", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=kb._default_spawn)
        run = kb.latest_run(conn, task_id)

    assert result.spawned and result.spawned[0][0] == task_id
    assert config_reads == [True]
    assert "--slice=snapshot-one.slice" in captured["cmd"]
    assert "--slice=snapshot-two.slice" not in captured["cmd"]
    assert run is not None
    assert run.scope_slice == first_config.slice
    assert run.memory_high == first_config.memory_high
    assert run.memory_max == first_config.memory_max
    assert run.memory_swap_max == first_config.memory_swap_max
    assert run.tasks_max == first_config.tasks_max
    assert run.oom_policy == first_config.oom_policy
