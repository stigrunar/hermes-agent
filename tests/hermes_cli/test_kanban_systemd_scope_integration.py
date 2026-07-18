"""Opt-in native proof for Kanban's transient user-scope lifecycle contract."""

from __future__ import annotations

import os
import select
import secrets
import shutil
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


def _unit_state(systemctl: str, unit: str) -> tuple[str, str]:
    result = subprocess.run(
        [
            systemctl,
            "--user",
            "show",
            unit,
            "--property=LoadState",
            "--property=ActiveState",
            "--no-pager",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=2.0,
        check=False,
    )
    properties = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line
    )
    return properties.get("LoadState", ""), properties.get("ActiveState", "")


def test_native_user_scope_exec_pid_descendants_stop_and_collect(monkeypatch):
    """Prove the real same-PID and unit-wide lifecycle behavior when opted in."""
    runtime_dir = Path("/run/user") / str(os.getuid())
    if not (runtime_dir / "bus").exists():
        pytest.skip("systemd user-manager bus is unavailable")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv(
        "DBUS_SESSION_BUS_ADDRESS",
        f"unix:path={runtime_dir / 'bus'}",
    )
    systemd_run = shutil.which("systemd-run")
    systemctl = shutil.which("systemctl")
    bash = shutil.which("bash")
    if not systemd_run or not systemctl or not bash:
        pytest.skip("systemd-run, systemctl, and bash are required")
    if not kb._systemd_user_scope_available(
        systemd_run,
        systemctl=systemctl,
    ):
        pytest.skip("reachable systemd user manager with --collect is required")

    unit = f"hermes-kanban-test-{secrets.token_hex(12)}.scope"
    ready_read, ready_write = os.pipe()
    payload = [
        bash,
        "-c",
        'sleep 300 & child=$!; printf "%s %s\\n" "$$" "$child"; wait',
    ]
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
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=(ready_write,),
        start_new_session=True,
    )
    os.close(ready_write)
    child_pid = None
    try:
        kb._await_scope_exec_ack(proc, ready_read, timeout=5.0)
        assert proc.stdout is not None
        readable, _, _ = select.select([proc.stdout], [], [], 2.0)
        assert readable, "payload did not report leader/child PIDs"
        leader_text, child_text = proc.stdout.readline().strip().split()
        leader_pid = int(leader_text)
        child_pid = int(child_text)
        assert proc.pid == leader_pid

        leader_cgroup = Path(f"/proc/{leader_pid}/cgroup").read_text()
        child_cgroup = Path(f"/proc/{child_pid}/cgroup").read_text()
        assert unit in leader_cgroup
        assert unit in child_cgroup
        assert _unit_state(systemctl, unit) == ("loaded", "active")

        subprocess.run(
            [systemctl, "--user", "stop", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=7.0,
            check=True,
        )
        proc.wait(timeout=2.0)
        assert not Path(f"/proc/{leader_pid}").exists()
        assert not Path(f"/proc/{child_pid}").exists()

        deadline = time.monotonic() + 2.0
        state = _unit_state(systemctl, unit)
        while state[0] != "not-found" and time.monotonic() < deadline:
            time.sleep(0.05)
            state = _unit_state(systemctl, unit)
        assert state == ("not-found", "inactive")
    finally:
        os.close(ready_read)
        subprocess.run(
            [systemctl, "--user", "stop", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=7.0,
            check=False,
        )
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)
        if child_pid is not None and Path(f"/proc/{child_pid}").exists():
            pytest.fail(f"disposable scope child {child_pid} survived cleanup")
