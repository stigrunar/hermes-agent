"""Opt-in native proof for Kanban's transient user-scope lifecycle contract."""

from __future__ import annotations

import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


pytestmark = [
    pytest.mark.integration,
    pytest.mark.live_system_guard_bypass,
    pytest.mark.skipif(
        not sys.platform.startswith("linux"),
        reason="systemd user scopes are Linux-only",
    ),
]


def _raw_user_manager_capability(
    systemd_run: str,
    systemctl: str,
) -> tuple[dict[str, str] | None, str | None]:
    """Probe host capability without using the production targeting seam."""
    uid = os.getuid()
    runtime_dir = Path("/run/user") / str(uid)
    bus_path = runtime_dir / "bus"
    try:
        runtime_stat = os.lstat(runtime_dir)
        bus_stat = os.lstat(bus_path)
    except OSError:
        return None, "local /run/user/<uid>/bus is unavailable"
    if (
        not stat.S_ISDIR(runtime_stat.st_mode)
        or runtime_stat.st_uid != uid
        or stat.S_IMODE(runtime_stat.st_mode) & 0o077
        or not stat.S_ISSOCK(bus_stat.st_mode)
        or bus_stat.st_uid != uid
    ):
        return None, "local /run/user/<uid>/bus is not a trusted user target"
    explicit_env = {
        "XDG_RUNTIME_DIR": str(runtime_dir),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus_path}",
    }
    try:
        version = subprocess.run(
            [systemd_run, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
            check=False,
        )
        reachable = subprocess.run(
            [systemctl, "--user", "show", "--property=Version", "--value"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
            check=False,
            env=explicit_env,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None, "required systemd tooling is unavailable"
    match = re.search(r"\bsystemd\s+(\d+)\b", version.stdout or "")
    if version.returncode != 0 or match is None or int(match.group(1)) < 236:
        return None, "systemd-run with --collect support is unavailable"
    if reachable.returncode != 0:
        return None, "explicitly addressed local systemd user manager is unavailable"
    return explicit_env, None


def _process_identity(pid: int) -> tuple[int, int] | None:
    """Return (owner uid, starttime) for one non-zombie Linux PID."""
    proc_dir = Path(f"/proc/{int(pid)}")
    try:
        owner_uid = proc_dir.stat().st_uid
        text = (proc_dir / "stat").read_text(encoding="utf-8")
        tail = text[text.rfind(")") + 2 :].split()
        if not tail or tail[0] == "Z":
            return None
        return owner_uid, int(tail[19])
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None


def _wait_original_process_dead(
    pid: int,
    identity: tuple[int, int],
    *,
    timeout: float = 5.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process_identity(pid) != identity:
            return True
        time.sleep(0.05)
    return _process_identity(pid) != identity


def _signal_original_process(
    pid: int,
    identity: tuple[int, int] | None,
    sig: int,
) -> None:
    """Last-resort cleanup without signaling a reused or foreign PID."""
    if identity is None or identity[0] != os.getuid():
        return
    if _process_identity(pid) != identity:
        return
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _signal_original_process_group(
    pid: int,
    identity: tuple[int, int] | None,
    sig: int,
) -> None:
    """Last-resort cleanup for the disposable leader and its descendants."""
    if identity is None or identity[0] != os.getuid():
        return
    if _process_identity(pid) != identity:
        return
    try:
        if os.getpgid(pid) != pid:
            return
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def test_native_cross_manager_receipt_stops_descendants_and_collects(
    tmp_path,
    monkeypatch,
):
    """Exercise Phase B receipt recovery and whole-unit stop without ambient bus."""
    systemd_run = shutil.which("systemd-run")
    systemctl = shutil.which("systemctl")
    if not systemd_run or not systemctl:
        pytest.skip("systemd-run and systemctl are required")

    raw_env, unavailable_reason = _raw_user_manager_capability(
        systemd_run,
        systemctl,
    )
    if unavailable_reason is not None:
        pytest.skip(unavailable_reason)
    assert raw_env is not None

    # Once the host is independently proven capable, every production helper
    # in the repaired seam must agree. A resolver/version/env regression is a
    # test failure, never a capability skip.
    target = kb._current_systemd_user_manager_target()
    assert target is not None
    assert target.manager_uid == os.getuid()
    assert raw_env == kb._systemd_user_manager_environment(target)
    assert kb._systemd_user_scope_available(
        systemd_run,
        manager_target=target,
        systemctl=systemctl,
    )

    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    proc: subprocess.Popen[str] | None = None
    ready_read: int | None = None
    ready_write: int | None = None
    leader_pid: int | None = None
    child_pid: int | None = None
    leader_identity: tuple[int, int] | None = None
    child_identity: tuple[int, int] | None = None
    unit: str | None = None
    try:
        task_id = kb.create_task(conn, title="disposable native scope", assignee="test")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        assert claimed.current_run_id is not None
        assert claimed.claim_lock is not None
        run_id = int(claimed.current_run_id)
        unit = kb._worker_scope_unit_name(
            task_id,
            run_id,
            db_path=db_path,
        )
        assert kb._systemd_user_scope_state(
            unit,
            manager_target=target,
            systemctl=systemctl,
        ) == "not-found"

        leader_code = (
            "import os,subprocess,sys,time;"
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import time;time.sleep(300)']);"
            "print(os.getpid(),child.pid,flush=True);"
            "time.sleep(0.5)"
        )
        payload = [sys.executable, "-c", leader_code]
        ready_read, ready_write = os.pipe()
        argv = [
            systemd_run,
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            f"--unit={unit}",
            "--",
            *kb._scope_exec_ack_argv(payload, ready_write),
        ]
        launch_env = dict(os.environ)
        for key in (
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
            "DBUS_STARTER_ADDRESS",
            "DBUS_STARTER_BUS_TYPE",
        ):
            launch_env.pop(key, None)
        launch_env.update(kb._systemd_user_manager_environment(target))
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=launch_env,
            pass_fds=(ready_write,),
            start_new_session=True,
        )
        # Capture the launcher process identity immediately. In synchronous
        # scope mode this same PID execs the payload, and the stable starttime
        # lets cleanup safely target its disposable process group even if the
        # acknowledgement path itself fails.
        leader_pid = proc.pid
        leader_identity = _process_identity(leader_pid)
        os.close(ready_write)
        ready_write = None
        try:
            kb._await_scope_exec_ack(proc, ready_read, timeout=5.0)
        finally:
            os.close(ready_read)
            ready_read = None
        # systemd-run's synchronous scope contract plus the exec
        # acknowledgement makes proc.pid the actual payload leader. Capture
        # its kernel identity before any stdout parsing so cleanup remains safe
        # if proof collection fails mid-test.
        assert _process_identity(leader_pid) == leader_identity
        assert leader_identity is not None
        assert leader_identity[0] == os.getuid()

        assert proc.stdout is not None
        readable, _, _ = select.select([proc.stdout], [], [], 2.0)
        assert readable, "payload did not report leader/descendant PIDs"
        leader_text, child_text = proc.stdout.readline().strip().split()
        reported_leader_pid = int(leader_text)
        child_pid = int(child_text)
        assert reported_leader_pid == leader_pid
        child_identity = _process_identity(child_pid)
        assert child_identity is not None
        assert child_identity[0] == os.getuid()

        leader_cgroup = Path(f"/proc/{leader_pid}/cgroup").read_text()
        child_cgroup = Path(f"/proc/{child_pid}/cgroup").read_text()
        assert unit in leader_cgroup
        assert unit in child_cgroup

        handle = kb._WorkerLaunchPid(
            leader_pid,
            scope_unit=unit,
            manager_kind=target.manager_kind,
            manager_uid=target.manager_uid,
            launch_acknowledged=True,
        )
        assert kb._set_worker_pid(
            conn,
            task_id,
            handle,
            expected_run_id=run_id,
            expected_claim_lock=claimed.claim_lock,
        ) is True

        spawned = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "spawned"
        ]
        assert len(spawned) == 1
        assert spawned[0].payload is not None
        assert spawned[0].payload["launch_mode"] == "systemd-user-scope"
        assert spawned[0].payload["scope_unit"] == unit
        assert spawned[0].payload["manager_kind"] == target.manager_kind
        assert spawned[0].payload["manager_uid"] == target.manager_uid
        assert spawned[0].payload["launch_acknowledged"] is True

        # The leader exits while its descendant keeps the exact scope active.
        # Phase B must recover that acknowledged receipt, stop the whole unit,
        # and finalize only after the child is proved gone.
        proc.wait(timeout=5.0)
        assert _wait_original_process_dead(leader_pid, leader_identity)
        assert _process_identity(child_pid) == child_identity

        # First remove, then poison, both ambient selectors. Receipt recovery
        # and the stop path must keep using the validated persisted identity.
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        status = kb._worker_scope_state(conn, task_id, run_id)
        assert tuple(status) == (unit, "active")
        assert status.manager_target is not None
        assert status.manager_target.manager_uid == os.getuid()

        bogus_runtime = tmp_path / "wrong-runtime"
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(bogus_runtime))
        monkeypatch.setenv(
            "DBUS_SESSION_BUS_ADDRESS",
            f"unix:path={bogus_runtime / 'wrong-bus'}",
        )

        assert kb.complete_task(
            conn,
            task_id,
            result="fixture worker finished",
            expected_run_id=run_id,
        )
        pending_task = kb.get_task(conn, task_id)
        pending_run = kb.get_run(conn, run_id)
        assert pending_task is not None and pending_task.status == "running"
        assert pending_task.current_run_id == run_id
        assert pending_task.worker_pid == leader_pid
        assert pending_run is not None
        assert pending_run.ended_at is None
        assert pending_run.reap_state == "terminal_requested"
        pending_events = kb.list_events(conn, task_id)
        assert sum(event.kind == "terminal_requested" for event in pending_events) == 1
        assert sum(event.kind == "completed" for event in pending_events) == 0

        decisions = kb.reconcile_worker_reaps(conn, process_effects=True)
        assert len(decisions) == 1
        assert decisions[0]["task_id"] == task_id
        assert decisions[0]["run_id"] == run_id
        assert decisions[0]["state"] == "finalized"
        assert _wait_original_process_dead(child_pid, child_identity)
        done_task = kb.get_task(conn, task_id)
        done_run = kb.get_run(conn, run_id)
        assert done_task is not None and done_task.status == "done"
        assert done_task.current_run_id is None
        assert done_task.worker_pid is None
        assert done_run is not None
        assert done_run.status == "done"
        assert done_run.outcome == "completed"
        assert done_run.ended_at is not None
        assert done_run.reap_state == "finalized"
        assert done_run.reap_error in (None, "")
        assert done_run.reap_term_intent_at is None
        assert done_run.reap_kill_intent_at is None
        assert done_run.reap_term_sent_at is None
        assert done_run.reap_kill_sent_at is None
        final_events = kb.list_events(conn, task_id)
        assert sum(event.kind == "terminal_requested" for event in final_events) == 1
        assert sum(event.kind == "completed" for event in final_events) == 1

        signature = (
            done_run.reap_state,
            done_run.reap_error,
            done_run.reap_completed_at,
            tuple((event.id, event.kind, event.run_id) for event in final_events),
        )
        assert kb.reconcile_worker_reaps(conn, process_effects=True) == []
        idempotent_run = kb.get_run(conn, run_id)
        assert idempotent_run is not None
        assert (
            idempotent_run.reap_state,
            idempotent_run.reap_error,
            idempotent_run.reap_completed_at,
            tuple(
                (event.id, event.kind, event.run_id)
                for event in kb.list_events(conn, task_id)
            ),
        ) == signature
        assert kb._systemd_user_scope_state(
            unit,
            manager_target=target,
            systemctl=systemctl,
        ) == "not-found"
    finally:
        if ready_write is not None:
            os.close(ready_write)
        if ready_read is not None:
            os.close(ready_read)
        if unit is not None:
            # The exact disposable unit may exist even if the pre-exec
            # acknowledgement path failed. Always attempt whole-unit cleanup;
            # never turn an acknowledgement regression into a capability skip.
            kb._stop_systemd_user_scope(
                unit,
                manager_target=target,
                systemctl=systemctl,
            )
        if proc is not None and proc.poll() is None:
            _signal_original_process_group(
                proc.pid,
                leader_identity,
                signal.SIGKILL,
            )
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
        if child_pid is not None:
            _signal_original_process(child_pid, child_identity, signal.SIGKILL)
        conn.close()
