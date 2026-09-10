"""Authenticated worker scope lifecycle helpers.

This sibling keeps native systemd scope identity and exact cgroup reaping
out of the dispatcher facade. Calls into the canonical facade are late-bound
through _kb so existing monkeypatch seams remain valid.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from hermes_cli import kanban_db as _kb


def _record_spawn_failure(
    conn: sqlite3.Connection, task_id: str, error: str, *, failure_limit: Optional[int] = None,
) -> bool:
    """Apply the dispatcher failure accounting after a launch cleanup."""
    return _kb._record_task_failure(
        conn, task_id, error, outcome="spawn_failed", failure_limit=failure_limit,
        release_claim=True, end_run=True,
    )


def _set_worker_launching(
    conn: sqlite3.Connection, task_id: str, *, scope_unit: str,
    target: "_SystemdUserManagerTarget", scope_config: "_WorkerScopeConfig",
) -> None:
    """Fence one validated native scope before Popen can start its worker."""
    with _kb.write_txn(conn):
        row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        run_id = int(row["current_run_id"]) if row and row["current_run_id"] else None
        try:
            db_row = next(
                item for item in conn.execute("PRAGMA database_list").fetchall()
                if item[1] == "main"
            )
            expected_unit = _kb._systemd_scope_unit_name(
                task_id, int(run_id), db_path=db_row[2],
            )
        except (OSError, StopIteration, TypeError, ValueError, IndexError):
            expected_unit = None
        valid = (
            row is not None and row["status"] == "running" and run_id is not None
            and scope_unit == expected_unit
            and _SYSTEMD_WORKER_SCOPE_RE.fullmatch(scope_unit)
            and _kb._systemd_user_manager_target_for_uid(target.uid) is not None
            and _valid_scope_resource_receipt(
                scope_slice=scope_config.slice,
                memory_high=scope_config.memory_high,
                memory_max=scope_config.memory_max,
                memory_swap_max=scope_config.memory_swap_max,
                tasks_max=scope_config.tasks_max,
                oom_policy=scope_config.oom_policy,
                control_group="/launching",
            )
        )
        if not valid:
            raise RuntimeError("refusing to persist an invalid worker launch identity")
        cur = conn.execute(
            "UPDATE task_runs SET launch_mode='systemd-user-scope', scope_unit=?, "
            "manager_kind=?, manager_uid=?, launch_acknowledged=0, "
            "verification_status='launching', scope_slice=?, memory_high=?, "
            "memory_max=?, memory_swap_max=?, tasks_max=?, oom_policy=?, "
            "control_group=NULL, reap_state='launching', reap_error=NULL "
            "WHERE id=? AND task_id=? AND ended_at IS NULL "
            "AND launch_mode IS NULL AND verification_status IS NULL",
            (
                scope_unit, _SYSTEMD_USER_MANAGER_KIND, target.uid,
                scope_config.slice, scope_config.memory_high, scope_config.memory_max,
                scope_config.memory_swap_max, scope_config.tasks_max,
                scope_config.oom_policy, run_id, task_id,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError("worker launch identity is already resolved")
        _kb._append_event(
            conn, task_id, "launching",
            {"scope_unit": scope_unit, "manager_uid": target.uid}, run_id=run_id,
        )


def _clear_worker_launching(conn: sqlite3.Connection, task_id: str) -> None:
    """Clear a pre-Popen intent only when no scoped child was created."""
    with _kb.write_txn(conn):
        run_id = _kb._current_run_id(conn, task_id)
        cur = conn.execute(
            "UPDATE task_runs SET launch_mode=NULL, scope_unit=NULL, "
            "manager_kind=NULL, manager_uid=NULL, launch_acknowledged=NULL, "
            "verification_status=NULL, scope_slice=NULL, memory_high=NULL, "
            "memory_max=NULL, memory_swap_max=NULL, tasks_max=NULL, "
            "oom_policy=NULL, control_group=NULL, reap_state=NULL, reap_error=NULL "
            "WHERE id=? AND task_id=? AND ended_at IS NULL "
            "AND verification_status='launching' AND worker_pid IS NULL",
            (run_id, task_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("worker launch intent could not be cleared")

_SYSTEMD_WORKER_SCOPE_PREFIX = "hermes-kanban-worker-"
# OOMPolicy= for scopes was added after the original --collect support. Do not
# advertise the resource-controlled path to managers too old to honor the
# properties we rely on. OOMPolicy=stop plus exact scope stop/reaping provides
# the worker-unit kill semantics; no separate MemoryOOMGroup property is used.
_SYSTEMD_SCOPE_MIN_VERSION = 243
_SYSTEMD_SCOPE_PROBE_TIMEOUT = 2.0
_SYSTEMD_SCOPE_VERIFY_TIMEOUT = 2.0
_SYSTEMD_SCOPE_CLEANUP_TIMEOUT = 2.0
_SYSTEMD_WORKER_SCOPE_RE = re.compile(
    rf"^{re.escape(_SYSTEMD_WORKER_SCOPE_PREFIX)}[0-9a-f]{{32}}\.scope$"
)
_SYSTEMD_WORKER_SLICE_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,126}[A-Za-z0-9])?\.slice$"
)
_SYSTEMD_RESOURCE_VALUE_RE = re.compile(r"^[1-9][0-9]*(?:[KMGTPE](?:i?B)?)?$")
_SYSTEMD_USER_MANAGER_KIND = "systemd-user"

_SYSTEMD_RESOURCE_MULTIPLIERS = {
    "": 1,
    "K": 1000,
    "KB": 1000,
    "KiB": 1024,
    "M": 1000**2,
    "MB": 1000**2,
    "MiB": 1024**2,
    "G": 1000**3,
    "GB": 1000**3,
    "GiB": 1024**3,
    "T": 1000**4,
    "TB": 1000**4,
    "TiB": 1024**4,
    "P": 1000**5,
    "PB": 1000**5,
    "PiB": 1024**5,
    "E": 1000**6,
    "EB": 1000**6,
    "EiB": 1024**6,
}


def _systemd_resource_value_bytes(value: object) -> Optional[int]:
    """Parse one finite systemd resource value for receipt validation."""
    if not isinstance(value, str) or not _kb._SYSTEMD_RESOURCE_VALUE_RE.fullmatch(value):
        return None
    match = re.fullmatch(r"([1-9][0-9]*)(.*)", value)
    if match is None:
        return None
    multiplier = _SYSTEMD_RESOURCE_MULTIPLIERS.get(match.group(2))
    if multiplier is None:
        return None
    return int(match.group(1)) * multiplier


def _valid_scope_resource_receipt(
    *,
    scope_slice: object,
    memory_high: object,
    memory_max: object,
    memory_swap_max: object,
    tasks_max: object,
    oom_policy: object,
    control_group: object,
) -> bool:
    """Validate the complete finite resource receipt of a scoped worker."""
    high = _systemd_resource_value_bytes(memory_high)
    maximum = _systemd_resource_value_bytes(memory_max)
    return bool(
        isinstance(scope_slice, str)
        and _kb._SYSTEMD_WORKER_SLICE_RE.fullmatch(scope_slice)
        and high is not None
        and maximum is not None
        and _systemd_resource_value_bytes(memory_swap_max) is not None
        and high <= maximum
        and type(tasks_max) is int
        and 1 <= tasks_max <= 1_000_000
        and isinstance(oom_policy, str)
        and oom_policy in {"stop", "kill"}
        and isinstance(control_group, str)
        and control_group.startswith("/")
        and control_group != "/"
        and ".." not in Path(control_group).parts
    )


@dataclass(frozen=True)
class _WorkerScopeConfig:
    enabled: bool
    required: bool
    slice: str
    memory_high: str
    memory_max: str
    memory_swap_max: str
    tasks_max: int
    oom_policy: str


def _worker_scope_config(kanban_cfg: Optional[dict] = None) -> _WorkerScopeConfig:
    """Resolve and strictly validate ``kanban.worker_scope``.

    The defaults preserve the candidate's supported-host auto-isolation while
    giving every scoped worker an explicit finite budget. Unsupported hosts may
    still launch directly when dispatch is effectively serial. Any supplied
    malformed value raises before argv construction; config text is never
    copied into a systemd argument without passing these allowlists.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            loaded = load_config() or {}
            kanban_cfg = loaded.get("kanban", {}) if isinstance(loaded, dict) else {}
        except Exception:
            kanban_cfg = {}
    if not isinstance(kanban_cfg, dict):
        raise ValueError("kanban config must be a mapping")
    raw = kanban_cfg.get("worker_scope", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("kanban.worker_scope must be a mapping")

    enabled = raw.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("kanban.worker_scope.enabled must be a boolean")
    required = raw.get("required", False)
    if type(required) is not bool:
        raise ValueError("kanban.worker_scope.required must be a boolean")
    slice_name = raw.get("slice", "hermes-kanban-workers.slice")
    if not isinstance(slice_name, str) or not _kb._SYSTEMD_WORKER_SLICE_RE.fullmatch(slice_name):
        raise ValueError("kanban.worker_scope.slice is not a valid slice unit")

    def resource(name: str, default: str) -> str:
        value = raw.get(name, default)
        if not isinstance(value, str) or not _kb._SYSTEMD_RESOURCE_VALUE_RE.fullmatch(value):
            raise ValueError(f"kanban.worker_scope.{name} is not a valid systemd size")
        return value

    tasks_max = raw.get("tasks_max", 512)
    if type(tasks_max) is not int or not 1 <= tasks_max <= 1_000_000:
        raise ValueError("kanban.worker_scope.tasks_max must be an integer from 1 to 1000000")
    oom_policy = raw.get("oom_policy", "stop")
    if not isinstance(oom_policy, str) or oom_policy not in {"stop", "kill"}:
        raise ValueError("kanban.worker_scope.oom_policy must be 'stop' or 'kill'")
    resolved = _WorkerScopeConfig(
        enabled=enabled,
        required=required,
        slice=slice_name,
        memory_high=resource("memory_high", "2G"),
        memory_max=resource("memory_max", "3G"),
        memory_swap_max=resource("memory_swap_max", "512M"),
        tasks_max=tasks_max,
        oom_policy=oom_policy,
    )
    multipliers = {
        "": 1,
        "K": 1000,
        "KB": 1000,
        "KiB": 1024,
        "M": 1000**2,
        "MB": 1000**2,
        "MiB": 1024**2,
        "G": 1000**3,
        "GB": 1000**3,
        "GiB": 1024**3,
        "T": 1000**4,
        "TB": 1000**4,
        "TiB": 1024**4,
        "P": 1000**5,
        "PB": 1000**5,
        "PiB": 1024**5,
        "E": 1000**6,
        "EB": 1000**6,
        "EiB": 1024**6,
    }

    def bytes_value(value: str) -> int:
        match = re.fullmatch(r"([1-9][0-9]*)(.*)", value)
        assert match is not None  # syntax was validated above
        return int(match.group(1)) * multipliers[match.group(2)]

    if bytes_value(resolved.memory_high) > bytes_value(resolved.memory_max):
        raise ValueError("kanban.worker_scope.memory_high must not exceed memory_max")
    return resolved


@dataclass(frozen=True)
class _SystemdUserManagerTarget:
    """Authenticated identity of the current process's user manager."""

    uid: int
    runtime_dir: Path = field(repr=False)
    bus_path: Path = field(repr=False)


class _WorkerLaunchPid(int):
    """Integer-compatible PID with a verified launch receipt."""

    def __new__(
        cls,
        pid: int,
        *,
        launch_mode: str = "direct",
        scope_unit: Optional[str] = None,
        verification_status: str = "not-applicable",
        manager_kind: Optional[str] = None,
        manager_uid: Optional[int] = None,
        launch_acknowledged: Optional[bool] = None,
        scope_slice: Optional[str] = None,
        memory_high: Optional[str] = None,
        memory_max: Optional[str] = None,
        memory_swap_max: Optional[str] = None,
        tasks_max: Optional[int] = None,
        oom_policy: Optional[str] = None,
        control_group: Optional[str] = None,
    ):
        value = int.__new__(cls, int(pid))
        value.launch_mode = launch_mode
        value.scope_unit = scope_unit
        value.verification_status = verification_status
        value.manager_kind = manager_kind
        value.manager_uid = manager_uid
        value.launch_acknowledged = launch_acknowledged
        value.scope_slice = scope_slice
        value.memory_high = memory_high
        value.memory_max = memory_max
        value.memory_swap_max = memory_swap_max
        value.tasks_max = tasks_max
        value.oom_policy = oom_policy
        value.control_group = control_group
        return value


class _WorkerScopeLaunchError(RuntimeError):
    """A post-Popen scope failure carrying the exact durable identity."""

    def __init__(self, message: str, launch: _WorkerLaunchPid):
        super().__init__(message)
        self.launch = launch


class _VerifiedWorkerPid(int):
    """Integer-compatible PID carrying the manager-observed cgroup path."""

    def __new__(cls, pid: int, *, control_group: str):
        value = int.__new__(cls, int(pid))
        value.control_group = control_group
        return value


def _systemd_user_manager_environment(
    target: _SystemdUserManagerTarget,
) -> dict[str, str]:
    return {
        "XDG_RUNTIME_DIR": str(target.runtime_dir),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={target.bus_path}",
    }


def _systemd_user_manager_target_for_cgroup(
    cgroup_path: Optional[str],
) -> Optional[_SystemdUserManagerTarget]:
    """Return a trusted same-UID user bus only for a user-service cgroup."""
    if not sys.platform.startswith("linux") or not cgroup_path:
        return None
    path = cgroup_path.rstrip("/")
    match = re.search(r"/user@(\d+)\.service(?:/|$)", path)
    if (
        not match
        or not path.rsplit("/", 1)[-1].endswith(".service")
        or not re.search(r"/user@\d+\.service/.+\.service$", path)
    ):
        return None
    getuid = getattr(os, "getuid", None)
    geteuid = getattr(os, "geteuid", None)
    if getuid is None or geteuid is None:
        return None
    uid = int(match.group(1))
    if uid != int(getuid()) or uid != int(geteuid()):
        return None
    runtime_dir = Path("/run/user") / str(uid)
    bus_path = runtime_dir / "bus"
    try:
        runtime_stat = os.lstat(runtime_dir)
        bus_stat = os.lstat(bus_path)
    except OSError:
        return None
    if (
        not stat.S_ISDIR(runtime_stat.st_mode)
        or runtime_stat.st_uid != uid
        or stat.S_IMODE(runtime_stat.st_mode) & 0o077
        or not stat.S_ISSOCK(bus_stat.st_mode)
        or bus_stat.st_uid != uid
    ):
        return None
    return _SystemdUserManagerTarget(uid, runtime_dir, bus_path)


def _systemd_user_manager_target_for_uid(uid: object) -> Optional[_SystemdUserManagerTarget]:
    """Re-authenticate a persisted manager UID against the current process."""
    if type(uid) is not int:
        return None
    return _kb._systemd_user_manager_target_for_cgroup(
        f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/hermes.service"
    )


def _current_cgroup_path() -> Optional[str]:
    if not sys.platform.startswith("linux"):
        return None
    try:
        for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
            hierarchy, controllers, path = line.split(":", 2)
            if hierarchy == "0" and not controllers:
                return path
            if "name=systemd" in controllers.split(","):
                return path
    except (OSError, ValueError):
        return None
    return None


def _systemd_run_version(
    runner: str,
    *,
    run_fn=None,
) -> Optional[int]:
    run = run_fn or subprocess.run
    try:
        result = run(
            [runner, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_SYSTEMD_SCOPE_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None
    match = re.search(r"\bsystemd\s+(\d+)\b", result.stdout or "")
    return int(match.group(1)) if result.returncode == 0 and match else None


def _systemd_user_manager_reachable(
    target: _SystemdUserManagerTarget,
    *,
    systemctl: Optional[str] = None,
    run_fn=None,
) -> bool:
    controller = systemctl or shutil.which("systemctl")
    if not controller:
        return False
    run = run_fn or subprocess.run
    try:
        result = run(
            [controller, "--user", "show", "--property=Version", "--value"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_SYSTEMD_SCOPE_PROBE_TIMEOUT,
            check=False,
            env=_systemd_user_manager_environment(target),
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return False
    return result.returncode == 0 and bool((result.stdout or "").strip())


def _systemd_scope_unit_name(
    task_id: str,
    run_id: int,
    *,
    board: Optional[str] = None,
    db_path: Optional[os.PathLike[str] | str] = None,
) -> str:
    """Derive a bounded unit name from canonical board DB, task, and run."""
    db_identity = os.path.normcase(
        str(
            (Path(db_path) if db_path is not None else _kb.kanban_db_path(board=board))
            .expanduser()
            .resolve(strict=False)
        )
    )
    digest = hashlib.blake2s(
        f"v1\0{db_identity}\0{task_id}\0{int(run_id)}".encode("utf-8"),
        digest_size=16,
    ).hexdigest()
    return f"{_kb._SYSTEMD_WORKER_SCOPE_PREFIX}{digest}.scope"


def _systemd_scope_preflight(
    *,
    require_scope: bool = False,
    force_probe: bool = False,
    kanban_cfg: Optional[dict] = None,
    scope_config: Optional[_WorkerScopeConfig] = None,
    cgroup_path: Optional[str] = None,
    manager_target: Optional[_SystemdUserManagerTarget] = None,
    systemd_run: Optional[str] = None,
    user_manager_ready: Optional[bool] = None,
) -> tuple[bool, str, Optional[_SystemdUserManagerTarget]]:
    """Check the scope capability without claiming or touching a task.

    This is deliberately separate from :func:`_systemd_scope_argv`: argv
    construction needs a claimed run id, while dispatch must reject a
    required-but-unavailable native launch before it claims a row or creates a
    workspace.  The result is read-only and may be used for both ready and
    review lanes.
    """
    config = (
        scope_config
        if scope_config is not None
        else _worker_scope_config(kanban_cfg)
    )
    if not (force_probe or require_scope or config.required):
        return True, "scope not required", None
    if not config.enabled:
        return False, "kanban.worker_scope.enabled is false", None
    if not sys.platform.startswith("linux"):
        return False, "host is not Linux", None
    current = cgroup_path if cgroup_path is not None else _current_cgroup_path()
    target = manager_target or _kb._systemd_user_manager_target_for_cgroup(current)
    runner = systemd_run or shutil.which("systemd-run")
    if target is None or runner is None:
        return False, "authenticated user manager or systemd-run is unavailable", target
    if user_manager_ready is None:
        version = _kb._systemd_run_version(runner)
        ready = (
            version is not None
            and version >= _SYSTEMD_SCOPE_MIN_VERSION
            and _kb._systemd_user_manager_reachable(target)
        )
    else:
        ready = bool(user_manager_ready)
    if not ready:
        return False, "user manager did not pass the capability probe", target
    return True, "verified systemd user manager capability", target


def _systemd_scope_argv(
    cmd: list[str],
    task: Task,
    *,
    board: Optional[str] = None,
    cgroup_path: Optional[str] = None,
    manager_target: Optional[_SystemdUserManagerTarget] = None,
    systemd_run: Optional[str] = None,
    user_manager_ready: Optional[bool] = None,
    kanban_cfg: Optional[dict] = None,
    scope_config: Optional[_WorkerScopeConfig] = None,
    require_scope: bool = False,
) -> tuple[list[str], Optional[str], Optional[_SystemdUserManagerTarget]]:
    """Return a resource-controlled scope or fail closed when parallel.

    Unknown concurrency keeps the ordinary serial-compatible behavior. The
    caller sets ``require_scope`` when observed in-flight work proves this
    board can be parallel. ``kanban.worker_scope.required`` is the host-level
    fail-closed switch for external multi-board dispatchers whose aggregate
    concurrency is not visible inside any one board database.
    """
    config = (
        scope_config
        if scope_config is not None
        else _worker_scope_config(kanban_cfg)
    )

    def unavailable(reason: str):
        if require_scope or config.required:
            raise RuntimeError(
                "Kanban dispatch requires a verified systemd user "
                f"worker scope ({reason})"
            )
        return cmd, None, None

    if not config.enabled:
        return unavailable("kanban.worker_scope.enabled is false")
    if not sys.platform.startswith("linux"):
        return unavailable("host is not Linux")
    if task.current_run_id is None:
        return unavailable("task has no active run identity")
    ready, reason, target = _kb._systemd_scope_preflight(
        require_scope=require_scope,
        force_probe=True,
        scope_config=config,
        cgroup_path=cgroup_path,
        manager_target=manager_target,
        systemd_run=systemd_run,
        user_manager_ready=user_manager_ready,
    )
    if not ready:
        return unavailable(reason)
    runner = systemd_run or shutil.which("systemd-run")
    if target is None or runner is None:
        return unavailable("authenticated user manager or systemd-run is unavailable")
    try:
        unit = _kb._systemd_scope_unit_name(task.id, int(task.current_run_id), board=board)
    except (OSError, TypeError, ValueError):
        return unavailable("opaque unit identity could not be derived")
    return (
        [
            runner,
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            f"--unit={unit}",
            f"--slice={config.slice}",
            f"--property=MemoryHigh={config.memory_high}",
            f"--property=MemoryMax={config.memory_max}",
            f"--property=MemorySwapMax={config.memory_swap_max}",
            f"--property=TasksMax={config.tasks_max}",
            f"--property=OOMPolicy={config.oom_policy}",
            "--",
            *cmd,
        ],
        unit,
        target,
    )


def _systemd_scope_properties(
    unit: str,
    target: _SystemdUserManagerTarget,
    *,
    systemctl: Optional[str] = None,
    run_fn=None,
) -> Optional[dict[str, str]]:
    if not _kb._SYSTEMD_WORKER_SCOPE_RE.fullmatch(unit):
        return None
    controller = systemctl or shutil.which("systemctl")
    if not controller:
        return None
    run = run_fn or subprocess.run
    try:
        result = run(
            [
                controller,
                "--user",
                "show",
                unit,
                "--property=LoadState",
                "--property=ActiveState",
                "--property=ControlGroup",
                "--no-pager",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_SYSTEMD_SCOPE_PROBE_TIMEOUT,
            check=False,
            env=_systemd_user_manager_environment(target),
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None
    properties = {
        key: value
        for key, value in (
            line.split("=", 1)
            for line in (result.stdout or "").splitlines()
            if "=" in line
        )
    }
    if result.returncode != 0 and properties.get("LoadState") != "not-found":
        return None
    return properties


def _systemd_scope_state(
    unit: str,
    target: _SystemdUserManagerTarget,
    *,
    systemctl: Optional[str] = None,
    run_fn=None,
) -> str:
    """Return active/inactive/not-found/unknown for one validated unit."""
    props = _kb._systemd_scope_properties(
        unit, target, systemctl=systemctl, run_fn=run_fn,
    )
    if props is None:
        return "unknown"
    if props.get("LoadState") == "not-found":
        return "not-found"
    state = props.get("ActiveState")
    if state in {"inactive", "failed"}:
        return "inactive"
    if state in {"active", "activating", "deactivating"}:
        return "active"
    return "unknown"


def _stop_systemd_scope(
    unit: str,
    target: _SystemdUserManagerTarget,
    *,
    systemctl: Optional[str] = None,
    run_fn=None,
) -> Optional[bool]:
    """Stop one authenticated scope and boundedly prove its boundary is gone."""
    if not isinstance(unit, str) or not _kb._SYSTEMD_WORKER_SCOPE_RE.fullmatch(unit):
        return None
    controller = systemctl or shutil.which("systemctl")
    if not controller:
        return None
    run = run_fn or subprocess.run
    try:
        run(
            [controller, "--user", "stop", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_kb._SYSTEMD_SCOPE_CLEANUP_TIMEOUT,
            check=False,
            env=_systemd_user_manager_environment(target),
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None
    deadline = time.monotonic() + _kb._SYSTEMD_SCOPE_CLEANUP_TIMEOUT
    while time.monotonic() < deadline:
        state = _kb._systemd_scope_state(
            unit, target, systemctl=controller, run_fn=run,
        )
        if state in {"inactive", "not-found"}:
            return True
        if state == "unknown":
            return None
        time.sleep(0.05)
    return False


class _WorkerScopeMode(str, Enum):
    """One authenticated launch identity classification for a task run."""

    DIRECT = "direct"
    UNTRACKED = "untracked"
    SCOPED = "scoped"
    LAUNCHING = "launching"
    INVALID = "invalid"


@dataclass(frozen=True)
class _WorkerScopeReceipt:
    """Immutable classification of one task-run launch receipt row."""

    mode: _WorkerScopeMode
    scope_unit: Optional[str] = None
    target: Optional[_SystemdUserManagerTarget] = None
    control_group: Optional[str] = None


def _persisted_worker_scope(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: Optional[int],
) -> _WorkerScopeReceipt:
    """Read and classify exactly one canonical persisted launch receipt."""
    if run_id is None:
        return _WorkerScopeReceipt(_WorkerScopeMode.UNTRACKED)
    row = conn.execute(
        "SELECT launch_mode, scope_unit, manager_kind, manager_uid, "
        "launch_acknowledged, verification_status, scope_slice, memory_high, "
        "memory_max, memory_swap_max, tasks_max, oom_policy, control_group "
        "FROM task_runs WHERE id=? AND task_id=?",
        (int(run_id), task_id),
    ).fetchone()
    if row is None:
        return _WorkerScopeReceipt(_WorkerScopeMode.INVALID)
    receipt_fields = (
        "launch_mode", "scope_unit", "manager_kind", "manager_uid",
        "launch_acknowledged", "verification_status", "scope_slice",
        "memory_high", "memory_max", "memory_swap_max", "tasks_max",
        "oom_policy", "control_group",
    )
    if all(
        row[name] is None for name in receipt_fields
    ):
        return _WorkerScopeReceipt(_WorkerScopeMode.UNTRACKED)
    if row["launch_mode"] == "direct":
        direct_valid = (
            row["scope_unit"] is None
            and row["manager_kind"] is None
            and row["manager_uid"] is None
            and row["launch_acknowledged"] is None
            and row["verification_status"] == "not-applicable"
            and all(
                row[name] is None
                for name in (
                    "scope_slice", "memory_high", "memory_max",
                    "memory_swap_max", "tasks_max", "oom_policy",
                    "control_group",
                )
            )
        )
        return _WorkerScopeReceipt(
            _WorkerScopeMode.DIRECT if direct_valid else _WorkerScopeMode.INVALID
        )
    identity_invalid = (
        row["launch_mode"] != "systemd-user-scope"
        or row["manager_kind"] != _kb._SYSTEMD_USER_MANAGER_KIND
    )
    if identity_invalid:
        return _WorkerScopeReceipt(_WorkerScopeMode.INVALID)
    launching = (
        row["launch_acknowledged"] == 0
        and row["verification_status"] == "launching"
    )
    resource_fields_valid = _kb._valid_scope_resource_receipt(
        scope_slice=row["scope_slice"],
        memory_high=row["memory_high"],
        memory_max=row["memory_max"],
        memory_swap_max=row["memory_swap_max"],
        tasks_max=row["tasks_max"],
        oom_policy=row["oom_policy"],
        control_group=(
            "/launching"
            if launching and row["control_group"] is None
            else row["control_group"]
        ),
    )
    if not resource_fields_valid:
        return _WorkerScopeReceipt(_WorkerScopeMode.INVALID)
    target = _kb._systemd_user_manager_target_for_uid(row["manager_uid"])
    if target is None:
        return _WorkerScopeReceipt(_WorkerScopeMode.INVALID)
    try:
        db_row = next(
            item for item in conn.execute("PRAGMA database_list").fetchall()
            if item[1] == "main"
        )
        expected = _kb._systemd_scope_unit_name(
            task_id, int(run_id), db_path=db_row[2],
        )
    except (OSError, StopIteration, TypeError, ValueError, IndexError):
        return _WorkerScopeReceipt(_WorkerScopeMode.INVALID)
    unit = row["scope_unit"]
    if unit != expected or not _kb._SYSTEMD_WORKER_SCOPE_RE.fullmatch(unit or ""):
        return _WorkerScopeReceipt(_WorkerScopeMode.INVALID)
    if launching:
        return _WorkerScopeReceipt(
            _WorkerScopeMode.LAUNCHING, unit, target, row["control_group"],
        )
    if (
        row["launch_acknowledged"] != 1
        or row["verification_status"] != "verified"
        or row["control_group"] is None
    ):
        return _WorkerScopeReceipt(_WorkerScopeMode.INVALID)
    return _WorkerScopeReceipt(
        _WorkerScopeMode.SCOPED, unit, target, row["control_group"],
    )


@dataclass(frozen=True)
class _WorkerScopeRelease:
    """Result of classifying one run and, when scoped, reaping its boundary."""

    receipt: _WorkerScopeReceipt
    cleanup: str

    @property
    def mode(self) -> _WorkerScopeMode:
        return self.receipt.mode

    @property
    def scope_unit(self) -> Optional[str]:
        return self.receipt.scope_unit

    @property
    def can_release(self) -> bool:
        return self.mode in {
            _WorkerScopeMode.DIRECT,
            _WorkerScopeMode.UNTRACKED,
        } or (
            self.mode is _WorkerScopeMode.SCOPED
            and self.cleanup == "confirmed"
        )

    @property
    def pid_signal_allowed(self) -> bool:
        return self.mode in {
            _WorkerScopeMode.DIRECT,
            _WorkerScopeMode.UNTRACKED,
        }


def _scope_release_result(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: Optional[int],
) -> _WorkerScopeRelease:
    """Classify one persisted run and perform exact scope cleanup once.

    The returned classification is the authority for both release and PID
    signaling. In particular, a confirmed scoped cleanup is releasable but
    must *not* signal the worker PID; an invalid/unknown receipt remains
    fenced and must not release or signal anything.
    """
    receipt = _persisted_worker_scope(conn, task_id, run_id)
    return _scope_release_result_for_receipt(receipt)


def _worker_scope_runtime_status(receipt: _WorkerScopeReceipt) -> str:
    """Classify one immutable scoped receipt against its exact live boundary.

    ``active`` includes an inactive manager unit whose cgroup still contains
    processes: the leader PID may have exited while descendants continue to
    run. ``unknown`` is deliberately distinct from absence and remains
    fenced. Launching receipts cannot prove an inactive unit is empty without
    a persisted cgroup path, so they also fail closed in that case.
    """
    if receipt.mode not in {
        _WorkerScopeMode.SCOPED,
        _WorkerScopeMode.LAUNCHING,
    } or receipt.scope_unit is None or receipt.target is None:
        return "unknown"
    state = _kb._systemd_scope_state(receipt.scope_unit, receipt.target)
    if state == "not-found":
        return "absent"
    if state == "active":
        process_ids = _kb._systemd_scope_process_ids(receipt.control_group)
        if process_ids is None:
            return "unknown"
        if process_ids:
            return "active"
        # The manager owns the exact unit and its cgroup is empty. Keep the
        # existing bounded stop-and-confirm cleanup; the final manager/cgroup
        # absence check remains authoritative.
        return "reapable"
    if state == "inactive":
        process_ids = _kb._systemd_scope_process_ids(receipt.control_group)
        if process_ids == ():
            return "absent"
        if process_ids:
            return "active"
        if process_ids is None:
            return "unknown"
    return "unknown"


def _scope_release_result_for_receipt(
    receipt: _WorkerScopeReceipt,
    *,
    observed_status: Optional[str] = None,
) -> _WorkerScopeRelease:
    """Reap/describe a previously classified receipt without re-reading DB.

    Callers that already classified a receipt pass ``observed_status`` so a
    recycled host PID cannot race a second database lookup and change the
    identity being acted on. Paths without a prior snapshot classify the
    exact live boundary first and fail closed when membership is unknown.
    """
    mode = receipt.mode
    if mode in {
        _WorkerScopeMode.DIRECT,
        _WorkerScopeMode.UNTRACKED,
    }:
        return _WorkerScopeRelease(receipt, "not_required")
    unit = receipt.scope_unit
    target = receipt.target
    control_group = receipt.control_group
    if mode is not _WorkerScopeMode.SCOPED or unit is None or target is None:
        return _WorkerScopeRelease(receipt, "identity_invalid")

    if observed_status is None:
        observed_status = _worker_scope_runtime_status(receipt)
    if observed_status == "unknown":
        return _WorkerScopeRelease(receipt, "unknown")
    if observed_status == "absent":
        return _WorkerScopeRelease(receipt, "confirmed")
    state = _kb._systemd_scope_state(unit, target)
    if state == "not-found":
        return _WorkerScopeRelease(receipt, "confirmed")
    if state == "inactive":
        process_ids = _kb._systemd_scope_process_ids(control_group)
        if process_ids == ():
            return _WorkerScopeRelease(receipt, "confirmed")
        # Unknown membership still permits a bounded manager stop attempt;
        # the final absence observation below remains authoritative.
    elif state == "unknown":
        return _WorkerScopeRelease(receipt, "unknown")

    stopped = _kb._stop_systemd_scope(unit, target)
    if stopped is not True:
        return _WorkerScopeRelease(
            receipt, "unknown" if stopped is None else "failed",
        )
    # The manager stop result is not itself enough when the stop helper is
    # supplied by a test/provider. Take one strict absence observation before
    # allowing the DB identity to be released.
    final_state = _kb._systemd_scope_state(unit, target)
    if final_state == "not-found":
        return _WorkerScopeRelease(receipt, "confirmed")
    if final_state == "inactive":
        process_ids = _kb._systemd_scope_process_ids(control_group)
        if process_ids == ():
            return _WorkerScopeRelease(receipt, "confirmed")
        if process_ids is None:
            return _WorkerScopeRelease(receipt, "unknown")
    return _WorkerScopeRelease(
        receipt,
        "unknown" if final_state == "unknown" else "failed",
    )


def _termination_metadata_without_pid_signal(
    pid: Optional[int], release: _WorkerScopeRelease,
) -> dict[str, Any]:
    """Describe a confirmed scoped reap without pretending a PID was signaled."""
    return {
        "prev_pid": int(pid) if pid else None,
        "host_local": False,
        "termination_attempted": False,
        "terminated": True,
        "sigkill": False,
        "scope_reaped": True,
        "scope_unit": release.scope_unit,
    }


def _scope_absence_confirmed(
    unit: str,
    target: _SystemdUserManagerTarget,
    control_group: Optional[str],
) -> bool:
    """Return true only from a positive manager/cgroup absence observation."""
    state = _kb._systemd_scope_state(unit, target)
    if state == "not-found":
        return True
    if state != "inactive":
        return False
    return _kb._systemd_scope_process_ids(control_group) == ()


def _stop_persisted_scope_for_release(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: Optional[int],
) -> bool:
    """Prove a scoped boundary gone before any dispatcher terminal release."""
    return _scope_release_result(conn, task_id, run_id).can_release


def _prepare_task_scope_release(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    allowed_statuses: Optional[set[str]] = None,
    expected_run_id: Optional[int] = None,
) -> Optional[tuple[str, Optional[int]]]:
    """Stop an exact active scope before a task identity can be released.

    The returned ``(status, current_run_id)`` is a CAS snapshot for the
    caller's subsequent mutation. ``None`` means the task is missing, its
    status/run fence does not match, or exact scope cleanup was not proven.
    """
    row = conn.execute(
        "SELECT status, current_run_id FROM tasks WHERE id=?", (task_id,),
    ).fetchone()
    if row is None:
        return None
    status = str(row["status"])
    run_id = (
        int(row["current_run_id"])
        if row["current_run_id"] is not None else None
    )
    if allowed_statuses is not None and status not in allowed_statuses:
        return None
    if expected_run_id is not None and run_id != int(expected_run_id):
        return None
    if run_id is not None and not _stop_persisted_scope_for_release(
        conn, task_id, run_id,
    ):
        return None
    return status, run_id


def _request_scoped_terminal_transition(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    action: str,
    payload: dict[str, Any],
    expected_run_id: Optional[int],
) -> Optional[bool]:
    """Persist terminal intent for a scoped worker without stopping itself.

    ``None`` means the active run is direct/untracked and the legacy immediate
    transition may continue. ``True`` means the exact scoped run owns a durable
    phase-one request; the dispatcher must stop and confirm that boundary
    before applying the requested task transition.
    """
    row = conn.execute(
        "SELECT status, current_run_id FROM tasks WHERE id=?", (task_id,),
    ).fetchone()
    if row is None or row["status"] != "running" or row["current_run_id"] is None:
        return None
    run_id = int(row["current_run_id"])
    if expected_run_id is not None and run_id != int(expected_run_id):
        return False
    receipt = _persisted_worker_scope(conn, task_id, run_id)
    if receipt.mode in {_WorkerScopeMode.DIRECT, _WorkerScopeMode.UNTRACKED}:
        return None
    if (
        receipt.mode not in {
            _WorkerScopeMode.SCOPED,
            _WorkerScopeMode.LAUNCHING,
        }
        or receipt.scope_unit is None
        or receipt.target is None
    ):
        raise RuntimeError("scoped worker terminal transition has invalid launch identity")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    now = int(time.time())
    with _kb.write_txn(conn):
        existing = conn.execute(
            "SELECT terminal_action, terminal_payload, reap_state FROM task_runs "
            "WHERE id=? AND task_id=? AND ended_at IS NULL",
            (run_id, task_id),
        ).fetchone()
        if existing is None:
            return False
        if existing["reap_state"] == "reaped":
            return None
        if existing["terminal_action"] is not None:
            return bool(
                existing["terminal_action"] == action
                and existing["terminal_payload"] == encoded
            )
        cur = conn.execute(
            "UPDATE task_runs SET terminal_action=?, terminal_payload=?, "
            "reap_state='terminal_requested', reap_requested_at=?, reap_error=NULL "
            "WHERE id=? AND task_id=? AND ended_at IS NULL "
            "AND terminal_action IS NULL",
            (action, encoded, now, run_id, task_id),
        )
        if cur.rowcount != 1:
            return False
        _kb._append_event(
            conn,
            task_id,
            "terminal_requested",
            {"action": action, "scope_unit": receipt.scope_unit},
            run_id=run_id,
        )
    return True


def reconcile_worker_scope_terminals(conn: sqlite3.Connection) -> list[str]:
    """Stop, confirm, and finalize durable scoped-worker terminal requests."""
    finalized: list[str] = []
    pending_launches = conn.execute(
        "SELECT t.id AS task_id, t.current_run_id, r.scope_unit, r.manager_uid, "
        "r.control_group, r.reap_error FROM tasks t JOIN task_runs r "
        "ON r.id=t.current_run_id WHERE t.status='running' "
        "AND r.ended_at IS NULL AND r.reap_state='launch_cleanup_pending'"
    ).fetchall()
    for pending in pending_launches:
        task_id = pending["task_id"]
        run_id = int(pending["current_run_id"])
        receipt = _persisted_worker_scope(conn, task_id, run_id)
        unit = receipt.scope_unit
        target = receipt.target
        if (
            receipt.mode is not _WorkerScopeMode.LAUNCHING
            or unit is None
            or target is None
        ):
            continue
        if _worker_scope_runtime_status(receipt) == "unknown":
            continue
        state = _kb._systemd_scope_state(unit, target)
        absent = state == "not-found" or (
            state == "inactive"
            and _kb._systemd_scope_process_ids(receipt.control_group) == ()
        )
        if not absent and state in {"active", "inactive"}:
            absent = (
                _kb._stop_systemd_scope(unit, target) is True
                and _kb._scope_absence_confirmed(
                    unit, target, receipt.control_group,
                )
            )
        if absent:
            _record_spawn_failure(
                conn,
                task_id,
                str(pending["reap_error"] or "worker launch cleanup completed"),
            )

    rows = conn.execute(
        "SELECT t.id AS task_id, t.current_run_id, r.terminal_action, "
        "r.terminal_payload FROM tasks t JOIN task_runs r "
        "ON r.id=t.current_run_id WHERE t.status='running' "
        "AND r.ended_at IS NULL "
        "AND r.reap_state IN ('terminal_requested', 'reaped')"
    ).fetchall()
    for row in rows:
        task_id = row["task_id"]
        run_id = int(row["current_run_id"])
        receipt = _persisted_worker_scope(conn, task_id, run_id)
        unit = receipt.scope_unit
        target = receipt.target
        if (
            receipt.mode is not _WorkerScopeMode.SCOPED
            or unit is None
            or target is None
        ):
            with _kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET reap_error='scope_identity_invalid' "
                    "WHERE id=? AND reap_state='terminal_requested'",
                    (run_id,),
                )
            continue
        already_reaped = conn.execute(
            "SELECT reap_state FROM task_runs WHERE id=?", (run_id,),
        ).fetchone()["reap_state"] == "reaped"
        control_group = receipt.control_group

        if not already_reaped and _worker_scope_runtime_status(receipt) == "unknown":
            with _kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET reap_error=? "
                    "WHERE id=? AND reap_state IN ('terminal_requested', 'reaped')",
                    ("scope_unknown_stop_unconfirmed", run_id),
                )
            continue
        state = "inactive" if already_reaped else _kb._systemd_scope_state(unit, target)
        stopped = already_reaped or (
            state == "not-found"
            or state == "inactive" and _kb._systemd_scope_process_ids(control_group) == ()
        )
        if not stopped and state in {"active", "inactive", "not-found"}:
            stopped = (
                _kb._stop_systemd_scope(unit, target) is True
                and _kb._scope_absence_confirmed(unit, target, control_group)
            )
        if not stopped:
            with _kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET reap_error=? "
                    "WHERE id=? AND reap_state IN ('terminal_requested', 'reaped')",
                    (f"scope_{state}_stop_unconfirmed", run_id),
                )
            continue
        if not already_reaped:
            with _kb.write_txn(conn):
                cur = conn.execute(
                    "UPDATE task_runs SET reap_state='reaped', reap_completed_at=?, "
                    "reap_error=NULL WHERE id=? AND task_id=? AND ended_at IS NULL "
                    "AND reap_state='terminal_requested'",
                    (int(time.time()), run_id, task_id),
                )
            if cur.rowcount != 1:
                continue
        try:
            payload = json.loads(row["terminal_payload"] or "{}")
            if row["terminal_action"] == "complete":
                ok = _kb.complete_task(
                    conn,
                    task_id,
                    result=payload.get("result"),
                    summary=payload.get("summary"),
                    metadata=payload.get("metadata"),
                    created_cards=payload.get("created_cards"),
                    expected_run_id=run_id,
                    fire_lifecycle_hook=bool(payload.get("fire_lifecycle_hook", True)),
                )
            elif row["terminal_action"] == "block":
                ok = _kb.block_task(
                    conn,
                    task_id,
                    reason=payload.get("reason"),
                    kind=payload.get("kind"),
                    expected_run_id=run_id,
                )
            elif row["terminal_action"] == "iteration_exhausted":
                ok = _kb._finalize_iteration_exhaustion_immediately(
                    conn,
                    task_id,
                    budget_used=payload.get("budget_used", 0),
                    budget_max=payload.get("budget_max", 0),
                    error=str(payload.get("error") or "Iteration budget exhausted")[:500],
                    expected_run_id=run_id,
                ) == run_id
            elif row["terminal_action"] == "changes_requested":
                ok, _implementer = _kb.request_changes(
                    conn,
                    task_id,
                    reason=str(payload.get("reason") or "changes requested"),
                    expected_run_id=run_id,
                )
            elif row["terminal_action"] == "review_requested":
                ok = bool(
                    _kb.request_review(
                        conn,
                        task_id,
                        summary=payload.get("summary"),
                        metadata=payload.get("metadata"),
                        reviewer=payload.get("reviewer"),
                        expected_run_id=run_id,
                        _scope_finalizing=True,
                    )
                )
            else:
                ok = False
        except Exception as exc:
            ok = False
            with _kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET reap_error=? WHERE id=? AND ended_at IS NULL",
                    (f"terminal_finalize_failed:{type(exc).__name__}", run_id),
                )
        if ok:
            finalized.append(task_id)
        else:
            with _kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET reap_error=COALESCE(reap_error, "
                    "'terminal_finalize_conflict') WHERE id=? AND ended_at IS NULL",
                    (run_id,),
                )
    return finalized


def _process_cgroup_path(pid: int) -> Optional[str]:
    try:
        for line in Path(f"/proc/{int(pid)}/cgroup").read_text(encoding="utf-8").splitlines():
            hierarchy, controllers, path = line.split(":", 2)
            if hierarchy == "0" and not controllers:
                return path
            if "name=systemd" in controllers.split(","):
                return path
    except (OSError, ValueError):
        return None
    return None


def _systemd_scope_process_ids(
    control_group: Optional[str],
) -> Optional[tuple[int, ...]]:
    """Return read PIDs, or ``None`` when cgroup state cannot be proven."""
    if not isinstance(control_group, str):
        return None
    relative = control_group.strip().lstrip("/")
    if not relative or ".." in Path(relative).parts:
        return None
    roots = [Path("/sys/fs/cgroup")]
    if not (roots[0] / "cgroup.controllers").exists():
        roots.insert(0, roots[0] / "systemd")
    for root in roots:
        procs = root / relative / "cgroup.procs"
        try:
            return tuple(
                int(line)
                for line in procs.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return None
    return None


def _process_command_argv(pid: int) -> tuple[str, ...]:
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except OSError:
        return ()
    return tuple(
        part.decode("utf-8", errors="surrogateescape")
        for part in raw.rstrip(b"\0").split(b"\0")
        if part
    )


def _process_command_matches(pid: int, worker_cmd: list[str]) -> bool:
    """Match a direct exec or a shebang interpreter followed by worker argv."""
    observed = _kb._process_command_argv(pid)
    expected = tuple(worker_cmd)
    return bool(expected) and (
        observed == expected
        or (len(observed) > len(expected) and observed[-len(expected):] == expected)
    )


def _verify_systemd_scope_worker_pid(
    proc: Any,
    unit: str,
    target: _SystemdUserManagerTarget,
    worker_cmd: list[str],
) -> int:
    """Return the actual worker PID after the manager has attached and exec'd it."""
    deadline = time.monotonic() + _kb._SYSTEMD_SCOPE_VERIFY_TIMEOUT
    last_reason = "scope identity was not observable"
    while time.monotonic() < deadline:
        props = _kb._systemd_scope_properties(unit, target)
        if props is not None:
            load_state = props.get("LoadState")
            active_state = props.get("ActiveState")
            control_group = (props.get("ControlGroup") or "").rstrip("/")
            if load_state != "loaded":
                last_reason = f"scope unit load state was {load_state!r}"
            elif active_state not in {"active", "activating"}:
                last_reason = f"scope unit state was {active_state!r}"
            elif not control_group:
                last_reason = "scope has no control group"
            else:
                scope_pids = _kb._systemd_scope_process_ids(control_group)
                if scope_pids is None:
                    last_reason = "scope cgroup process membership was unreadable"
                    scope_pids = ()
                for pid in scope_pids:
                    process_group = (_kb._process_cgroup_path(pid) or "").rstrip("/")
                    if (
                        process_group == control_group
                        and _process_command_matches(pid, worker_cmd)
                    ):
                        return _VerifiedWorkerPid(pid, control_group=control_group)
                last_reason = (
                    f"scope PIDs {scope_pids!r} did not expose worker argv "
                    f"{tuple(worker_cmd)!r}"
                )
        if getattr(proc, "poll", lambda: None)() is not None:
            raise RuntimeError(f"systemd scope launcher exited before verification: {unit}")
        time.sleep(0.05)
    raise RuntimeError(f"could not verify systemd scope {unit}: {last_reason}")


def _cleanup_systemd_scope_launch(
    proc: subprocess.Popen,
    unit: str,
    target: _SystemdUserManagerTarget,
) -> None:
    """Stop exactly the transient unit and reap exactly its Popen wrapper."""
    _kb._stop_systemd_scope(unit, target)
    try:
        proc.wait(timeout=_kb._SYSTEMD_SCOPE_CLEANUP_TIMEOUT)
        return
    except (subprocess.TimeoutExpired, AttributeError):
        pass
    try:
        proc.terminate()
        proc.wait(timeout=_kb._SYSTEMD_SCOPE_CLEANUP_TIMEOUT)
    except (ProcessLookupError, OSError, subprocess.TimeoutExpired, AttributeError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError, AttributeError):
            return
        try:
            proc.wait(timeout=_kb._SYSTEMD_SCOPE_CLEANUP_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired, AttributeError):
            pass


def _abort_unpersisted_worker_launch(launch: object) -> bool:
    """Stop a verified scope and positively confirm its exact absence."""
    if getattr(launch, "launch_mode", None) != "systemd-user-scope":
        return True
    unit = getattr(launch, "scope_unit", None)
    target = _kb._systemd_user_manager_target_for_uid(
        getattr(launch, "manager_uid", None)
    )
    if not (
        isinstance(unit, str)
        and _kb._SYSTEMD_WORKER_SCOPE_RE.fullmatch(unit)
        and target is not None
    ):
        return False
    control_group = getattr(launch, "control_group", None)
    state = _kb._systemd_scope_state(unit, target)
    if state == "not-found" or (
        state == "inactive" and _kb._systemd_scope_process_ids(control_group) == ()
    ):
        return True
    if state not in {"active", "inactive"}:
        return False
    return (
        _kb._stop_systemd_scope(unit, target) is True
        and _kb._scope_absence_confirmed(unit, target, control_group)
    )


def _ensure_worker_launch_identity(
    conn: sqlite3.Connection,
    task_id: str,
    launch: object,
) -> None:
    """Backfill the launch fence for scoped custom spawn compatibility."""
    if getattr(launch, "launch_mode", None) != "systemd-user-scope":
        return
    run_id = _kb._current_run_id(conn, task_id)
    receipt = _persisted_worker_scope(conn, task_id, run_id)
    if receipt.mode is _WorkerScopeMode.LAUNCHING:
        return
    if receipt.mode is not _WorkerScopeMode.UNTRACKED:
        raise RuntimeError("worker scope launch identity conflicts with active run")
    target = _kb._systemd_user_manager_target_for_uid(
        getattr(launch, "manager_uid", None)
    )
    unit = getattr(launch, "scope_unit", None)
    if target is None or not isinstance(unit, str):
        raise RuntimeError("scoped worker launch has no authenticated identity")
    config = _WorkerScopeConfig(
        enabled=True,
        required=False,
        slice=getattr(launch, "scope_slice", None),
        memory_high=getattr(launch, "memory_high", None),
        memory_max=getattr(launch, "memory_max", None),
        memory_swap_max=getattr(launch, "memory_swap_max", None),
        tasks_max=getattr(launch, "tasks_max", None),
        oom_policy=getattr(launch, "oom_policy", None),
    )
    _set_worker_launching(
        conn,
        task_id,
        scope_unit=unit,
        target=target,
        scope_config=config,
    )


def _mark_worker_launch_cleanup_pending(
    conn: sqlite3.Connection,
    task_id: str,
    launch: object,
    error: str,
) -> None:
    """Keep a possibly live scope fenced for a later exact cleanup retry."""
    run_id = _kb._current_run_id(conn, task_id)
    if run_id is None:
        raise RuntimeError("cannot fence failed launch without an active run")
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE task_runs SET reap_state='launch_cleanup_pending', reap_error=?, "
            "control_group=COALESCE(control_group, ?) "
            "WHERE id=? AND task_id=? AND ended_at IS NULL "
            "AND launch_mode='systemd-user-scope'",
            (
                f"launch_persist_failed:{error}"[:500],
                getattr(launch, "control_group", None),
                run_id,
                task_id,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError("failed worker launch has no durable scope fence")
        _kb._append_event(
            conn,
            task_id,
            "launch_cleanup_pending",
            {
                "scope_unit": getattr(launch, "scope_unit", None),
                "error": error[:500],
            },
            run_id=run_id,
        )
