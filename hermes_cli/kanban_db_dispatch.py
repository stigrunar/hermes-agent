"""Dispatcher: crash/stale/orphan detection, failure accounting and the respawn circuit breaker, memory-aware concurrency caps, the one-shot ``dispatch_once`` pass, worker spawning (``_default_spawn``), worker-log rotation and the long-lived ``run_daemon`` loop.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterable
from typing import Mapping
from typing import Optional
from typing import TYPE_CHECKING
from types import MappingProxyType

from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task


# After this many consecutive non-success attempts on a task/profile the
# dispatcher parks the task in ``blocked`` with a reason — prevents retry storms.
DEFAULT_FAILURE_LIMIT = 2

# Worker log files larger than this at spawn time are rotated.
DEFAULT_LOG_ROTATE_BYTES = 2 * 1024 * 1024   # 2 MiB
DEFAULT_LOG_BACKUP_COUNT = 1

# Keep a little wall-clock budget for the worker to observe a terminal timeout
# and make a terminal board call (kanban_block/kanban_complete/kanban_request_review)
# before max_runtime_seconds kills it.
KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS = 30

# A healthy worker is still alive for a while after kanban_complete /
# kanban_request_review returns (final assistant turn, session persistence), so
# a run's retained worker is only reaped once ended_at is at least this old
# (two default dispatch ticks).
TERMINAL_WORKER_REAP_GRACE_SECONDS = 120

# ---------------------------------------------------------------------------
# Respawn guard constants
# ---------------------------------------------------------------------------

# Patterns in last_failure_error that indicate a quota / auth blocker.
# These errors won't resolve by retrying immediately — auto-block instead.
# The auth family is a curated list, not an open `auth\w*` stem: that stem
# also matched ordinary English words like "author"/"authored"/"authoring"/
# "authoritative" in worker progress prose, parking a healthy card forever
# (#117009).
_RESPAWN_BLOCKER_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit|429|403|"
    r"auth|authenticat(?:e|es|ed|ing|ion)|authoriz(?:e|es|ed|ing|ation)|"
    r"authoris(?:e|es|ed|ing|ation)|authz|"
    r"unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|"
    r"invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

# Within this window a completed run counts as "recent proof"; don't re-spawn.
_RESPAWN_GUARD_SUCCESS_WINDOW = 3600  # 1 hour

# Cooldown after a rate-limited (quota-wall) requeue before re-spawning. Without
# it the task would re-spawn on the very next tick and bounce off the same quota
# wall, burning a worker slot every tick for hours. Overridable via
# ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS``.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300  # 5 minutes

# Within this window a GitHub PR URL in a comment blocks re-spawn.
_RESPAWN_GUARD_PR_WINDOW = 86400  # 24 hours

_RESPAWN_GUARD_PR_URL_RE = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+",
    re.IGNORECASE,
)


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch`` pass.

    ``kanban.default_assignee`` applied this tick before spawning (#27145). Surfaces the auto-assignment to
    telemetry / CLI / dashboard so the operator can see when the dispatcher is acting on the fallback rule
    ``kanban.max_in_progress_per_profile`` (#21582). Each entry is ``(task_id, assignee,
    current_running_count)``. NOT an operator-actionable failure — the task will be picked up on a
    subsequent tick when the assignee has capacity. Separate bucket so telemetry / dashboards can show "this
    profile is busy" vs
    the board's dispatch lock (issue #35240). A losing dispatcher does no DB writes this tick — the lock
    holder is making progress on the same board. This is the steady-state signal that a single-writer guard
    is
    """

    reclaimed: int = 0
    promoted: int = 0
    reconciled_orphans: list[str] = field(default_factory=list)
    """``running`` cards requeued by :func:`reconcile_orphaned_running` (broken
    claim bookkeeping, dead/gone worker)."""
    reaped_terminal_workers: list[str] = field(default_factory=list)
    """Task ids whose worker outlived its closed run and was terminated by
    :func:`reap_terminal_workers`."""
    spawned: list[tuple[str, str, str]] = field(default_factory=list)
    """``(task_id, assignee, workspace_path)`` triples."""
    skipped_unassigned: list[str] = field(default_factory=list)
    """Ready task ids with no assignee at all — operator-actionable (usually a
    misfiled task waiting for routing)."""
    auto_assigned_default: list[str] = field(default_factory=list)
    """Unassigned task ids that had ``kanban.default_assignee`` applied this
    tick before spawning, so telemetry/CLI/dashboard can show the dispatcher
    acting on the fallback rule rather than explicit assignments."""
    skipped_nonspawnable: list[str] = field(default_factory=list)
    """Ready task ids whose assignee names a control-plane lane (e.g. a Claude
    Code terminal like ``orion-cc``), not a Hermes profile. Expected steady-state
    on multi-lane setups, NOT operator-actionable; tracked apart so health
    telemetry can tell "stuck" from "correctly idle"."""
    skipped_per_profile_capped: list[tuple[str, str, int]] = field(default_factory=list)
    """``(task_id, assignee, current_running_count)`` deferred because the
    assignee is at ``kanban.max_in_progress_per_profile``. Picked up on a later
    tick; separate bucket so dashboards show "profile busy" vs "stuck"."""
    crashed: list[str] = field(default_factory=list)
    """Task ids reclaimed because their worker PID disappeared."""
    auto_blocked: list[str] = field(default_factory=list)
    """Task ids auto-blocked by the spawn-failure circuit breaker."""
    timed_out: list[str] = field(default_factory=list)
    """Task ids whose workers exceeded ``max_runtime_seconds``."""
    stale: list[str] = field(default_factory=list)
    """Task ids reclaimed for no heartbeat within ``dispatch_stale_timeout_seconds``."""
    respawn_guarded: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, reason)`` skipped by the respawn guard: ``"blocker_auth"``
    (quota/auth error — also auto-blocked), ``"recent_success"`` (completed run
    within guard window), ``"active_pr"`` (GitHub PR URL in a recent comment)."""
    rate_limited: list[str] = field(default_factory=list)
    """Task ids whose workers bailed on a provider rate-limit / quota wall
    (EX_TEMPFAIL sentinel exit) and were released to ``ready`` WITHOUT counting
    a failure — a long quota window must never trip the circuit breaker."""
    skipped_locked: bool = False
    """True when another process held the board's dispatch lock: this tick did
    no DB writes; the lock holder is making progress on the same board."""
    memory_pressure: Optional[str] = None
    """Memory pressure that restricted this tick: ``"critical"`` (no new
    workers), ``"elevated"`` (at most one), ``None`` (no restriction).
    Reclaim/promotion bookkeeping still ran; deferred tasks stay queued."""
    capability_rejections: list[dict[str, Any]] = field(default_factory=list)
    """Stable pre-claim diagnostics for explicit worker capability misses."""
    admission_blocked: bool = False
    admission_reason: Optional[str] = None
    admission_metrics: dict[str, Any] = field(default_factory=dict)
    skipped_worker_profile_not_allowed: list[tuple[str, str]] = field(default_factory=list)
    skipped_worker_profile_not_allowed_total: int = 0
    skipped_worker_profile_not_allowed_truncated: bool = False
    remote_routed: list[dict[str, Any]] = field(default_factory=list)
    remote_deferred: list[tuple[str, str]] = field(default_factory=list)


def describe_suppression(results: Iterable[Optional["DispatchResult"]]) -> str:
    """One line naming why the tick(s) held ready work back, or ``""``.

    ``active_pr=1, recent_success=2, rate_limited=1, skipped_locked=1,
    memory_pressure=critical`` — the respawn-guard reasons counted per task
    plus the tick-level holds. Feeds the "dispatcher stuck" warnings of the
    CLI daemon and the embedded gateway dispatcher, which otherwise report a
    bare zero-spawn count while ``hermes kanban tail`` is the only place the
    guard reason is written (#111910).
    """
    counts: dict[str, int] = {}
    pressure: Optional[str] = None
    for res in results:
        if res is None:
            continue
        for _task_id, reason in res.respawn_guarded:
            counts[reason] = counts.get(reason, 0) + 1
        if res.rate_limited:
            counts["rate_limited"] = counts.get("rate_limited", 0) + len(res.rate_limited)
        if res.skipped_locked:
            counts["skipped_locked"] = counts.get("skipped_locked", 0) + 1
        if res.memory_pressure:
            pressure = res.memory_pressure
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    if pressure:
        parts.append(f"memory_pressure={pressure}")
    return ", ".join(parts)


# Bounded registry of recently-reaped worker exits, filled by the reap loop in
# ``dispatch_once`` and read by ``detect_crashed_workers`` to classify a dead-pid
# task. Entry: ``pid -> (raw_wait_status, reaped_at_epoch)``; raw status kept so
# both WIFEXITED/WEXITSTATUS and WIFSIGNALED can be consulted. Trimmed by age
# plus a total size cap. Process-local by nature (``waitpid`` only reaps our own
# children): a per-tick ``hermes kanban dispatch`` process finds it empty, so
# ``_classify_dead_worker_exit`` falls back to the exit trailer the worker
# leaves in its own log (``KANBAN_WORKER_EXIT_TRAILER``).
_RECENT_WORKER_EXIT_TTL_SECONDS = 600
_RECENT_WORKER_EXITS_MAX = 4096
_recent_worker_exits: "dict[int, tuple[int, float]]" = {}

# Windows has no ``waitpid(-1)``: a child's exit code is only recoverable
# through a live handle, so ``_default_spawn`` parks each worker's ``Popen``
# here (Windows only) and ``reap_worker_zombies`` polls it. Entry: ``pid -> Popen``.
_live_worker_procs: "dict[int, subprocess.Popen]" = {}


def _wait_status_from_returncode(returncode: int) -> int:
    """Encode a ``Popen.returncode`` in the wait-status layout the registry stores."""
    return (int(returncode) & 0xFF) << 8


def _record_worker_exit(pid: int, raw_status: int) -> None:
    """Record a reaped child's exit status; duplicate pids overwrite (latest wins)."""
    if not pid or pid <= 0:
        return
    now = time.time()
    _recent_worker_exits[int(pid)] = (int(raw_status), now)
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX // 2:
        cutoff = now - _RECENT_WORKER_EXIT_TTL_SECONDS
        for _pid in [p for p, (_s, t) in _recent_worker_exits.items() if t < cutoff]:
            _recent_worker_exits.pop(_pid, None)
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX:
        # Drop oldest half.
        ordered = sorted(_recent_worker_exits.items(), key=lambda kv: kv[1][1])
        for _pid, _ in ordered[: len(ordered) // 2]:
            _recent_worker_exits.pop(_pid, None)


def _classify_worker_exit(pid: int) -> "tuple[str, Optional[int]]":
    """``(kind, code)`` for a reaped worker PID: ``clean_exit`` (rc 0 while
    still ``running`` = protocol violation), ``rate_limited``
    (``KANBAN_RATE_LIMIT_EXIT_CODE``, never counts as a failure),
    ``nonzero_exit``, ``signaled`` (``code`` is the signal), ``unknown`` (pid
    not in the reap registry; ``code`` None)."""
    entry = _recent_worker_exits.get(int(pid))
    if entry is None:
        return ("unknown", None)
    raw, _ = entry
    # Bit-level POSIX wait-status decode instead of os.WIFEXITED/WEXITSTATUS/
    # WIFSIGNALED/WTERMSIG: those helpers do not exist on Windows, where the
    # registry is fed by reap_worker_zombies' Popen poll. Low 7 bits = signal
    # (0 = normal exit, 0x7F = stopped), bits 8-15 = exit code.
    raw = int(raw)
    signal_number = raw & 0x7F
    if signal_number == 0:
        return _exit_code_kind((raw >> 8) & 0xFF)
    if signal_number != 0x7F:
        return ("signaled", signal_number)
    return ("unknown", None)


def _exit_code_kind(code: int) -> "tuple[str, int]":
    """``(kind, code)`` for a worker's exit code, however it was observed."""
    if code == 0:
        return ("clean_exit", 0)
    if code == _kb.KANBAN_RATE_LIMIT_EXIT_CODE:
        return ("rate_limited", code)
    if code == _kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE:
        return ("terminal_provider", code)
    return ("nonzero_exit", code)


_EXIT_TRAILER_RE = re.compile(
    r"^" + re.escape(KANBAN_WORKER_EXIT_TRAILER) + r"(\d+)\s*$", re.MULTILINE,
)


def _worker_log_exit_code(task_id: str, board: Optional[str] = None) -> Optional[int]:
    """Exit code from the trailer the worker CLI wrote to its own log; None when absent.

    The durable twin of ``_recent_worker_exits``: written by the worker itself
    (``hermes_cli.quiet_single_query.exit_single_query``), so it is there whether
    or not the process running this sweep ever reaped the worker. Last trailer
    wins — the log is append-mode across re-runs.
    """
    try:
        raw = _kb.read_worker_log(task_id, tail_bytes=4000, board=board)
    except Exception:
        return None
    matches = _EXIT_TRAILER_RE.findall(raw or "")
    return int(matches[-1]) if matches else None


def _record_iteration_exhaustion(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    budget_used: int,
    budget_max: int,
    error: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> Optional[int]:
    """Record the accepted non-retryable iteration-exhaustion transition.

    Direct and legacy untracked workers keep the immediate terminal path.  A
    scoped worker first persists terminal intent; the scope reconciler then
    stops and proves the exact cgroup is absent before finalizing the run.
    """
    used = max(0, int(budget_used))
    maximum = max(0, int(budget_max))
    message = str(
        error
        or f"Iteration budget exhausted ({used}/{maximum}) — task could not complete within the allowed iterations"
    )[:500]
    row = conn.execute(
        "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return None
    run_id = int(row["current_run_id"]) if row["current_run_id"] else None
    if expected_run_id is not None:
        try:
            if run_id != int(expected_run_id):
                return None
        except (TypeError, ValueError):
            return None
    if row["status"] == "running" and run_id is not None:
        deferred = _kb._request_scoped_terminal_transition(
            conn,
            task_id,
            action="iteration_exhausted",
            payload={
                "budget_used": used,
                "budget_max": maximum,
                "error": message,
            },
            expected_run_id=run_id,
        )
        if deferred is True:
            return run_id
        if deferred is False:
            return None
    return _kb._finalize_iteration_exhaustion_immediately(
        conn,
        task_id,
        budget_used=used,
        budget_max=maximum,
        error=message,
        expected_run_id=expected_run_id,
    )


def reap_worker_zombies() -> "list[int]":
    """Reap exited workers without blocking; returns reaped PIDs. POSIX reaps
    every child via ``waitpid(-1)``; Windows polls the ``Popen`` handles
    parked by ``_default_spawn`` (the only way to learn a child's exit code
    there), so the rate-limit sentinel exit is classified on both hosts."""
    reaped: "list[int]" = []
    if _kb._IS_WINDOWS:
        for pid, proc in list(_live_worker_procs.items()):
            returncode = proc.poll()
            if returncode is None:
                continue
            _record_worker_exit(pid, _wait_status_from_returncode(returncode))
            _live_worker_procs.pop(pid, None)
            reaped.append(pid)
        return reaped
    try:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            _record_worker_exit(pid, status)
            reaped.append(pid)
    except Exception:
        pass
    return reaped


def _pid_alive(pid: Optional[int]) -> bool:
    """Return True if ``pid`` is still running on this host.

    Uses ``gateway.status._pid_exists`` (OpenProcess on Windows, ``os.kill(pid, 0)``
    on POSIX). **DO NOT** call ``os.kill(pid, 0)`` directly on Windows — there
    ``sig=0`` is ``CTRL_C_EVENT`` broadcast to the console group, potentially
    killing unrelated processes.

    Zombies (exited, not yet reaped) still pass the existence check, so a
    worker would look "alive" forever between exit and reap. Linux: peek at
    ``/proc/<pid>/status`` and treat ``State: Z`` as dead; macOS: ask ``ps``
    for the BSD ``stat`` field and treat ``Z`` as dead.
    """
    if not pid or pid <= 0:
        return False
    from gateway.status import _pid_exists
    if not _pid_exists(int(pid)):
        return False
    if sys.platform == "linux":
        try:
            with open(f"/proc/{int(pid)}/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        # "State:\tZ (zombie)" → dead
                        if "Z" in line.split(":", 1)[1]:
                            return False
                        break
        except (FileNotFoundError, PermissionError, OSError):
            # proc entry gone → already reaped; treat as dead.
            pass
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(int(pid))],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace',
                timeout=1,
                check=False,
            )
            if proc.returncode != 0:
                return False
            if "Z" in (proc.stdout or "").strip():
                return False
        except (OSError, subprocess.SubprocessError, TimeoutError):
            # If the secondary probe fails, keep the kill(0) answer.
            pass
    return True


# ``worker_started_at`` value for a spawn whose fingerprint could not be captured. Distinct from the
# NULL legacy row (pre-fingerprint spawn): such a worker is held (its claim is never released beside
# the live PID) but NEVER signalled — missing process identity is refusal, not permission (#99558).
UNVERIFIED_WORKER_FINGERPRINT = "unverified"


def _process_fingerprint(pid: int) -> Optional[str]:
    """Restart-stable identity of a live process: ``"<instantiation epoch>|<start time>"``. The start
    time alone (``/proc/<pid>/stat`` field 22 on Linux) is clock ticks since THIS boot, so a row that
    survives a reboot could match an unrelated process with the same PID and the same tick value;
    ``gateway.drain_control.current_instantiation_epoch`` (``boot_id`` + PID-1 start) changes on every
    reboot / container recreate, so the composed value never survives one. ``None`` when unreadable."""
    from gateway.drain_control import current_instantiation_epoch
    from gateway.status import get_process_start_time
    start = get_process_start_time(int(pid))
    if start is None:
        return None
    return f"{current_instantiation_epoch()}|{start}"


def _worker_alive(pid: Optional[int], started_at) -> bool:
    """True when ``pid`` is live AND is still the worker we spawned. ``started_at`` is the fingerprint
    recorded by ``_set_worker_pid``; after a reboot (or any PID recycle) an unrelated process can own
    the number, so bare existence is never enough to extend a claim or to signal. A legacy row without
    a fingerprint keeps the existence answer: killing it is the pre-fingerprint behaviour and the row is
    rewritten with a fingerprint on its next spawn. An UNVERIFIED spawn also keeps the existence answer
    (a claim is never released beside a possibly-live worker) but ``_terminate_reclaimed_worker``
    refuses to signal it."""
    if not _kb._pid_alive(pid):
        return False
    if started_at == UNVERIFIED_WORKER_FINGERPRINT:
        return True
    return not _pid_recycled(pid, started_at)


def _pid_recycled(pid: Optional[int], started_at) -> bool:
    """True when a live ``pid`` is NOT the process fingerprinted at spawn (or the fingerprint can no
    longer be read). Signalling it would hit a stranger. ``None`` fingerprint = legacy row, never
    recycled; the UNVERIFIED marker is always foreign. An integer fingerprint (rows written before the
    boot witness was added) compares the start time only."""
    if started_at is None or not pid:
        return False
    if started_at == UNVERIFIED_WORKER_FINGERPRINT:
        return True
    if isinstance(started_at, str) and "|" in started_at:
        return _process_fingerprint(int(pid)) != started_at
    from gateway.status import _start_times_agree, get_process_start_time
    current = get_process_start_time(int(pid))
    if current is None:
        return True
    try:
        return not _start_times_agree(current, started_at)
    except (TypeError, ValueError):
        return True


def _kill_fn(signal_fn) -> Optional[Callable[[int, int], None]]:
    """``signal_fn`` test hook, else ``os.kill`` when the platform has one."""
    if signal_fn is not None:
        return signal_fn
    return os.kill if hasattr(os, "kill") else None


def _poll_worker_exit(pid: int, started_at: Optional[int] = None) -> bool:
    """Poll ~5 s (10 x 0.5 s) for ``pid`` to die; True once it is gone."""
    for _ in range(10):
        if not _worker_alive(pid, started_at):
            return True
        time.sleep(0.5)
    return False


def _sigkill(kill, pid: int) -> bool:
    """Best-effort SIGKILL; True when the signal was delivered."""
    try:
        # signal.SIGKILL doesn't exist on Windows; SIGTERM maps to TerminateProcess.
        kill(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))
        return True
    except (ProcessLookupError, OSError):
        return False


def _terminate_reclaimed_worker(
    pid: Optional[int],
    claim_lock: Optional[str],
    *,
    signal_fn=None,
    started_at=None,
) -> dict[str, Any]:
    """Best-effort host-local worker termination for reclaim paths. ``started_at`` is the spawn-time
    fingerprint: when the live process no longer matches it, the PID was recycled and nothing is
    signalled — the worker is gone, which is what the reclaim wanted (``terminated`` = True). An
    UNVERIFIED spawn (fingerprint capture failed) that is still live is never signalled either, but
    it is reported as surviving (``signal_refused``) so the reclaim holds the claim instead of
    spawning a duplicate beside it."""
    info: dict[str, Any] = {
        "prev_pid": int(pid) if pid else None,
        "host_local": False,
        "termination_attempted": False,
        "terminated": False,
        "sigkill": False,
    }
    if not pid or pid <= 0 or not claim_lock:
        return info
    if not str(claim_lock).startswith(_kb._host_prefix()):
        return info
    info["host_local"] = True

    kill = _kill_fn(signal_fn)
    if kill is None:
        return info
    if started_at == UNVERIFIED_WORKER_FINGERPRINT:
        # Never signal by bare number: a dead PID is "gone" (reclaim proceeds), a live one is held.
        info["signal_refused"] = True
        info["terminated"] = not _kb._pid_alive(pid)
        return info
    if _kb._pid_alive(pid) and _pid_recycled(pid, started_at):
        info["terminated"] = True
        info["pid_recycled"] = True
        return info

    info["termination_attempted"] = True
    try:
        kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        # Already gone = successful termination. Leaving terminated=False would
        # make the reclaim guard misread a dead worker as alive and defer forever.
        info["terminated"] = True
        return info
    except OSError:
        return info

    if _poll_worker_exit(pid, started_at):
        info["terminated"] = True
        return info
    if _worker_alive(pid, started_at):
        if not _sigkill(kill, pid):
            return info
        info["sigkill"] = True
    info["terminated"] = not _worker_alive(pid, started_at)
    return info


def reap_terminal_workers(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """End host-local workers that outlived their run (issue #111791) — a worker
    that called ``kanban_complete`` and then hung keeps its ``state.db`` sidecar
    fds open and no ``running``-only sweep can see it once ``tasks.worker_pid`` is
    cleared. Keys on the closed ``task_runs`` row's retained pid + spawn
    fingerprint: a legacy row (NULL fingerprint) or a recycled PID is never
    signalled; a pid that is simply gone just has its evidence cleared. A run
    that ended less than ``TERMINAL_WORKER_REAP_GRACE_SECONDS`` ago is left
    alone so a worker still finalising after its own transition is not killed.
    One row's failure (signal, /proc probe) is logged and skips only that row.
    Returns the task ids whose worker was terminated."""
    rows = conn.execute(
        "SELECT id, task_id, worker_pid, worker_started_at, claim_lock FROM task_runs "
        "WHERE ended_at IS NOT NULL AND ended_at <= ? "
        "AND worker_pid IS NOT NULL AND worker_started_at IS NOT NULL",
        (int(time.time()) - TERMINAL_WORKER_REAP_GRACE_SECONDS,),
    ).fetchall()
    host_prefix = _kb._host_prefix()
    reaped: list[str] = []
    for row in rows:
        try:
            _reap_terminal_worker_row(conn, row, host_prefix, signal_fn, reaped)
        except Exception:
            _kb._log.debug(
                "kanban dispatch: terminal worker reap failed for run %s (task %s)",
                row["id"], row["task_id"], exc_info=True,
            )
    return reaped


def _reap_terminal_worker_row(conn, row, host_prefix: str, signal_fn, reaped: list[str]) -> None:
    pid, fingerprint = int(row["worker_pid"]), row["worker_started_at"]
    if pid == os.getpid() or not str(row["claim_lock"] or "").startswith(host_prefix):
        return
    scope_release = _kb._scope_release_result(conn, row["task_id"], int(row["id"]))
    if not scope_release.can_release:
        return
    if not scope_release.pid_signal_allowed:
        termination = _kb._termination_metadata_without_pid_signal(pid, scope_release)
        with _kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET worker_pid = NULL, worker_started_at = NULL "
                "WHERE id = ? AND worker_pid = ? AND worker_started_at = ?",
                (row["id"], pid, fingerprint),
            )
            _kb._append_event(
                conn,
                row["task_id"],
                "terminal_worker_reaped",
                {"pid": pid, "worker_started_at": fingerprint, **termination},
                run_id=row["id"],
            )
        reaped.append(row["task_id"])
        return
    if fingerprint == UNVERIFIED_WORKER_FINGERPRINT and _kb._pid_alive(pid):
        return  # unproven identity: never signalled; its evidence is cleared once the pid is gone
    alive = _worker_alive(pid, fingerprint)
    termination = None
    if alive:
        termination = _terminate_reclaimed_worker(
            pid, row["claim_lock"], signal_fn=signal_fn, started_at=fingerprint)
        if not termination["terminated"]:
            return  # still alive: try again next tick
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND worker_pid = ? AND worker_started_at = ?",
            (row["id"], pid, fingerprint),
        )
        if alive:
            _kb._append_event(
                conn, row["task_id"], "terminal_worker_reaped",
                {"pid": pid, "worker_started_at": fingerprint, **termination}, run_id=row["id"],
            )
    if alive:
        reaped.append(row["task_id"])


def _worker_survived_termination(termination: dict) -> bool:
    """True when we tried to kill our own host-local worker and it is still alive.

    Reclaiming then would release the claim and spawn a second worker while the
    first still runs — the duplication loop. Only host-local workers we actually
    signalled count; a non-local lock or no-op attempt (no ``os.kill``) must fall
    through to the normal release path since we cannot manage that worker anyway.
    """
    return bool(
        termination.get("host_local")
        and (termination.get("termination_attempted") or termination.get("signal_refused"))
        and not termination.get("terminated")
    )


def _defer_reclaim_for_live_worker(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    now: int,
    termination: dict,
    *,
    reason: str,
) -> None:
    """Hold a claim whose worker survived termination instead of releasing it.

    Extends ``claim_expires`` by ``RECLAIM_DEFER_GRACE_SECONDS`` so the task
    stays ``running`` (no duplicate spawn) and records ``reclaim_deferred``.
    The next tick retries the kill; not spawning a duplicate is what lets the
    throttled worker finally die.
    """
    grace = now + _kb.RECLAIM_DEFER_GRACE_SECONDS
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock IS ?",
            (grace, task_id, claim_lock),
        )
        if cur.rowcount != 1:
            return
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (grace, run_id))
        payload = {"reason": reason, "claim_lock": claim_lock, "claim_expires_now": grace}
        payload.update(termination)
        _kb._append_event(conn, task_id, "reclaim_deferred", payload, run_id=run_id)


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Record a ``heartbeat`` event + touch ``last_heartbeat_at``.

    Liveness signal orthogonal to the PID check: a worker whose forked child
    (train loop, crawl) is stuck can still have a live Python process.
    Returns False if the task is not running or its claim expired.
    """
    now = int(time.time())
    with _kb.write_txn(conn):
        sql = "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ? AND status = 'running'"
        params: tuple = (now, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params += (int(expected_run_id),)
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = (
            int(expected_run_id)
            if expected_run_id is not None
            else _kb._current_run_id(conn, task_id)
        )
        if run_id is not None:
            conn.execute("UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?", (now, run_id))
        _kb._append_event(
            conn, task_id, "heartbeat",
            {"note": note} if note else None,
            run_id=run_id,
        )
    return True


def enforce_max_runtime(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """Terminate workers whose per-task ``max_runtime_seconds`` has elapsed.

    SIGTERM, short grace, then SIGKILL. Emits ``timed_out`` and restores the
    task's source phase so the next tick re-spawns the same kind of worker —
    unless the circuit breaker already gave up, leaving it blocked. Host-local
    only (same reasoning as ``detect_crashed_workers``). ``signal_fn`` is a test hook.
    """
    timed_out: list[str] = []
    now = int(time.time())
    host_prefix = _kb._host_prefix()

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.max_runtime_seconds, t.claim_lock, r.launch_mode "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.max_runtime_seconds IS NOT NULL "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        # Runtime is per attempt: ``tasks.started_at`` records the FIRST start,
        # so retries must be measured from the active task_runs row.
        elapsed = now - int(row["active_started_at"])
        limit = int(row["max_runtime_seconds"])
        if elapsed < limit:
            continue

        pid = int(row["worker_pid"])
        tid = row["id"]
        if row["launch_mode"] == "remote-codex-supervisor":
            _fence_remote_dispatch(conn, tid, int(_kb._current_run_id(conn, tid)), "remote_supervisor_timeout")
            timed_out.append(tid)
            continue
        started_at = _kb._row_get(row, "worker_started_at")
        current_run = _kb._current_run_id(conn, tid)
        scope_release = _kb._scope_release_result(
            conn, tid, int(current_run) if current_run is not None else None,
        )
        if not scope_release.can_release:
            continue
        if (
            scope_release.pid_signal_allowed
            and started_at == UNVERIFIED_WORKER_FINGERPRINT
            and _kb._pid_alive(pid)
        ):
            # Fingerprint capture failed at spawn: we cannot prove this live PID is our worker, so
            # it is neither signalled nor released beside (duplicate). It is reclaimed once it exits.
            _kb._log.warning("kanban: task %s worker pid %s exceeded max runtime but has no verified "
                             "identity; not signalled", tid, pid)
            continue
        # SIGTERM then SIGKILL after 5 s grace; workers wanting a cleaner
        # shutdown install their own SIGTERM handler. A recycled PID (fingerprint
        # mismatch) is never signalled: the worker is already gone.
        killed = False
        kill = _kill_fn(signal_fn)
        if (
            kill is not None
            and scope_release.pid_signal_allowed
            and not (_kb._pid_alive(pid) and _pid_recycled(pid, started_at))
        ):
            with contextlib.suppress(ProcessLookupError, OSError):
                kill(pid, signal.SIGTERM)
            # Short polling wait — no time.sleep on the write txn.
            _poll_worker_exit(pid, started_at)
            if _worker_alive(pid, started_at):
                killed = _sigkill(kill, pid)

        error = f"elapsed {int(elapsed)}s > limit {limit}s"
        with _kb.write_txn(conn):
            retry_status = _kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, tid, pid, row["claim_lock"]),
            )
            if cur.rowcount == 1:
                payload = {
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": limit,
                    "sigkill": killed,
                    "retry_status": retry_status,
                }
                run_id = _kb._end_run(
                    conn, tid, outcome="timed_out", status="timed_out",
                    error=error, metadata=payload,
                )
                _kb._append_event(conn, tid, "timed_out", payload, run_id=run_id)
                timed_out.append(tid)
        # Outside the write_txn above because ``_record_task_failure`` opens its
        # own. If the breaker trips this flips the task to ``blocked`` and emits
        # ``gave_up`` on top of the ``timed_out`` already emitted.
        if cur.rowcount == 1:
            _record_task_failure(
                conn, tid,
                error=error,
                outcome="timed_out",
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "sigkill": killed, "retry_status": retry_status},
            )
    return timed_out


# A running task with no heartbeat for this long is inactive regardless of
# ``dispatch_stale_timeout_seconds`` (spec: ">4h started + no commits in 1h").
_STALE_HEARTBEAT_GAP_SECONDS = 3600


def detect_stale_running(
    conn: sqlite3.Connection,
    *,
    stale_timeout_seconds: int = 0,
    signal_fn=None,
) -> list[str]:
    """Reclaim ``running`` tasks with no heartbeat progress; returns their ids.

    Stale = running longer than ``stale_timeout_seconds`` (active run's
    ``started_at``, else ``tasks.started_at``) AND ``last_heartbeat_at`` NULL or
    older than ``_STALE_HEARTBEAT_GAP_SECONDS``. Task returns to its source
    phase, run closes ``outcome='stale'``, a live host-local worker is killed.
    ``0`` disables the check; ``signal_fn`` is a test hook. Deliberately NOT
    counted via ``_record_task_failure``: an absent heartbeat is not a worker
    failure, and counting it would let long-running tasks trip the breaker.
    """
    if stale_timeout_seconds <= 0:
        return []

    now = int(time.time())
    reclaimed: list[str] = []

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, t.last_heartbeat_at, t.claim_lock, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.current_run_id "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running'"
    ).fetchall()

    for row in rows:
        if row["active_started_at"] is None:
            continue
        elapsed = now - int(row["active_started_at"])
        if elapsed < stale_timeout_seconds:
            continue

        last_hb = row["last_heartbeat_at"]
        hb_age = (now - int(last_hb)) if last_hb is not None else None
        if hb_age is not None and hb_age < _STALE_HEARTBEAT_GAP_SECONDS:
            continue

        pid = row["worker_pid"]
        tid = row["id"]
        lock = row["claim_lock"] or ""

        scope_release = _kb._scope_release_result(
            conn,
            tid,
            int(row["current_run_id"]) if row["current_run_id"] is not None else None,
        )
        if not scope_release.can_release:
            continue
        termination = (
            _kb._termination_metadata_without_pid_signal(pid, scope_release)
            if not scope_release.pid_signal_allowed
            else _kb._terminate_reclaimed_worker(
                pid,
                lock,
                signal_fn=signal_fn,
                started_at=_kb._row_get(row, "worker_started_at"),
            )
        )

        # Never release a claim while our own worker is still alive: that would
        # spawn a duplicate beside it. Hold the claim and retry next tick.
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, tid, lock, now, termination,
                reason="heartbeat_stale_worker_alive",
            )
            continue

        with _kb.write_txn(conn):
            retry_status = _kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ?",
                (retry_status, tid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue

            payload = {
                "elapsed_seconds": int(elapsed),
                "last_heartbeat_at": _kb._opt_int(last_hb),
                "heartbeat_age_seconds": _kb._opt_int(hb_age),
                "timeout_seconds": stale_timeout_seconds,
                "pid": int(pid) if pid else None,
                "retry_status": retry_status,
            }
            payload.update(termination)

            run_id = _kb._end_run(
                conn, tid,
                outcome="stale", status="stale",
                error=(
                    f"no heartbeat for {int(hb_age)}s "
                    if hb_age is not None
                    else "no heartbeat ever"
                ) + f" after {int(elapsed)}s running",
                metadata=payload,
            )
            _kb._append_event(conn, tid, "stale", payload, run_id=run_id)
            reclaimed.append(tid)

    return reclaimed


def reconcile_orphaned_running(conn: sqlite3.Connection) -> list[str]:
    """Requeue ``running`` cards with broken claim bookkeeping; returns their ids.

    A task ``running`` with NULL ``claim_lock``/``claim_expires`` (crash
    mid-claim, manual SQL, DB restore) is a zombie forever: ``release_stale_claims``
    needs ``claim_expires``, ``detect_crashed_workers`` needs a host-local lock +
    pid, ``detect_stale_running`` is off by default. Orphans go back to ``ready``
    with a comment, leaked run closed, ``reconciled`` event; a row with a live
    host-local PID is deferred so no duplicate spawns beside it.
    """
    now = int(time.time())
    reconciled: list[str] = []
    rows = conn.execute(
        "SELECT id, claim_lock, claim_expires, worker_pid, worker_started_at FROM tasks "
        "WHERE status = 'running' "
        "  AND (claim_lock IS NULL OR claim_expires IS NULL)"
    ).fetchall()
    for row in rows:
        tid = row["id"]
        pid = row["worker_pid"]
        run_id = _kb._current_run_id(conn, tid)
        pid_signal_allowed = True
        if run_id is not None:
            scope_release = _kb._scope_release_result(conn, tid, int(run_id))
            if not scope_release.can_release:
                # Exact scoped occupancy is authoritative; do not probe or
                # signal a reused host PID while its boundary is active/unknown.
                continue
            pid_signal_allowed = scope_release.pid_signal_allowed
        if (
            pid
            and pid_signal_allowed
            and _worker_alive(pid, _kb._row_get(row, "worker_started_at"))
        ):
            # Never requeue beside a live process. Retry next tick.
            _kb._log.debug(
                "kanban reconcile: task %s has broken claim bookkeeping but "
                "pid %s is alive on this host — deferring", tid, pid,
            )
            continue
        with _kb.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ? AND claim_expires IS ?",
                (tid, row["claim_lock"], row["claim_expires"]),
            )
            if cur.rowcount != 1:
                continue
            payload = {
                "reason": "orphaned_running",
                "claim_lock": row["claim_lock"],
                "claim_expires": _kb._opt_int(row["claim_expires"]),
                "worker_pid": int(pid) if pid else None,
                "now": now,
            }
            run_id = _kb._end_run(
                conn, tid,
                outcome="reclaimed", status="reclaimed",
                error="orphaned running card (broken claim bookkeeping)",
                metadata=payload,
            )
            _kb._insert_comment(
                conn, tid, "dispatcher",
                "reconciliation: card was 'running' with no valid claim "
                "(dead/gone worker) — requeued to ready",
                now,
            )
            _kb._append_event(conn, tid, "reconciled", payload, run_id=run_id)
            reconciled.append(tid)
        _kb._log.info(
            "kanban reconcile: requeued orphaned running task %s "
            "(claim_lock=%r, worker_pid=%r)", tid, row["claim_lock"], pid,
        )
    return reconciled


def _error_fingerprint(error_text: str) -> str:
    """Normalize an error message (strip PIDs, timestamps) so same-root-cause errors group."""
    fp = re.sub(r'\bpid \d+\b', 'pid N', error_text[:80])
    fp = re.sub(r'\b\d{10,}\b', '<TS>', fp)
    return fp.lower().strip()


# ~96% of "clean exit without a terminal tool call" tasks complete on a later
# run, so a protocol violation gets a bounded retry before the breaker trips.
# The budget is a violation-only STREAK (``_protocol_violation_streak``),
# independent of ``consecutive_failures``: other failure kinds neither consume
# nor extend it. Per-task ``max_retries`` overrides it.
_PROTOCOL_VIOLATION_FAILURE_LIMIT = 3

# Closed runs to walk when counting the streak; it trips at a handful anyway.
_PROTOCOL_VIOLATION_SCAN_LIMIT = 50


def _protocol_violation_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Count the task's trailing run of clean-exit protocol violations.

    Walks closed runs newest-first (including the one ``detect_crashed_workers``
    just closed). ``rate_limited`` runs are neutral and skipped (a quota wall
    says nothing about the task); any other closed run breaks the streak, so
    the budget counts ONLY protocol violations. Violations are recognized by the
    ``protocol_violation`` run-metadata marker, with the error text as fallback
    for runs recorded before the marker existed.
    """
    streak = 0
    rows = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (task_id, _PROTOCOL_VIOLATION_SCAN_LIMIT),
    ).fetchall()
    for row in rows:
        outcome = row["outcome"] or ""
        if outcome == "rate_limited":
            continue
        if outcome == "crashed" and (
            _kb._json_dict(row["metadata"]).get("protocol_violation")
            or "protocol violation" in (row["error"] or "")
        ):
            streak += 1
            continue
        break
    return streak


_PROTOCOL_VIOLATION_ERROR = (
    # Worker subprocess returned 0 but its task is still ``running`` in the DB — it exited without calling
    # ``kanban_complete`` / ``kanban_block`` / ``kanban_request_review``. Overwhelmingly the work itself succeeded and only the
    # paperwork was skipped, so a retry usually completes; the corrective sentence below is surfaced to the
    # retry worker via the prior-attempt error in ``build_worker_context`` (guidance approach from #61817).
    # Keep this short: ``_record_task_failure`` caps the stored error at 500 chars and the worker's own
    # last output (``_worker_final_output``, up to 400 chars) is appended after it — a longer preamble
    # truncates away the worker's explanation, which is the part the board and the retry worker need.
    "worker exited cleanly (rc=0) without kanban_complete, kanban_block "
    "or kanban_request_review — protocol violation. "
    "If the prior run already did the work, verify it and "
    "report it via kanban_complete (or kanban_request_review); "
    "a run without a terminal kanban call counts as failed no "
    "matter what it did."
)


_EXIT_SUMMARY_MARKER = "Resume this session with:"
# Rich panel/rule chrome around the rendered response, and the CLI's own preamble lines.
_LOG_CHROME = re.compile(r"[─━═╭╮╰╯│┃┌┐└┘]+|☤\s*Hermes")
_LOG_NOISE_PREFIXES = ("session_id:", "Query:", "Initializing agent")


def _worker_final_output(task_id: str, board: Optional[str] = None) -> str:
    """Best-effort read of a dead worker's last printed text, for the board diagnostic.

    A ``chat -q`` worker's stdout/stderr are redirected to its per-task log
    (``_default_spawn``), so when it exits without a terminal board call the
    reason is usually sitting there: the model's own explanation of why it could
    not comply (#88603), or the rendered provider error (#46593). The reap used to
    discard it in favour of a canned message on every retry. Trims the CLI exit
    summary, rule lines and the ``session_id:`` trailer; returns "" (never raises)
    on a missing/empty log.

    ``board`` must come from the dispatching tick: ambient current-board resolution
    is wrong for every board but the one the dispatcher thread happens to call
    "current", so the log would silently not be found.
    """
    try:
        raw = _kb.read_worker_log(task_id, tail_bytes=4000, board=board)
    except Exception:
        return ""
    if not raw:
        return ""
    raw = _EXIT_TRAILER_RE.sub("", raw)
    cut = raw.rfind(_EXIT_SUMMARY_MARKER)
    if cut != -1:
        raw = raw[:cut]
    lines = []
    for ln in raw.splitlines():
        ln = _LOG_CHROME.sub("", ln).strip()
        if ln and not ln.startswith(_LOG_NOISE_PREFIXES):
            lines.append(ln)
    return " ".join(lines)[-400:]


@dataclass
class _DeadWorker:
    """How ``detect_crashed_workers`` should book one dead worker."""

    kind: str
    code: Optional[int]
    error_text: str
    event_kind: str
    event_payload: dict
    protocol_violation: bool = False
    rate_limited: bool = False
    terminal_provider: bool = False
    """``KANBAN_TERMINAL_PROVIDER_EXIT_CODE``: the provider rejected the worker's
    credential/model — trips the breaker on this first occurrence."""

    @property
    def run_outcome(self) -> str:
        # A rate-limited requeue is recorded as ``rate_limited`` so board history
        # doesn't show a phantom crash for a quota wall.
        return "rate_limited" if self.rate_limited else "crashed"


def _classify_dead_worker(
    pid: int, claimer: Optional[str], *, task_id: Optional[str] = None, board: Optional[str] = None,
) -> _DeadWorker:
    """Map a dead worker's reaped exit status to its reclaim bookkeeping.

    A clean exit or a crash carries the worker's own last output (``worker_output``
    in the event payload, appended to the error text) so the board and the retry
    worker see WHY instead of a bare label; a rate-limited requeue does not need it.
    """
    dead = _classify_dead_worker_exit(pid, claimer, task_id=task_id, board=board)
    if task_id and not dead.rate_limited:
        worker_output = _worker_final_output(task_id, board=board)
        if worker_output:
            dead.error_text += f" Worker's last output: {worker_output!r}"
            dead.event_payload["worker_output"] = worker_output
    return dead


def _classify_dead_worker_exit(
    pid: int,
    claimer: Optional[str],
    *,
    task_id: Optional[str] = None,
    board: Optional[str] = None,
) -> _DeadWorker:
    """Exit status -> reclaim bookkeeping, before the worker's own words are folded in.

    The reap registry only knows children of THIS process; a per-tick dispatcher
    reads the exit trailer the worker left in its log instead, so the same death
    gets the same booking (protocol violation / rate-limit requeue / crash) as
    under the gateway-embedded dispatcher. A worker that never reached its exit
    epilogue (killed, OOM) leaves no trailer and stays a plain crash.
    """
    kind, code = _classify_worker_exit(pid)
    if kind == "unknown" and task_id:
        logged = _worker_log_exit_code(task_id, board=board)
        if logged is not None:
            kind, code = _exit_code_kind(logged)
    if kind == "clean_exit":
        # rc=0 while still ``running``: usually the work succeeded and only the
        # paperwork was skipped; the corrective sentence reaches the retry
        # worker via ``build_worker_context``.
        return _DeadWorker(
            kind, code, _PROTOCOL_VIOLATION_ERROR, "protocol_violation",
            # ``protocol_violation`` is the durable marker for
            # _protocol_violation_streak: _end_run copies this payload into the
            # run metadata.
            {"pid": pid, "claimer": claimer, "exit_code": code, "protocol_violation": True},
            protocol_violation=True,
        )
    if kind == "rate_limited":
        # Quota wall — NOT a task failure. Release to the source phase and do
        # NOT count a failure so a long quota window can't trip the breaker.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited rate-limited (quota wall) — requeued without counting a failure",
            "rate_limited",
            {"pid": pid, "claimer": claimer, "exit_code": code},
            rate_limited=True,
        )
    if kind == "terminal_provider":
        # The worker classified its own provider failure as unhealable (credential
        # revoked, model gone): every further spawn would hit the same wall, so
        # ``_account_crashes`` trips the breaker now instead of after ``failure_limit``.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited on a terminal provider error (exit {code}): the provider rejected "
            "this profile's credential or model — fix the configuration, then unblock.",
            "crashed",
            {"pid": pid, "claimer": claimer, "exit_kind": kind, "exit_code": code, "terminal_provider": True},
            terminal_provider=True,
        )
    if kind == "nonzero_exit":
        error_text = f"pid {pid} exited with code {code}"
    elif kind == "signaled":
        error_text = f"pid {pid} killed by signal {code}"
    else:
        error_text = f"pid {pid} not alive"
    event_payload = {"pid": pid, "claimer": claimer}
    if code is not None and kind != "unknown":
        event_payload["exit_kind"] = kind
        event_payload["exit_code"] = code
    return _DeadWorker(kind, code, error_text, "crashed", event_payload)


@dataclass
class _CrashSweep:
    """Everything ``detect_crashed_workers`` collects inside its reclaim txn."""

    crashed: list[str] = field(default_factory=list)
    rate_limited: list[str] = field(default_factory=list)
    # ``(task_id, pid, claimer, dead_worker)``: accounted after the txn via
    # ``_record_task_failure`` (needs its own write_txn).
    crash_details: list[tuple[str, int, str, _DeadWorker]] = field(default_factory=list)
    # Worker-exit observer payloads, fired only after every reclaim/accounting
    # txn has committed.
    exited_hook_payloads: list[dict] = field(default_factory=list)


def _reclaim_dead_workers(conn: sqlite3.Connection, board: Optional[str] = None) -> _CrashSweep:
    """Release every host-local ``running`` task whose worker PID is dead."""
    sweep = _CrashSweep()
    with _kb.write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, worker_started_at, claim_lock, started_at, assignee "
            "       , current_run_id "
            "FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = _kb._host_prefix()
        for row in rows:
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            # Launch-window grace so a freshly-spawned worker isn't reclaimed
            # before its PID is visible on /proc.
            started_at = _kb._row_get(row, "started_at")
            if started_at is not None and time.time() - started_at < _kb._resolve_crash_grace_seconds():
                continue
            run_id = row["current_run_id"]
            scope_release = _kb._scope_release_result(
                conn, row["id"], int(run_id) if run_id is not None else None,
            )
            if not scope_release.can_release:
                # Unknown/active scoped occupancy remains fenced; a host PID
                # being dead does not prove descendants are gone.
                continue

            if (
                scope_release.pid_signal_allowed
                and _worker_alive(
                    row["worker_pid"], _kb._row_get(row, "worker_started_at")
                )
            ):
                continue

            pid = int(row["worker_pid"])
            dead = _classify_dead_worker(pid, row["claim_lock"], task_id=row["id"], board=board)
            retry_status = _kb._retry_status_for_run(conn, row["id"])
            dead.event_payload["retry_status"] = retry_status
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, row["id"], pid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue
            run_id = _kb._end_run(
                conn, row["id"],
                outcome=dead.run_outcome, status=dead.run_outcome,
                error=dead.error_text,
                metadata=dict(dead.event_payload),
            )
            _kb._append_event(conn, row["id"], dead.event_kind, dead.event_payload, run_id=run_id)
            sweep.exited_hook_payloads.append({
                "task_id": row["id"],
                "assignee": row["assignee"],
                "run_id": run_id,
                "worker_pid": pid,
                "exit_kind": dead.kind,
                "exit_code": dead.code,
                "outcome": dead.run_outcome,
                "retry_status": retry_status,
            })
            if dead.rate_limited or dead.protocol_violation:
                # Stamp last_failure_error WITHOUT touching ``consecutive_failures``:
                # a rate-limited requeue must show ``check_respawn_guard`` a quota
                # blocker; a below-budget protocol violation never reaches
                # ``_record_task_failure`` (which stamps this column), yet the
                # board UI and retry worker need the corrective message.
                conn.execute(
                    "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                    (dead.error_text[:500], row["id"]),
                )
            if dead.rate_limited:
                sweep.rate_limited.append(row["id"])
            else:
                sweep.crashed.append(row["id"])
                sweep.crash_details.append((row["id"], pid, row["claim_lock"], dead))
    return sweep


def _account_crashes(conn: sqlite3.Connection, crash_details: list) -> list[str]:
    """Count each crash against the breaker; returns the task ids it tripped.

    Protocol violations get a BOUNDED violation-only budget independent of
    ``consecutive_failures`` (per-task ``max_retries`` takes precedence);
    systemic same-error crashes (>= 3 identical fingerprints this tick) and
    terminal provider errors (credential revoked, model gone — a retry cannot
    heal them) trip immediately.
    """
    auto_blocked: list[str] = []
    fp_counts: dict[str, int] = {}
    for _, _, _, dead in crash_details:
        fp = _error_fingerprint(dead.error_text)
        fp_counts[fp] = fp_counts.get(fp, 0) + 1
    for tid, pid, claimer, dead in crash_details:
        error_text = dead.error_text
        if dead.protocol_violation:
            streak = _protocol_violation_streak(conn, tid)
            trow = conn.execute("SELECT max_retries FROM tasks WHERE id = ?", (tid,)).fetchone()
            if trow is None:
                continue  # task deleted mid-loop
            task_override = _kb._row_get(trow, "max_retries")
            violation_limit = (
                int(task_override) if task_override is not None else _PROTOCOL_VIOLATION_FAILURE_LIMIT
            )
            if streak < violation_limit:
                # Below budget: already back at ``ready`` with the error stamped.
                # No ``_record_task_failure`` — must not consume the unified budget.
                continue
            # ``force_trip``: the decision (incl. per-task ``max_retries``) was
            # already made against the violation streak above.
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=violation_limit,
                force_trip=True,
                release_claim=False,
                end_run=False,
                event_payload_extra={
                    "pid": pid,
                    "claimer": claimer,
                    "protocol_violations": streak,
                    "protocol_violation_limit": violation_limit,
                },
            )
        elif dead.terminal_provider:
            # A retry cannot heal a revoked credential or a missing model, so
            # the whole ``failure_limit`` budget would be spent on identical
            # failures. ``force_trip`` blocks now, sticky: ``recompute_ready``
            # must not auto-resume it before the operator fixes the provider.
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                force_trip=True,
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "claimer": claimer, "terminal_provider": True},
            )
        else:
            is_systemic = fp_counts.get(_error_fingerprint(error_text), 0) >= 3
            extra = {"pid": pid, "claimer": claimer}
            if is_systemic:
                # Trips at 1, below any ``failure_limit``: hold it for an operator.
                extra["sticky"] = True
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=1 if is_systemic else None,
                release_claim=False,
                end_run=False,
                event_payload_extra=extra,
            )
        if tripped:
            auto_blocked.append(tid)
    return auto_blocked


def detect_crashed_workers(conn: sqlite3.Connection, board: Optional[str] = None) -> list[str]:
    """Reclaim ``running`` tasks whose worker PID is no longer alive.

    Restores the source phase immediately (no waiting for the claim TTL), for
    tasks claimed by *this host* only — other hosts' PIDs are meaningless.
    Clean exit while ``running`` is a protocol violation with a bounded
    violation-only retry budget; ``KANBAN_RATE_LIMIT_EXIT_CODE`` is a quota
    wall, released WITHOUT counting a failure and surfaced via the
    ``_last_rate_limited`` attribute (the return stays crashed-only).
    """
    sweep = _reclaim_dead_workers(conn, board=board)
    # Outside the main txn: account each crash and maybe trip the breaker.
    auto_blocked = _account_crashes(conn, sweep.crash_details) if sweep.crash_details else []
    # Side-channel attributes keep the public ``list[str]`` return stable;
    # ``dispatch_once`` reads them to populate ``DispatchResult``. Rate-limited
    # requeues did NOT count a failure and are NOT crashes.
    detect_crashed_workers._last_auto_blocked = auto_blocked  # type: ignore[attr-defined]
    detect_crashed_workers._last_rate_limited = sweep.rate_limited  # type: ignore[attr-defined]
    # Fired only now, after the reclaim txn AND breaker accounting have
    # committed, so subscribers always observe fully durable board state.
    if sweep.exited_hook_payloads and _kb._kanban_observer_consumed("on_kanban_worker_exited"):
        _board = _kb.get_current_board()
        for hook_fields in sweep.exited_hook_payloads:
            hook_fields = dict(hook_fields)
            _kb._fire_kanban_lifecycle_hook(
                # Kanban worker-lifecycle, task-mutation, and dispatcher-tick observers (RFC #58548,
                # accepted as the design basis in the #64231 batch disposition; on_kanban_dispatch_tick is
                # the re-port of PR #56066). All five are observers only: return values are ignored, and
                # every fire site is fully best-effort, so a broken callback can never break dispatch or a
                # task mutation. Cost rule: every call site short-circuits on has_hook(), so when nothing
                # subscribes no payload is built and the hot paths (each dispatcher tick, each task write)
                # pay one dict probe. WHICH PROCESS: worker spawn/exit/stale-claim and the dispatch tick
                # fire in the DISPATCHER process (gateway-embedded dispatcher or ``hermes kanban
                # dispatch``); on_kanban_task_updated fires in whichever process committed the mutation
                # (CLI, worker, or the gateway-embedded dashboard API). Common kwargs (task-scoped hooks):
                # task_id: str, profile_name: str, board: str | None, assignee: str | None, run_id: int |
                # None. on_kanban_worker_spawned fires after ``spawn_fn`` returns AND the worker PID (when
                # one was reported) is durably persisted, per the RFC timing contract; like
                # kanban_task_claimed it runs inside the board's dispatch lock, so callbacks must stay fast.
                # Adds: worker_pid: int | None, workspace_path: str. Privacy: workspace_path is a filesystem
                # path and may reveal project layout or usernames.
                "on_kanban_worker_exited",
                hook_fields.pop("task_id"),
                board=_board,
                **hook_fields,
            )
    return sweep.crashed


def _record_task_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    outcome: str,
    failure_limit: int = None,
    force_trip: bool = False,
    release_claim: bool = False,
    end_run: bool = False,
    event_payload_extra: Optional[dict] = None,
    infrastructure: bool = False,
) -> bool:
    """Record a non-success outcome and maybe trip the circuit breaker; every
    non-success path funnels through here so ``consecutive_failures`` stays
    consistent. Returns True when the task was auto-blocked.

    ``release_claim=True, end_run=True``: spawn-failure path (task still
    running with an open run — restore source phase or ``blocked``, release
    claim, close run). Both False: timeout/crash path (caller already restored
    the phase and closed the run; only the counter moves, a trip flips to
    ``blocked`` + ``gave_up``). Threshold: per-task ``max_retries`` >
    ``failure_limit`` > ``DEFAULT_FAILURE_LIMIT``. ``force_trip`` trips
    unconditionally (caller applied its own bounded-retry policy).

    ``infrastructure=True``: the host refused the spawn (no restart-safe scope,
    #114720) — nothing about the card ran, so the run and event are recorded
    with ``infrastructure: true`` but ``consecutive_failures`` is left alone and
    the breaker never trips; the card stays retryable and
    :func:`check_respawn_guard` spaces the retries.
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    error = error[:500]
    with _kb.write_txn(conn):
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries, current_run_id "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        retry_status = (
            _kb._retry_status_for_run(conn, task_id, row["current_run_id"])
            if release_claim
            else ("review" if row["status"] == "review" else "ready")
        )
        failures = int(row["consecutive_failures"]) + (0 if infrastructure else 1)

        # Per-task override wins over caller-supplied and default thresholds.
        task_override = _kb._row_get(row, "max_retries")
        if task_override is not None:
            effective_limit, limit_source = int(task_override), "task"
        else:
            effective_limit, limit_source = int(failure_limit), "dispatcher"

        if infrastructure or not (force_trip or failures >= effective_limit):
            if release_claim:
                # Spawn path: restore the claimed source phase + clear claim.
                conn.execute(
                    "UPDATE tasks SET status = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (retry_status, failures, error, task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, error, task_id),
                )
            # Timeout/crash path's caller already emitted its own event.
            if end_run:
                detail = {"failures": failures, "retry_status": retry_status}
                if infrastructure:
                    detail["infrastructure"] = True
                run_id = _kb._end_run(
                    conn, task_id, outcome=outcome, status=outcome, error=error, metadata=detail,
                )
                _kb._append_event(conn, task_id, outcome, {"error": error, **detail}, run_id=run_id)
            return False

        # Spawn path (release_claim) is still running and also clears claim
        # state; the timeout/crash path already did.
        conn.execute(
            "UPDATE tasks SET status = 'blocked', "
            + ("claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
               if release_claim else "")
            + "consecutive_failures = ?, last_failure_error = ? "
            "WHERE id = ? AND status IN ('running', 'ready', 'review')",
            (failures, error, task_id),
        )
        payload = {
            "failures": failures,
            "effective_limit": effective_limit,
            "limit_source": limit_source,
            "error": error,
            "trigger_outcome": outcome,
            "retry_status": retry_status,
        }
        run_id = None
        if end_run:
            # Only the spawn path has an open run to close.
            run_id = _kb._end_run(
                conn, task_id, outcome="gave_up", status="gave_up", error=error,
                metadata={
                    "failures": failures,
                    "trigger_outcome": outcome,
                    "effective_limit": effective_limit,
                    "limit_source": limit_source,
                    "retry_status": retry_status,
                },
            )
        if force_trip:
            # The caller applied its own bounded policy, so the counter cannot
            # judge this block: ``recompute_ready`` holds it for an operator.
            payload["sticky"] = True
        if event_payload_extra:
            payload.update(event_payload_extra)
        _kb._append_event(conn, task_id, "gave_up", payload, run_id=run_id)
        return True


def _set_worker_pid(conn: sqlite3.Connection, task_id: str, pid: int) -> None:
    """Persist an authenticated launch receipt and PID-reuse fingerprint."""
    receipt_launch_mode = getattr(pid, "launch_mode", None)
    launch_mode = receipt_launch_mode or "direct"
    scope_unit = getattr(pid, "scope_unit", None)
    receipt_verification_status = getattr(pid, "verification_status", None)
    verification_status = receipt_verification_status or "not-applicable"
    manager_kind = getattr(pid, "manager_kind", None)
    manager_uid = getattr(pid, "manager_uid", None)
    launch_acknowledged = getattr(pid, "launch_acknowledged", None)
    scope_slice = getattr(pid, "scope_slice", None)
    memory_high = getattr(pid, "memory_high", None)
    memory_max = getattr(pid, "memory_max", None)
    memory_swap_max = getattr(pid, "memory_swap_max", None)
    tasks_max = getattr(pid, "tasks_max", None)
    oom_policy = getattr(pid, "oom_policy", None)
    control_group = getattr(pid, "control_group", None)
    if type(pid) is bool or not isinstance(pid, int) or int(pid) <= 0:
        raise ValueError("worker PID must be a positive integer")
    if launch_mode not in {"direct", "systemd-user-scope", "remote-codex-supervisor"}:
        raise RuntimeError(f"unknown worker launch mode: {launch_mode!r}")
    started_at = _process_fingerprint(int(pid)) or UNVERIFIED_WORKER_FINGERPRINT
    with _kb.write_txn(conn):
        task_row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        run_id = int(task_row["current_run_id"]) if task_row and task_row["current_run_id"] is not None else None
        if task_row is None or task_row["status"] != "running" or run_id is None:
            raise RuntimeError("worker launch no longer owns the active task run")
        if launch_mode == "systemd-user-scope":
            try:
                db_row = next(item for item in conn.execute("PRAGMA database_list").fetchall() if item[1] == "main")
                expected_unit = _kb._systemd_scope_unit_name(task_id, run_id, db_path=db_row[2])
            except (OSError, StopIteration, TypeError, ValueError, IndexError):
                expected_unit = None
            if not (
                scope_unit == expected_unit
                and _kb._SYSTEMD_WORKER_SCOPE_RE.fullmatch(scope_unit or "")
                and manager_kind == _kb._SYSTEMD_USER_MANAGER_KIND
                and type(manager_uid) is int
                and _kb._systemd_user_manager_target_for_uid(manager_uid) is not None
                and launch_acknowledged is True
                and verification_status == "verified"
                and _kb._valid_scope_resource_receipt(
                    scope_slice=scope_slice, memory_high=memory_high,
                    memory_max=memory_max, memory_swap_max=memory_swap_max,
                    tasks_max=tasks_max, oom_policy=oom_policy,
                    control_group=control_group,
                )
            ):
                raise RuntimeError("refusing to persist an unauthenticated worker scope receipt")
        elif launch_mode == "remote-codex-supervisor":
            if verification_status not in {"remote-prepared", "remote-running"}:
                raise RuntimeError("remote worker launch has no prepared receipt")
            if any(value is not None for value in (
                scope_unit, manager_kind, manager_uid, launch_acknowledged,
                scope_slice, memory_high, memory_max, memory_swap_max,
                tasks_max, oom_policy, control_group,
            )):
                raise RuntimeError("remote worker launch carried scoped identity fields")
        elif any(value is not None for value in (
            scope_unit, manager_kind, manager_uid, launch_acknowledged,
            scope_slice, memory_high, memory_max, memory_swap_max,
            tasks_max, oom_policy, control_group,
        )):
            raise RuntimeError("direct worker launch carried scoped identity fields")
        cur = conn.execute(
            "UPDATE tasks SET worker_pid=?, worker_started_at=? "
            "WHERE id=? AND status='running' AND current_run_id=?",
            (int(pid), started_at, task_id, run_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("worker launch lost its task/run fence")
        cur = conn.execute(
            "UPDATE task_runs SET worker_pid=?, worker_started_at=?, launch_mode=?, scope_unit=?, manager_kind=?, manager_uid=?, "
            "launch_acknowledged=?, verification_status=?, scope_slice=?, memory_high=?, memory_max=?, "
            "memory_swap_max=?, tasks_max=?, oom_policy=?, control_group=?, "
            "reap_state=CASE WHEN terminal_action IS NOT NULL THEN reap_state ELSE NULL END, "
            "reap_error=CASE WHEN terminal_action IS NOT NULL THEN reap_error ELSE NULL END "
            "WHERE id=? AND task_id=? AND ended_at IS NULL",
            (int(pid), started_at, launch_mode, scope_unit, manager_kind, manager_uid,
             int(launch_acknowledged) if type(launch_acknowledged) is bool else None,
             verification_status, scope_slice, memory_high, memory_max,
             memory_swap_max, tasks_max, oom_policy, control_group, run_id, task_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("worker launch receipt promotion lost its run fence")
        payload = {"pid": int(pid), "started_at": started_at}
        for key, value in (
            ("launch_mode", receipt_launch_mode), ("scope_unit", scope_unit),
            ("verification_status", receipt_verification_status), ("manager_kind", manager_kind),
            ("manager_uid", manager_uid), ("launch_acknowledged", launch_acknowledged),
            ("scope_slice", scope_slice), ("memory_high", memory_high),
            ("memory_max", memory_max), ("memory_swap_max", memory_swap_max),
            ("tasks_max", tasks_max), ("oom_policy", oom_policy), ("control_group", control_group),
        ):
            if value is not None:
                payload[key] = value
        _kb._append_event(conn, task_id, "spawned", payload, run_id=run_id)


def _clear_failure_counter(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset the unified consecutive-failures counter.

    Called from ``complete_task`` on success. NOT called on spawn success: a
    spawn proves the worker could start, not that the run will succeed, so
    timeouts and crashes must accumulate across spawn boundaries.
    """
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 0, "
            "last_failure_error = NULL WHERE id = ?",
            (task_id,),
        )


def check_respawn_guard(
    conn: sqlite3.Connection, task_id: str, *, lane: str = "ready",
) -> Optional[str]:
    """Return a guard reason if ``task_id`` should NOT be re-spawned, else None.

    Called per ready/review row before any claim attempt. Priority order:
    ``"infrastructure_cooldown"`` (latest run is a ``spawn_failed`` the host
    refused — no restart-safe scope — within the cooldown; never counted),
    ``"rate_limit_cooldown"`` (latest run ``rate_limited`` within the cooldown;
    checked BEFORE ``blocker_auth`` because the requeue stamps a quota-flavored
    ``last_failure_error`` that would otherwise park the task forever — that
    path never increments ``consecutive_failures``), ``"blocker_auth"``
    (quota/auth pattern; the breaker still trips eventually), then for the
    ready lane only ``"recent_success"`` (completed run within the window, unless
    a re-queue event arrived after it — a deliberate re-run) and ``"active_pr"``
    (PR URL in a recent comment; re-spawning risks a duplicate PR — unless a
    handoff event followed the comment: the named profile must work on that
    PR). The review lane skips the last two: they are the *inputs* to a review
    handoff. Stale / dead claim locks are NOT a guard reason — the reclaim
    passes own those.
    """
    row = conn.execute(
        "SELECT last_failure_error FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    now = int(time.time())

    # 1. Rate-limit cooldown — see docstring for why this precedes blocker_auth.
    #    LATEST run only: a newer crash/completion supersedes the rate-limit run.
    #    An infrastructure spawn refusal (#114720) shares the cooldown: the host
    #    condition is not the card's, so it retries forever, spaced, and never
    #    reaches the breaker.
    rl_cooldown = _kb._resolve_rate_limit_cooldown_seconds()
    latest_run = conn.execute(
        "SELECT outcome, ended_at, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if latest_run is not None and latest_run["outcome"] == "spawn_failed":
        if rl_cooldown > 0 and _kb._json_dict(latest_run["metadata"]).get("infrastructure"):
            ended_at = latest_run["ended_at"]
            if ended_at is not None and (now - int(ended_at)) < rl_cooldown:
                return "infrastructure_cooldown"
    if latest_run is not None and latest_run["outcome"] == "rate_limited":
        if rl_cooldown <= 0:
            # Cooldown disabled — respawn immediately, skipping blocker_auth so
            # the stamped rate-limit text doesn't re-trap the task.
            return None
        ended_at = latest_run["ended_at"]
        if ended_at is not None and (now - int(ended_at)) < rl_cooldown:
            return "rate_limit_cooldown"
        # Cooldown elapsed — return early so blocker_auth doesn't catch the
        # stamped rate-limit text; this path intentionally retries forever
        # (spaced by the cooldown) until quota returns or a real run supersedes it.
        return None

    # 2. Quota / auth blocker: retrying immediately will not help.  A plain
    # crash is different: its persisted error includes the worker's last
    # captured output, which is context rather than a diagnosis and may contain
    # benign commands such as ``claude auth status`` (#117097).
    err = _kb._lossy_text(row["last_failure_error"])
    latest_outcome = latest_run["outcome"] if latest_run is not None else None
    if err and latest_outcome != "crashed" and _RESPAWN_BLOCKER_RE.search(err):
        return "blocker_auth"

    # Review-lane spawns stop here: a recent completed run and a fresh PR URL
    # are the canonical *inputs* to a review handoff, not duplicate-work signals.
    if lane == "review":
        return None

    # 3. Completed run within guard window. Exception: an explicit re-queue
    #    AFTER that success (done→ready drag, re-promotion, unblock, reclaim) is
    #    a deliberate "run it again" — otherwise a manual done→ready would sit
    #    silently held until the window elapses.
    cutoff = now - _RESPAWN_GUARD_SUCCESS_WINDOW
    recent_completed = conn.execute(
        "SELECT ended_at FROM task_runs "
        "WHERE task_id = ? AND outcome = 'completed' AND ended_at >= ? "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id, cutoff),
    ).fetchone()
    if recent_completed:
        completed_at = int(recent_completed["ended_at"] or 0)
        requeued_after = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND created_at >= ? "
            "AND kind IN ('status', 'promoted', 'unblocked', 'reclaimed') "
            "LIMIT 1",
            (task_id, completed_at),
        ).fetchone()
        if not requeued_after:
            return "recent_success"

    # 4. GitHub PR URL in a recent comment — prior worker already opened a PR.
    #    Exception: a handoff AFTER the newest PR comment (operator reassign,
    #    reviewer changes_requested, review reopen) names the profile that must
    #    now work on THAT PR — a closer or the implementer finishing it, not a
    #    duplicate implementation (#111910). A crash/reclaim is not a handoff,
    #    so the worker that opened the PR is still not re-spawned against it.
    pr_cutoff = now - _RESPAWN_GUARD_PR_WINDOW
    for c in conn.execute(
        "SELECT body, created_at FROM task_comments "
        "WHERE task_id = ? AND created_at >= ? ORDER BY created_at DESC",
        (task_id, pr_cutoff),
    ).fetchall():
        body = _kb._lossy_text(c["body"])
        if not (body and _RESPAWN_GUARD_PR_URL_RE.search(body)):
            continue
        events = conn.execute(
            # Strictly after: a same-second tie stays guarded (fail closed).
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND created_at > ? "
            "AND kind IN ('assigned', 'changes_requested', 'review_reopened')",
            (task_id, int(c["created_at"] or 0)),
        ).fetchall()
        if any(_is_handoff_event(e["kind"], e["payload"]) for e in events):
            return None
        return "active_pr"

    return None


def _is_handoff_event(kind: str, payload: Optional[str]) -> bool:
    """Only an ``assigned`` event that moves the card to a DIFFERENT profile is
    a handoff. A no-op re-assign (dev→dev via CLI/dashboard/``reassign
    --reclaim``), an unassign, or the dispatcher's own
    ``kanban.default_assignee`` write would otherwise lift ``active_pr`` for
    the very implementer that opened the PR. Events without ``from`` (written
    before it was recorded) are not trusted as handoffs — fail closed."""
    if kind != "assigned":
        return True
    data = _kb._json_or(payload, {})
    if not isinstance(data, dict) or data.get("source") == "kanban.default_assignee":
        return False
    to = data.get("assignee")
    return bool(to) and "from" in data and data["from"] != to


def _profile_exists_fn() -> Optional[Callable[[str], bool]]:
    """``hermes_cli.profiles.profile_exists``, or ``None`` when it cannot be
    imported (local import avoids a cycle; callers fall back to trusting the
    assignee).

    When ``kanban.dispatch_profiles`` is set (#110995) the returned predicate
    additionally requires the assignee to be listed, fail-closed — so a card
    assigned to ``default`` is only claimable by homes that opted into it.
    Foreign assignees land in the existing ``skipped_nonspawnable`` bucket.
    """
    try:
        from hermes_cli.profiles import normalize_profile_name, profile_exists
    except Exception:
        return None
    allowlist = _dispatch_profile_allowlist(normalize_profile_name)
    if allowlist is None:
        return profile_exists

    def _gated(name: str) -> bool:
        try:
            canon = normalize_profile_name(name)
        except ValueError:
            return False
        return canon in allowlist and bool(profile_exists(name))

    return _gated


def _dispatch_profile_allowlist(normalize_profile_name) -> Optional[frozenset]:
    """Per-home claim allowlist ``kanban.dispatch_profiles`` (#110995).

    On a shared board (one ``kanban.db`` mounted across several Hermes homes),
    every home's ``profile_exists`` returns True for ``default`` — the root
    profile every home has — so a card assigned to ``default`` is claimable by
    every home's dispatcher. A home opts out of foreign claims by declaring
    which assignees it may claim::

        kanban:
          dispatch_profiles: ["sage", "researcher"]   # or "sage,researcher"

    Returns ``None`` only when the key is absent from the user config (upstream
    behavior: any existing profile is claimable). A present value is
    fail-closed: an empty list, ``null`` or a bare ``dispatch_profiles:`` claims
    nothing. The user layer is read without the ``DEFAULT_CONFIG`` merge (whose
    ``None`` placeholder would make the key look present in every home), and a
    config read that raises also claims nothing — a corrupt config on a shared
    board must never widen this home's claim scope silently (#113620).
    """
    try:
        from hermes_cli.config_effective import load_user_config_effective
        kanban = (load_user_config_effective(fail_closed=True) or {}).get("kanban", {})
    except Exception as exc:
        _kb._log.warning(
            "kanban: could not read kanban.dispatch_profiles (%s: %s) — "
            "this home claims no cards until the config is readable",
            type(exc).__name__, exc,
        )
        return frozenset()
    if not isinstance(kanban, Mapping) or "dispatch_profiles" not in kanban:
        return None
    raw = kanban["dispatch_profiles"]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        _kb._log.warning(
            "kanban: kanban.dispatch_profiles is present but empty — this home "
            "claims no cards; omit the key to allow any existing profile"
        )
        return frozenset()
    names = [str(n) for n in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    allowed = set()
    for n in names:
        try:
            allowed.add(normalize_profile_name(n))
        except ValueError:
            continue
    return frozenset(allowed)


def dispatch_profile_allowlist_summary() -> str:
    """Human-readable resolution of ``kanban.dispatch_profiles`` for this home.

    Surfaced by ``hermes kanban diagnostics`` so an operator on a shared board
    can see what a home believes it may claim (#113620): ``any`` (key absent),
    the sorted allowed names, or ``none (fail-closed: ...)``.
    """
    try:
        from hermes_cli.profiles import normalize_profile_name
    except Exception as exc:
        return f"none (fail-closed: profiles unavailable: {exc})"
    allowlist = _dispatch_profile_allowlist(normalize_profile_name)
    if allowlist is None:
        return "any"
    if allowlist:
        return ", ".join(sorted(allowlist))
    return ("none (fail-closed: kanban.dispatch_profiles is present but names no valid "
            "profile, or the config could not be read — omit the key to allow any)")


def _has_spawnable(conn: sqlite3.Connection, status: str) -> bool:
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM tasks "
        "WHERE status = ? AND assignee IS NOT NULL AND claim_lock IS NULL",
        (status,),
    ).fetchall()
    if not rows:
        return False
    profile_exists = _profile_exists_fn()
    if profile_exists is None:
        # Can't introspect — assume spawnable, preserve legacy behavior.
        return True
    return any(profile_exists(row["assignee"]) for row in rows)


def has_spawnable_ready(conn: sqlite3.Connection) -> bool:
    """True iff a ready+assigned+unclaimed task maps to a real Hermes profile.

    Lets health telemetry tell "stuck" (``0 spawned`` with spawnable work) from
    "correctly idle" (only control-plane lanes waiting on ``claim_task``). Falls
    back to "any assigned" when ``profile_exists`` is unimportable.
    """
    return _has_spawnable(conn, "ready")


def has_spawnable_review(conn: sqlite3.Connection) -> bool:
    """:func:`has_spawnable_ready` for the review column."""
    return _has_spawnable(conn, "review")


def review_dispatch_enabled() -> bool:
    """Whether review tasks dispatch automatically. Default true (Hermes ships
    ``sdlc-review``); operators disable it for human-only review boards.
    """
    try:
        from hermes_cli.config import load_config
        return bool((load_config() or {}).get("kanban", {}).get("review_dispatch", True))
    except Exception:
        return True


# Memory-aware dispatch guard: an uncapped board once OOM'd a 1 GiB host. Two
# safeguards — a memory-DERIVED default cap when none is configured
# (``resolve_max_in_progress``) and a live memory-PRESSURE guard inside the
# tick (``_memory_pressure_level``) because a static cap can't see other
# tenants. Both fail open: non-Linux / read error → no cap / "unknown".

# Assumed per-worker footprint for the derived cap; deliberately conservative
# so the cap errs toward fewer workers on small VMs.
MEMORY_GUARD_MB_PER_WORKER = 512

# Derived default bounds: never below 2 (smallest VM must still progress),
# never above 8 (more fan-out must be explicit in config).
DERIVED_MAX_IN_PROGRESS_FLOOR = 2
DERIVED_MAX_IN_PROGRESS_CEILING = 8


def _system_memory_sample() -> dict:
    """Best-effort system memory snapshot (KiB values), ``{}`` when unknown.

    Local import keeps ``kanban_db`` importable without the gateway package.
    Module-level indirection is also the test seam — conftest patches this to
    ``{}`` so results don't depend on the CI runner's live memory.
    """
    try:
        from gateway.lifecycle_ledger import sample_memory
        return sample_memory() or {}
    except Exception:
        return {}


def derive_default_max_in_progress(sample: Optional[Mapping[str, Any]] = None) -> Optional[int]:
    """Memory-derived default for ``kanban.max_in_progress`` when unset:
    ``clamp(MemTotal / MEMORY_GUARD_MB_PER_WORKER, FLOOR, CEILING)``. Returns
    ``None`` (no cap) when total memory is unknown, so macOS/Windows dev
    machines are unaffected.
    """
    if sample is None:
        sample = _system_memory_sample()
    total_kib = sample.get("mem_total_kib")
    if isinstance(total_kib, bool) or not isinstance(total_kib, int) or total_kib <= 0:
        return None
    workers = (total_kib // 1024) // MEMORY_GUARD_MB_PER_WORKER
    return max(DERIVED_MAX_IN_PROGRESS_FLOOR, min(workers, DERIVED_MAX_IN_PROGRESS_CEILING))


def resolve_max_in_progress(configured: Optional[int]) -> Optional[int]:
    """Effective global concurrency cap: explicit config wins, else the
    memory-derived default. All config-parsing callers route through this so
    both paths agree.
    """
    if configured is not None:
        return configured
    return derive_default_max_in_progress()


def configured_max_in_progress() -> Optional[int]:
    """Read ``kanban.max_in_progress`` from config, or None when unset/invalid.

    Shared so every dispatch entry point agrees on "explicitly configured": a
    positive integer wins, anything else falls through to the derived default.
    """
    try:
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly() or {}).get("kanban", {}).get("max_in_progress")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        ival = int(raw)
    except (TypeError, ValueError):
        return None
    return ival if ival >= 1 else None


def count_running_tasks(conn: sqlite3.Connection) -> int:
    """Number of tasks in ``status='running'``.

    Used by the multi-board sweep to count OTHER boards' workers against the
    host-level budget — the memory-derived cap bounds the machine, not the
    board. Fails open to 0 so a broken board doesn't brick dispatch on healthy ones.
    """
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
            ).fetchone()[0]
        )
    except Exception:
        return 0


def count_running_tasks_other_boards(board: Optional[str] = None) -> int:
    """Total ``running`` tasks across every board EXCEPT ``board``.

    Caps bound the HOST, but each board's tick only sees its own DB; without
    this a derived cap of N gets multiplied by the number of active boards.
    Boards are matched by resolved DB path, so ``HERMES_KANBAN_DB`` (pins every
    board to one file) yields 0. Fails open per board.
    """
    try:
        current_path = str(_kb.kanban_db_path(board=board).expanduser().resolve())
    except Exception:
        current_path = None
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        return 0
    total = 0
    for meta in boards:
        slug = meta.get("slug") or _kb.DEFAULT_BOARD
        try:
            path = _kb.kanban_db_path(board=slug).expanduser()
            resolved = str(path.resolve())
            if current_path is not None and resolved == current_path:
                continue
            if not path.exists():
                continue
            other = _kbc.connect(board=slug)
            try:
                total += count_running_tasks(other)
            finally:
                with contextlib.suppress(Exception):
                    other.close()
        except Exception:
            continue
    return total


def _memory_pressure_level(sample: Optional[Mapping[str, Any]] = None) -> str:
    """Classify system memory pressure: ok/elevated/critical/unknown.

    Reuses :func:`gateway.memory_status.classify_pressure` so "critical" matches
    the dashboard banner and lifecycle-ledger OOM heuristics. ``unknown``
    (non-Linux, read failure) imposes no restriction — never brick dispatch
    where /proc is unavailable.
    """
    if sample is None:
        sample = _system_memory_sample()
    if not sample:
        return "unknown"
    try:
        from gateway.memory_status import classify_pressure
        return classify_pressure(sample.get("mem_available_kib"), sample.get("mem_total_kib"))
    except Exception:
        return "unknown"


_ADMISSION_ARGUMENT_MISSING = object()
_CANONICAL_PARALLEL_DISPATCH_KEY = "_canonical_parallel_dispatch"
MAX_ADMISSION_SKIP_DETAILS = 32
_ALLOCATION_THREAD_LOCK = threading.Lock()
_NATIVE_ADMISSION_THREAD_LOCK = threading.Lock()


def _native_admission_lock_identity_matches(lock_path: Path, handle: object) -> bool:
    """Verify that the locked descriptor still names the lock path itself."""
    try:
        path_info = lock_path.lstat()
        fd_info = os.fstat(handle.fileno())  # type: ignore[union-attr]
    except (OSError, ValueError, AttributeError):
        return False
    return (
        stat.S_ISREG(path_info.st_mode)
        and stat.S_ISREG(fd_info.st_mode)
        and path_info.st_dev == fd_info.st_dev
        and path_info.st_ino == fd_info.st_ino
    )


def _native_admission_lock_path_is_usable(lock_path: Path) -> bool:
    """Reject a symlink or non-regular admission lock before opening it."""
    try:
        path_info = lock_path.lstat()
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(path_info.st_mode)


@contextlib.contextmanager
def _native_admission_lock():
    """Serialize native host occupancy observation and claim/launch admission."""
    if not _NATIVE_ADMISSION_THREAD_LOCK.acquire(blocking=False):
        yield False
        return
    handle = None
    acquired = False
    admissible = False
    try:
        lock_path = _kb.kanban_home() / "kanban" / ".native-admission.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            if _native_admission_lock_path_is_usable(lock_path):
                handle = lock_path.open("a+b")
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
        except (OSError, AttributeError, ValueError):
            acquired = False
        if acquired:
            admissible = _native_admission_lock_identity_matches(lock_path, handle)
        yield bool(acquired and admissible)
    finally:
        try:
            if acquired and handle is not None:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, AttributeError, ValueError):
            pass
        if handle is not None:
            handle.close()
        _NATIVE_ADMISSION_THREAD_LOCK.release()


@contextlib.contextmanager
def _allocation_lock(*, required: bool = False):
    """Serialize host-wide adaptive occupancy observation and claims."""
    if not _ALLOCATION_THREAD_LOCK.acquire(blocking=False):
        yield False
        return
    handle = None
    acquired = False
    try:
        path = _kb.kanban_home() / "kanban" / ".allocation.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield True
    except (OSError, AttributeError, ValueError):
        yield bool(acquired and not required)
    finally:
        if acquired and handle is not None:
            with contextlib.suppress(OSError, AttributeError, ValueError):
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        if handle is not None:
            handle.close()
        _ALLOCATION_THREAD_LOCK.release()


@dataclass(frozen=True)
class _OtherBoardsRunningObservation:
    running_count: int
    has_independent_db: bool
    per_profile_running: Mapping[str, int] = field(default_factory=dict, compare=False)


def observe_running_tasks_other_boards(
    board: Optional[str] = None,
) -> Optional[_OtherBoardsRunningObservation]:
    """Read exact foreign occupancy and lock-domain evidence, or ``None``.

    A safety decision may use only a stable, immutable snapshot.  A missing,
    replacing, locked, malformed, or WAL-backed board therefore remains
    unknown rather than being treated as idle.
    """

    def _regular_snapshot(path: Path) -> tuple[Path, tuple[int, int, int, int]]:
        resolved = path.resolve(strict=True)
        info = os.lstat(resolved)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"kanban DB is not a regular file: {resolved}")
        return resolved, (int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns))

    def _wal_snapshot(db_path: Path) -> tuple[str, Optional[tuple[int, int, int, int]]]:
        wal_path = Path(f"{db_path}-wal")
        try:
            info = os.lstat(wal_path)
        except FileNotFoundError:
            return "absent", None
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"kanban WAL is not a regular file: {wal_path}")
        snapshot = (int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns))
        if snapshot[2] != 0:
            raise ValueError(f"kanban WAL is non-empty: {wal_path}")
        return "empty", snapshot

    def _same_snapshot(raw_path: Path, resolved_path: Path,
                       db_snapshot: tuple[int, int, int, int],
                       wal_snapshot: tuple[str, Optional[tuple[int, int, int, int]]]) -> bool:
        try:
            after_path, after_db = _regular_snapshot(raw_path)
            after_wal = _wal_snapshot(after_path)
        except (OSError, ValueError):
            return False
        return after_path == resolved_path and after_db == db_snapshot and after_wal == wal_snapshot

    try:
        current_raw = _kb.kanban_db_path(board=board).expanduser()
        current_path, current_snapshot = _regular_snapshot(current_raw)
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        return None

    total = 0
    per_profile: dict[str, int] = {}
    foreign_paths: set[Path] = set()
    foreign_identities: set[tuple[int, int]] = set()
    observations: list[tuple[Path, Path, tuple[int, int, int, int],
                              tuple[str, Optional[tuple[int, int, int, int]]]]] = []
    for metadata in boards:
        if not isinstance(metadata, dict):
            return None
        slug = metadata.get("slug") or _kb.DEFAULT_BOARD
        try:
            raw_path = _kb.kanban_db_path(board=slug).expanduser()
            path, db_snapshot = _regular_snapshot(raw_path)
            identity = (db_snapshot[0], db_snapshot[1])
            if path == current_path or identity == (current_snapshot[0], current_snapshot[1]):
                continue
            if path in foreign_paths or identity in foreign_identities:
                continue
            wal_snapshot = _wal_snapshot(path)
            foreign_paths.add(path)
            foreign_identities.add(identity)
            observations.append((raw_path, path, db_snapshot, wal_snapshot))
            other = sqlite3.connect(path.as_uri() + "?immutable=1", uri=True, timeout=0.5)
            try:
                rows = other.execute(
                    "SELECT assignee, COUNT(*) FROM tasks "
                    "WHERE status = 'running' GROUP BY assignee"
                ).fetchall()
                board_total = 0
                for row in rows:
                    if row is None or len(row) < 2:
                        return None
                    assignee, raw_count = row[0], row[1]
                    if type(raw_count) is not int or raw_count < 0:
                        return None
                    if assignee is not None and (type(assignee) is not str or not assignee.strip()):
                        return None
                    board_total += raw_count
                    if assignee is not None:
                        per_profile[assignee] = per_profile.get(assignee, 0) + raw_count
                total += board_total
            finally:
                other.close()
            if not _same_snapshot(raw_path, path, db_snapshot, wal_snapshot):
                return None
        except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
            return None
    for raw_path, path, db_snapshot, wal_snapshot in observations:
        if not _same_snapshot(raw_path, path, db_snapshot, wal_snapshot):
            return None
    try:
        current_after, current_after_snapshot = _regular_snapshot(current_raw)
    except (OSError, ValueError):
        return None
    if current_after != current_path or current_after_snapshot != current_snapshot:
        return None
    return _OtherBoardsRunningObservation(
        running_count=total,
        has_independent_db=bool(foreign_paths),
        per_profile_running=MappingProxyType(dict(per_profile)),
    )


def _read_live_worker_scopes() -> dict[str, int]:
    """Read systemd worker-scope occupancy for adaptive admission."""
    states = ("active", "activating", "deactivating")
    proc = subprocess.run(
        ["systemctl", "--user", "list-units", "hermes-kanban-worker-*.scope",
         "--type=scope", f"--state={','.join(states)}", "--plain", "--no-legend", "--no-pager"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=3, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("could not list live worker scopes")
    counts = {state: 0 for state in states}
    for line in (proc.stdout or "").splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) < 3 or not fields[0].startswith("hermes-kanban-worker-"):
            raise RuntimeError("live worker scope telemetry was malformed")
        state = fields[2]
        if state not in counts:
            raise RuntimeError("live worker scope telemetry was malformed")
        counts[state] += 1
    counts["total"] = sum(counts.values())
    return counts


def _normalize_worker_toolset_override(toolsets: Optional[Iterable[str]]) -> Optional[list[str]]:
    if toolsets is None:
        return None
    if isinstance(toolsets, str):
        toolsets = toolsets.split(",")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in toolsets:
        name = str(value or "").strip()
        if name and name.casefold() not in seen:
            normalized.append(name)
            seen.add(name.casefold())
    return normalized


def _positive_dispatch_cap(value: Any, name: str) -> Optional[int]:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_dispatch_cap(value: Any, name: str) -> Optional[int]:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def resolve_dispatch_caps(
    config: Optional[Mapping[str, Any]] = None, *, max_spawn: Any = None,
    max_in_progress: Any = None,
) -> tuple[Optional[int], Optional[int]]:
    configured_spawn = configured_progress = None
    if config is not None:
        if not isinstance(config, Mapping):
            raise ValueError("effective Hermes config must be a mapping")
        section = config.get("kanban", {}) or {}
        if not isinstance(section, Mapping):
            raise ValueError("kanban config must be a mapping")
        configured_spawn = _nonnegative_dispatch_cap(section.get("max_spawn"), "kanban.max_spawn")
        configured_progress = _positive_dispatch_cap(section.get("max_in_progress"), "kanban.max_in_progress")
    explicit_spawn = _nonnegative_dispatch_cap(max_spawn, "max_spawn") if max_spawn is not None else None
    explicit_progress = _positive_dispatch_cap(max_in_progress, "max_in_progress") if max_in_progress is not None else None
    return (
        min(configured_spawn, explicit_spawn) if configured_spawn is not None and explicit_spawn is not None
        else explicit_spawn if explicit_spawn is not None else configured_spawn,
        min(configured_progress, explicit_progress) if configured_progress is not None and explicit_progress is not None
        else explicit_progress if explicit_progress is not None else configured_progress,
    )


def _parallel_dispatch_required(
    config: Optional[Mapping[str, Any]] = None, *, max_spawn: Any = None,
    max_in_progress: Any = None,
) -> bool:
    section = config.get("kanban", {}) if isinstance(config, Mapping) else {}
    canonical = isinstance(section, Mapping) and section.get(_CANONICAL_PARALLEL_DISPATCH_KEY) is True
    configured_progress = resolve_dispatch_caps(config)[1]
    explicit_progress = _positive_dispatch_cap(max_in_progress, "max_in_progress") if max_in_progress is not None else None
    return bool(canonical or (configured_progress is not None and configured_progress > 1) or (explicit_progress is not None and explicit_progress > 1))


def validate_allowed_worker_profiles(value: Any) -> Optional[list[str]]:
    if value is None:
        return None
    if type(value) is not list or not value:
        raise ValueError("kanban.safe_dispatch_admission.allowed_worker_profiles must be a non-empty list")
    try:
        from hermes_cli.profiles import profile_exists, validate_profile_name
    except Exception as exc:
        raise ValueError("could not validate allowed worker profiles") from exc
    profiles: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if type(item) is not str or not item or item != item.strip():
            raise ValueError(f"kanban.safe_dispatch_admission.allowed_worker_profiles [{index}] must be a canonical profile name")
        try:
            validate_profile_name(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"kanban.safe_dispatch_admission.allowed_worker_profiles [{index}] is invalid: {exc}") from exc
        if item in seen:
            raise ValueError(f"kanban.safe_dispatch_admission.allowed_worker_profiles must not contain duplicates: {item!r}")
        if not profile_exists(item):
            raise ValueError(f"kanban.safe_dispatch_admission.allowed_worker_profiles names missing profile {item!r}")
        profiles.append(item)
        seen.add(item)
    return profiles


def _admission_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    section = config.get("kanban", {}) or {}
    if not isinstance(section, Mapping):
        raise ValueError("kanban config must be a mapping")
    return section


def _admission_allowlist(config: Mapping[str, Any]) -> Any:
    section = _admission_section(config)
    if "safe_dispatch_admission" not in section:
        return _ADMISSION_ARGUMENT_MISSING
    policy = section["safe_dispatch_admission"]
    if not isinstance(policy, Mapping):
        raise ValueError("kanban.safe_dispatch_admission must be a mapping")
    return policy.get("allowed_worker_profiles", _ADMISSION_ARGUMENT_MISSING)


def _admission_snapshot(config: Mapping[str, Any]) -> dict[str, Any]:
    section = _admission_section(config)
    snapshot: dict[str, Any] = {}
    for key in ("max_spawn", "max_in_progress", "max_in_progress_per_profile", _CANONICAL_PARALLEL_DISPATCH_KEY, "codex_host_router"):
        if key in section:
            snapshot[key] = copy.deepcopy(section[key])
    policy = section.get("safe_dispatch_admission", _ADMISSION_ARGUMENT_MISSING)
    if policy is not _ADMISSION_ARGUMENT_MISSING:
        if not isinstance(policy, Mapping):
            raise ValueError("kanban.safe_dispatch_admission must be a mapping")
        snapshot["safe_dispatch_admission"] = {}
        if "allowed_worker_profiles" in policy:
            snapshot["safe_dispatch_admission"]["allowed_worker_profiles"] = copy.deepcopy(policy["allowed_worker_profiles"])
    return {"kanban": snapshot}


@dataclass(frozen=True)
class _CanonicalAdmissionSnapshot(Mapping[str, Any]):
    _config: Mapping[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self._config[key]

    def __iter__(self):
        return iter(self._config)

    def __len__(self) -> int:
        return len(self._config)


def _freeze_admission_config(config: Mapping[str, Any]) -> _CanonicalAdmissionSnapshot:
    section = dict(_admission_section(config))
    policy = section.get("safe_dispatch_admission")
    if isinstance(policy, Mapping):
        policy = dict(policy)
        if isinstance(policy.get("allowed_worker_profiles"), list):
            policy["allowed_worker_profiles"] = tuple(policy["allowed_worker_profiles"])
        section["safe_dispatch_admission"] = MappingProxyType(policy)
    return _CanonicalAdmissionSnapshot(MappingProxyType({"kanban": MappingProxyType(section)}))


def _canonical_dispatch_config(
    effective_config: Optional[Mapping[str, Any]], *, max_spawn: Any = None,
    max_in_progress: Any = None,
) -> Optional[Mapping[str, Any]]:
    if isinstance(effective_config, _CanonicalAdmissionSnapshot):
        return effective_config
    if effective_config is not None and not isinstance(effective_config, Mapping):
        raise ValueError("effective Hermes config must be a mapping")
    explicit_arg_progress = _positive_dispatch_cap(max_in_progress, "max_in_progress") if max_in_progress is not None else None
    config_path = _kb.kanban_home() / "config.yaml"
    try:
        info = config_path.lstat()
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise ValueError("could not access canonical Hermes config") from exc
    if info is None:
        explicit = _admission_snapshot(effective_config) if effective_config is not None else None
        _spawn, progress = resolve_dispatch_caps(explicit)
        if (explicit_arg_progress is not None and explicit_arg_progress > 1) or (progress is not None and progress > 1) or (explicit is not None and _admission_allowlist(explicit) is not _ADMISSION_ARGUMENT_MISSING):
            raise ValueError("canonical Hermes config is required for adaptive or parallel-capable Kanban dispatch")
        return _freeze_admission_config(explicit) if explicit is not None else None
    if not stat.S_ISREG(info.st_mode) or config_path.is_symlink() or not os.access(config_path, os.R_OK):
        raise ValueError("canonical Hermes config is not a regular file")
    fingerprint = (int(info.st_dev), int(info.st_ino), int(info.st_mode), int(info.st_size), int(info.st_mtime_ns))
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli.config import load_config_readonly
        token = set_hermes_home_override(_kb.kanban_home())
        try:
            canonical_raw = load_config_readonly()
            import hermes_cli.config as config_module
            marker = (str(config_path), info.st_mtime_ns, info.st_size)
            if marker in getattr(config_module, "_CONFIG_PARSE_WARNED", set()):
                raise ValueError("canonical Hermes config could not be parsed")
        finally:
            reset_hermes_home_override(token)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("could not load canonical Hermes config") from exc
    try:
        post = config_path.lstat()
    except OSError as exc:
        raise ValueError("canonical Hermes config changed while loading") from exc
    post_fingerprint = (int(post.st_dev), int(post.st_ino), int(post.st_mode), int(post.st_size), int(post.st_mtime_ns))
    if post_fingerprint != fingerprint or not stat.S_ISREG(post.st_mode) or config_path.is_symlink():
        raise ValueError("canonical Hermes config changed while loading")
    if not isinstance(canonical_raw, Mapping):
        raise ValueError("effective Hermes config must be a mapping")
    canonical = _admission_snapshot(canonical_raw)
    canonical_spawn, canonical_progress = resolve_dispatch_caps(canonical)
    section = dict(_admission_section(canonical))
    canonical_per_profile = _positive_dispatch_cap(section.get("max_in_progress_per_profile"), "kanban.max_in_progress_per_profile")
    value = _admission_allowlist(canonical)
    allowed = validate_allowed_worker_profiles(value) if value is not _ADMISSION_ARGUMENT_MISSING else None
    policy_present = "safe_dispatch_admission" in section
    if policy_present and canonical_progress is None:
        canonical_progress = _kb.resolve_max_in_progress(None)
        if canonical_progress is not None:
            section["max_in_progress"] = canonical_progress
    authorized = bool(policy_present or (canonical_progress is not None and canonical_progress > 1))
    if authorized and allowed is None:
        raise ValueError("canonical kanban.safe_dispatch_admission.allowed_worker_profiles is required when parallel dispatch safety is active")
    section[_CANONICAL_PARALLEL_DISPATCH_KEY] = authorized
    canonical["kanban"] = section
    if effective_config is None:
        if not authorized and explicit_arg_progress is not None and explicit_arg_progress > 1:
            raise ValueError("effective_config cannot enable adaptive or parallel-capable Kanban dispatch without canonical shared-root policy")
        return _freeze_admission_config(canonical)
    explicit = _admission_snapshot(effective_config)
    explicit_spawn, explicit_progress = resolve_dispatch_caps(explicit)
    explicit_section = _admission_section(explicit)
    explicit_per_profile = _positive_dispatch_cap(explicit_section.get("max_in_progress_per_profile"), "kanban.max_in_progress_per_profile")
    explicit_value = _admission_allowlist(explicit)
    if not authorized and ((explicit_arg_progress is not None and explicit_arg_progress > 1) or (explicit_progress is not None and explicit_progress > 1) or "safe_dispatch_admission" in explicit_section):
        raise ValueError("effective_config cannot enable adaptive or parallel-capable Kanban dispatch without canonical shared-root policy")
    explicit_allowed = validate_allowed_worker_profiles(explicit_value) if explicit_value is not _ADMISSION_ARGUMENT_MISSING else None
    if allowed is not None and explicit_allowed is not None:
        widened = sorted(set(explicit_allowed) - set(allowed))
        if widened:
            raise ValueError(f"effective_config cannot widen canonical kanban.safe_dispatch_admission policy: {widened}")
    resolved = {"kanban": dict(section)}
    for key, canonical_value, explicit_value in (("max_spawn", canonical_spawn, explicit_spawn), ("max_in_progress", canonical_progress, explicit_progress), ("max_in_progress_per_profile", canonical_per_profile, explicit_per_profile)):
        value = min(canonical_value, explicit_value) if canonical_value is not None and explicit_value is not None else explicit_value if explicit_value is not None else canonical_value
        if value is not None:
            resolved["kanban"][key] = value
    if allowed is not None or explicit_allowed is not None:
        resolved["kanban"]["safe_dispatch_admission"] = {"allowed_worker_profiles": explicit_allowed if explicit_allowed is not None else allowed}
    return _freeze_admission_config(resolved)


def resolve_worker_profile_admission(
    config: Optional[Mapping[str, Any]] = None, *, max_spawn: Any = None,
    max_in_progress: Any = None, allowed_worker_profiles: Any = _ADMISSION_ARGUMENT_MISSING,
) -> Optional[list[str]]:
    configured_value = _admission_allowlist(config) if isinstance(config, Mapping) else _ADMISSION_ARGUMENT_MISSING
    canonical_snapshot = isinstance(config, _CanonicalAdmissionSnapshot) and _admission_section(config).get(_CANONICAL_PARALLEL_DISPATCH_KEY) is True
    if (configured_value is not _ADMISSION_ARGUMENT_MISSING and not canonical_snapshot) or (allowed_worker_profiles is not _ADMISSION_ARGUMENT_MISSING and allowed_worker_profiles is not None and not canonical_snapshot):
        raise ValueError("worker-profile admission cannot enable adaptive or parallel-capable Kanban dispatch without canonical policy")
    configured = validate_allowed_worker_profiles(list(configured_value) if isinstance(configured_value, tuple) else configured_value) if configured_value is not _ADMISSION_ARGUMENT_MISSING else None
    explicit = validate_allowed_worker_profiles(allowed_worker_profiles) if allowed_worker_profiles is not _ADMISSION_ARGUMENT_MISSING and allowed_worker_profiles is not None else None
    if configured is not None and explicit is not None:
        widened = sorted(set(explicit) - set(configured))
        if widened:
            raise ValueError(f"explicit allowed_worker_profiles cannot widen configured kanban.safe_dispatch_admission policy: {widened}")
        profiles = explicit
    else:
        profiles = explicit or configured
    if _parallel_dispatch_required(config, max_spawn=max_spawn, max_in_progress=max_in_progress) and configured is None:
        raise ValueError("canonical kanban.safe_dispatch_admission.allowed_worker_profiles is required when parallel dispatch safety is active")
    return profiles


def prepare_dispatch_admission(
    effective_config: Optional[Mapping[str, Any]] = None, *, max_spawn: Any = None,
    max_in_progress: Any = None, max_in_progress_per_profile: Any = None,
    allowed_worker_profiles: Any = _ADMISSION_ARGUMENT_MISSING,
) -> Optional[Mapping[str, Any]]:
    snapshot = _canonical_dispatch_config(effective_config, max_spawn=max_spawn, max_in_progress=max_in_progress)
    if effective_config is not None:
        _positive_dispatch_cap(_admission_section(effective_config).get("max_in_progress_per_profile"), "kanban.max_in_progress_per_profile")
    _positive_dispatch_cap(max_in_progress_per_profile, "max_in_progress_per_profile")
    resolved_spawn, resolved_progress = resolve_dispatch_caps(snapshot, max_spawn=max_spawn, max_in_progress=max_in_progress)
    resolve_worker_profile_admission(snapshot, max_spawn=resolved_spawn, max_in_progress=resolved_progress, allowed_worker_profiles=allowed_worker_profiles)
    return snapshot


def _record_worker_profile_admission_skip(result: DispatchResult, task_id: str, assignee: str) -> None:
    result.skipped_worker_profile_not_allowed_total += 1
    if len(result.skipped_worker_profile_not_allowed) < MAX_ADMISSION_SKIP_DETAILS:
        result.skipped_worker_profile_not_allowed.append((task_id, assignee))
    else:
        result.skipped_worker_profile_not_allowed_truncated = True


def _record_worker_capability_rejection(result: DispatchResult, diagnostic: Optional[Mapping[str, Any]]) -> None:
    if not diagnostic:
        return
    task_id = str(diagnostic.get("task_id") or "")
    if not any(str(item.get("task_id") or "") == task_id for item in result.capability_rejections):
        result.capability_rejections.append(dict(diagnostic))


def _capability_admitted(
    conn: sqlite3.Connection, row: sqlite3.Row, assignee: Optional[str],
    result: DispatchResult, *, worker_toolsets: Optional[Iterable[str]] = None,
) -> bool:
    """Reject an explicit capability miss before claim or spawn budget use."""
    task = _kb.get_task(conn, str(row["id"]))
    diagnostic = (
        _worker_capabilities_for_task(
            task, assignee=assignee, toolsets_override=worker_toolsets,
        )
        if task is not None else None
    )
    if diagnostic is None:
        return True
    _record_worker_capability_rejection(result, diagnostic)
    return False


def _resolve_worker_capability_tools(assignee: str, *, toolsets_override: Optional[Iterable[str]] = None) -> Optional[set[str]]:
    names = _normalize_worker_toolset_override(toolsets_override)
    if names is None:
        try:
            from hermes_cli.profiles import resolve_profile_env
            profile_home = resolve_profile_env(str(assignee))
        except Exception:
            try:
                root = Path(_kb.kanban_home())
                profile_home = str(root if str(assignee).casefold() == "default" else root / "profiles" / str(assignee).strip().lower())
            except Exception:
                return None
        names = _kb._resolve_worker_cli_toolsets(profile_home)
    if names is None:
        return None
    try:
        from toolsets import resolve_toolset
    except Exception:
        return None
    tools: set[str] = set()
    for name in names:
        tools.add(str(name).strip().casefold())
        with contextlib.suppress(Exception):
            tools.update(str(tool).casefold() for tool in resolve_toolset(str(name).strip()))
    return tools


def _worker_capabilities_for_task(task: "Task", *, assignee: Optional[str] = None, toolsets_override: Optional[Iterable[str]] = None) -> Optional[dict[str, Any]]:
    try:
        required = _kb.normalize_required_worker_capabilities(task.required_capabilities)
    except ValueError as exc:
        return {"task_id": task.id, "assignee": assignee or task.assignee, "reason": "invalid_required_capabilities", "reason_code": "invalid_required_capabilities", "missing_capabilities": sorted(str(value) for value in (task.required_capabilities or [])), "required_capabilities": sorted(str(value) for value in (task.required_capabilities or [])), "available_capabilities": [], "detail": str(exc)}
    if not required:
        return None
    candidate = str(assignee or task.assignee or "").strip()
    tools = _resolve_worker_capability_tools(candidate, toolsets_override=toolsets_override) if candidate else None
    if tools is None:
        return {"task_id": task.id, "assignee": candidate or None, "reason": "missing_capabilities", "reason_code": "worker_toolsets_unresolved", "missing_capabilities": list(required), "required_capabilities": list(required), "available_capabilities": []}
    available: set[str] = set()
    if {"terminal", "process_manage"} & tools:
        available.update({"terminal", "local_file_hash"})
    if {"read_file", "search_files"} & tools:
        available.add("local_file_read")
    if {"kanban_attach", "kanban_attach_url"} & tools:
        available.add("task_attachment_write")
    if task.workspace_kind in _kb.VALID_WORKSPACE_KINDS and ({"terminal", "process_manage", "read_file", "search_files"} & tools):
        available.add("workspace_access")
    missing = sorted(set(required) - available)
    if not missing:
        return None
    return {"task_id": task.id, "assignee": candidate or None, "reason": "missing_capabilities", "reason_code": "missing_worker_capabilities", "missing_capabilities": missing, "required_capabilities": list(required), "available_capabilities": sorted(available)}


def dispatch_once(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_new_spawns: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    allowed_worker_profiles: Optional[Iterable[str]] = None,
    worker_toolsets: Optional[Iterable[str]] = None,
    effective_config: Optional[Mapping[str, Any]] = None,
    selected_boards: Optional[Iterable[str]] = None,
    reconcile_orphans: bool = True,
) -> DispatchResult:
    """Run one dispatcher tick under the board's single-writer lock.

    Wraps :func:`_dispatch_once_locked` in the non-blocking :func:`_dispatch_tick_lock`
    so two dispatchers on one ``kanban.db`` never race a write tick on WAL
    frames. The loser returns an empty ``DispatchResult`` with
    ``skipped_locked=True`` and writes nothing; the lock is keyed on the
    resolved DB path so unrelated boards tick in parallel.
    """
    # Resolve through the facade seam so native callers and tests can provide
    # the already-admitted snapshot without bypassing dispatch ownership.
    effective_config = _kb.prepare_dispatch_admission(
        effective_config,
        max_spawn=max_spawn,
        max_in_progress=max_in_progress,
        max_in_progress_per_profile=max_in_progress_per_profile,
        allowed_worker_profiles=allowed_worker_profiles,
    )
    max_spawn, max_in_progress = resolve_dispatch_caps(
        effective_config, max_spawn=max_spawn, max_in_progress=max_in_progress,
    )
    configured_per_profile = None
    if effective_config is not None:
        configured_per_profile = _positive_dispatch_cap(
            _admission_section(effective_config).get("max_in_progress_per_profile"),
            "kanban.max_in_progress_per_profile",
        )
    explicit_per_profile = _positive_dispatch_cap(
        max_in_progress_per_profile, "max_in_progress_per_profile"
    )
    max_in_progress_per_profile = (
        min(configured_per_profile, explicit_per_profile)
        if configured_per_profile is not None and explicit_per_profile is not None
        else explicit_per_profile if explicit_per_profile is not None
        else configured_per_profile
    )
    allowed_worker_profiles = resolve_worker_profile_admission(
        effective_config,
        max_spawn=max_spawn,
        max_in_progress=max_in_progress,
        allowed_worker_profiles=allowed_worker_profiles,
    )
    worker_toolsets = _normalize_worker_toolset_override(worker_toolsets)
    if max_new_spawns is not None and (type(max_new_spawns) is not int or max_new_spawns < 0):
        raise ValueError("max_new_spawns must be a non-negative integer or None")
    parallel_dispatch = _parallel_dispatch_required(
        effective_config, max_spawn=max_spawn, max_in_progress=max_in_progress,
    )
    if parallel_dispatch:
        max_new_spawns = 1 if max_new_spawns is None else min(max_new_spawns, 1)
    if dry_run:
        result = _dispatch_preview(
            conn,
            failure_limit=failure_limit,
            max_spawn=max_spawn,
            max_new_spawns=max_new_spawns,
            max_in_progress=max_in_progress,
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
            allowed_worker_profiles=allowed_worker_profiles,
            worker_toolsets=worker_toolsets,
        )
        # Dry-run previews never claim/spawn work, but terminal worker cleanup
        # is lifecycle maintenance rather than dispatch mutation and remains
        # active just as it did on the pre-admission path.
        _kb.reconcile_worker_scope_terminals(conn)
        result.reaped_terminal_workers = reap_terminal_workers(conn)
        return result

    native_spawn = (
        spawn_fn is None
        or spawn_fn is _default_spawn
        or spawn_fn is _kb._default_spawn
    )

    def _locked_tick(*, native_admission_held: bool) -> DispatchResult:
        return _dispatch_once_locked(
            conn, spawn_fn=spawn_fn, ttl_seconds=ttl_seconds, dry_run=dry_run,
            max_spawn=max_spawn, max_new_spawns=max_new_spawns,
            max_in_progress=max_in_progress, failure_limit=failure_limit,
            stale_timeout_seconds=stale_timeout_seconds, board=board,
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
            allowed_worker_profiles=allowed_worker_profiles,
            worker_toolsets=worker_toolsets, selected_boards=selected_boards,
            effective_config=effective_config, parallel_dispatch=parallel_dispatch,
            reconcile_orphans=reconcile_orphans,
            _native_admission_held=native_admission_held,
        )

    native_scope = (
        _kb._native_admission_lock()
        if native_spawn
        else contextlib.nullcontext(True)
    )
    with native_scope as native_admission_held:
        if not native_admission_held:
            result = DispatchResult(
                admission_blocked=True,
                admission_reason="native_admission_lock_unavailable",
            )
        else:
            try:
                db_path = _kb.kanban_db_path(board=board)
            except Exception as exc:
                # DB identity is an admission prerequisite.  Never tick or
                # spawn against an unverified path, and do not checkpoint WAL.
                result = DispatchResult(
                    admission_blocked=True,
                    admission_reason="db_path_unavailable",
                    admission_metrics={"error": f"{type(exc).__name__}: {exc}"},
                )
                _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
                return result
            allocation_scope = (
                _kb._allocation_lock(required=True)
                if parallel_dispatch else contextlib.nullcontext(True)
            )
            with allocation_scope as allocation_held:
                if not allocation_held:
                    result = DispatchResult(skipped_locked=True)
                else:
                    with _kbc._dispatch_tick_lock(db_path) as held:
                        if not held:
                            result = DispatchResult(skipped_locked=True)
                        else:
                            result = _locked_tick(
                                native_admission_held=native_admission_held,
                            )
                            _kbc._maybe_checkpoint_wal(conn, db_path)
    # Lock released. Fire the tick observer strictly OUTSIDE the critical
    # section: a slow subscriber must never stall a sibling dispatcher's tick.
    _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
    return result


def _call_spawn_fn(
    spawn_fn, task: Task, workspace: str, board: Optional[str], *,
    worker_toolsets: Optional[Iterable[str]] = None,
    require_scope: bool = False,
    scope_config: Optional[_WorkerScopeConfig] = None,
    launch_intent_fn=None,
    clear_launch_intent_fn=None,
) -> Optional[int]:
    """Back-compat: older spawn_fn signatures (and test stubs) accept only
    ``(task, workspace)``; pass ``board`` only when the callable supports it."""
    import inspect
    try:
        sig = inspect.signature(spawn_fn)
        kwargs: dict[str, Any] = {}
        if "board" in sig.parameters:
            kwargs["board"] = board
        if "worker_toolsets" in sig.parameters:
            kwargs["worker_toolsets"] = worker_toolsets
        if "require_scope" in sig.parameters:
            kwargs["require_scope"] = require_scope
        if "scope_config" in sig.parameters:
            kwargs["scope_config"] = scope_config
        if "launch_intent_fn" in sig.parameters:
            kwargs["launch_intent_fn"] = launch_intent_fn
        if "clear_launch_intent_fn" in sig.parameters:
            kwargs["clear_launch_intent_fn"] = clear_launch_intent_fn
        return spawn_fn(task, workspace, **kwargs)
    except (TypeError, ValueError):
        return spawn_fn(task, workspace)


def _dispatch_lane_task(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    assignee: str,
    result: "DispatchResult",
    *,
    lane: str,
    dry_run: bool,
    ttl_seconds: Optional[int],
    board: Optional[str],
    failure_limit: int,
    spawn_fn,
    per_profile_cap: Optional[int],
    per_profile_running: dict[str, int],
    worker_toolsets: Optional[Iterable[str]] = None,
    require_scope: bool = False,
    scope_config: Optional[_WorkerScopeConfig] = None,
) -> bool:
    """Guard, claim, resolve the workspace and spawn one ready/review row.
    Returns True when a spawn slot was consumed (real or ``dry_run``); every
    skip is recorded on ``result``.
    """
    task_id = row["id"]
    candidate = _kb.get_task(conn, task_id)
    diagnostic = (
        _worker_capabilities_for_task(
            candidate, assignee=assignee, toolsets_override=worker_toolsets,
        )
        if candidate is not None else None
    )
    if diagnostic is not None:
        _record_worker_capability_rejection(result, diagnostic)
        return False
    # Non-profile assignees (control-plane lanes that pull via ``claim_task``)
    # would fail ``hermes -p <assignee>`` at startup and loop ready→crash→ready
    # forever. Bucketed apart from skipped_unassigned: the operator cannot fix
    # it by assigning a profile, and health telemetry suppresses "stuck" for it.
    profile_exists = _profile_exists_fn()
    if profile_exists is not None and not profile_exists(assignee):
        result.skipped_nonspawnable.append(task_id)
        return False
    # Per-profile cap: one profile's local model / API quota / browser pool
    # must not be overwhelmed by a fan-out even with global headroom.
    if per_profile_cap is not None:
        current = per_profile_running.get(assignee, 0)
        if current >= per_profile_cap:
            result.skipped_per_profile_capped.append((task_id, assignee, current))
            return False
    guard_reason = check_respawn_guard(conn, task_id, lane=lane)
    if guard_reason is not None:
        result.respawn_guarded.append((task_id, guard_reason))
        # Event so ``hermes kanban tail`` shows why the task looks stuck.
        # Honour kanban.default_assignee: when the dispatcher hits an unassigned ready task and an
        # operator-configured fallback exists, persist the assignment and proceed. This removes the
        # dashboard footgun where a task created without an assignee parks in 'ready' forever even though
        # the operator's intent ("default") was perfectly clear (#27145). Mutating the row (not just the
        # in-memory view) keeps diagnostics and the board state consistent: the task is now legitimately
        # owned by ``kanban.default_assignee``, not "unassigned but secretly routed".
        if not dry_run:
            with _kb.write_txn(conn):
                _kb._append_event(conn, task_id, "respawn_guarded", {"reason": guard_reason})
        return False

    def _count_spawn(name: str) -> None:
        # Later rows in this tick respect the per-profile cap; subsequent
        # ticks re-query from the DB.
        if per_profile_cap is not None and name:
            per_profile_running[name] = per_profile_running.get(name, 0) + 1

    if dry_run:
        result.spawned.append((task_id, assignee, ""))
        _count_spawn(assignee)
        return True
    claim = _kb.claim_review_task if lane == "review" else _kb.claim_task
    claimed = claim(conn, task_id, ttl_seconds=ttl_seconds)
    if claimed is None:
        return False
    try:
        resolved_branch_name = None
        if claimed.workspace_kind == "worktree":
            workspace, resolved_branch_name = _kbw._resolve_worktree_workspace(claimed, board=board)
        else:
            workspace = _kbw.resolve_workspace(claimed, board=board)
    except Exception as exc:
        if _record_task_failure(
            conn, claimed.id, f"workspace: {exc}",
            outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
        ):
            result.auto_blocked.append(claimed.id)
        return False
    _kbw.set_workspace_path(conn, claimed.id, str(workspace))
    if claimed.workspace_kind == "worktree":
        _kbw.set_branch_name(conn, claimed.id, resolved_branch_name or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
    _kbw._maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
    if lane == "review":
        # Force-load sdlc-review; the kanban lifecycle is already in every
        # worker's system prompt via KANBAN_GUIDANCE.
        claimed.skills = list(dict.fromkeys([*(claimed.skills or []), "sdlc-review"]))
    try:
        effective_spawn = spawn_fn if spawn_fn is not None else _kb._default_spawn
        launch_config = scope_config
        if launch_config is None and effective_spawn is _kb._default_spawn:
            with contextlib.suppress(Exception):
                launch_config = _kb._worker_scope_config()
        pid = _call_spawn_fn(
            effective_spawn, claimed, str(workspace), board,
            worker_toolsets=worker_toolsets, require_scope=require_scope,
            scope_config=launch_config,
            launch_intent_fn=(
                lambda unit, target, config: _kb._set_worker_launching(
                    conn, claimed.id, scope_unit=unit, target=target, scope_config=config,
                )
            ) if launch_config is not None else None,
            clear_launch_intent_fn=(
                lambda: _kb._clear_worker_launching(conn, claimed.id)
            ) if launch_config is not None else None,
        )
        if pid:
            # Custom spawn hooks may return a verified scoped receipt without
            # calling the native launch-intent callback.  Backfill the exact
            # launching fence before promoting the receipt so a persistence
            # failure can never fall back to host-PID termination.
            _kb._ensure_worker_launch_identity(conn, claimed.id, pid)
            # Persist through the facade seam so host callers can fence the
            # launch receipt atomically (and a failed receipt cannot fall
            # back to host-PID cleanup).
            _kb._set_worker_pid(conn, claimed.id, pid)
        # Fires AFTER the PID (when reported) is durably persisted. Best-effort.
        _kb._fire_worker_spawned_hook(conn, claimed, str(workspace), pid, board=board)
        # consecutive_failures is deliberately NOT reset here: resetting on
        # spawn would let a task that keeps timing out loop forever. Cleared
        # only on successful completion (complete_task).
        result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
        _count_spawn(claimed.assignee)
        return True
    except Exception as exc:
        from tools.process_registry import RestartSafeScopeUnavailable

        # The host refused the spawn (no restart-safe scope): nothing about the
        # card ran, so it must not spend the card's retry budget (#114720).
        infrastructure = isinstance(exc, RestartSafeScopeUnavailable)
        if infrastructure:
            _kb._log.warning("kanban dispatcher: spawn of %s deferred, host cannot place the worker: %s", claimed.id, exc)
        if pid is None and isinstance(exc, _kb._WorkerScopeLaunchError):
            pid = exc.launch
        if pid is not None and not _kb._abort_unpersisted_worker_launch(pid):
            _kb._mark_worker_launch_cleanup_pending(
                conn, claimed.id, pid, str(exc),
            )
            return False
        if _record_task_failure(
            conn, claimed.id, str(exc),
            outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
            infrastructure=infrastructure,
        ):
            result.auto_blocked.append(claimed.id)
        return False


def _connection_db_path(conn: sqlite3.Connection, board: Optional[str]) -> Path:
    """Resolve the already-open connection's canonical file for a supervisor."""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main" and row[2]:
            return Path(row[2]).expanduser()
    return _kb.kanban_db_path(board=board)


def _try_remote_codex_when_full(
    conn: sqlite3.Connection,
    ready_rows: Iterable[sqlite3.Row],
    result: DispatchResult,
    *,
    effective_config: Optional[Mapping[str, Any]],
    board: Optional[str],
) -> None:
    """Attempt bounded remote Codex admission after local capacity is full."""
    try:
        from hermes_cli.kanban_codex_host import (
            load_host_router_config, prepare_route, select_route,
            launch_supervisor, task_is_eligible,
        )
        cfg = load_host_router_config(effective_config)
    except Exception:
        if effective_config is not None:
            result.remote_deferred.append(("*", "router_config_invalid"))
        return
    if not cfg.enabled:
        return

    active_remote = count_active_remote_supervisors(conn)
    routed = 0
    for row in ready_rows:
        if routed >= cfg.max_routes_per_tick or active_remote >= cfg.max_total_routes:
            break
        task = _kb.get_task(conn, row["id"])
        if task is None or not task_is_eligible(task, cfg):
            continue
        selection = select_route(cfg, task_id=task.id, assignee=task.assignee or "")
        route = selection.get("route")
        if route in {"local_codex", "defer"}:
            if route == "defer":
                result.remote_deferred.append(
                    (task.id, str(selection.get("reason", "defer"))[:160])
                )
            continue

        workspace: Optional[Path] = None
        branch: Optional[str] = None
        prepared = None
        claimed = None
        try:
            workspace, branch = _kbw._resolve_worktree_workspace(task, board=board)
            prepared = prepare_route(task, workspace, cfg, selection)
            claimed = _kb.claim_task(conn, task.id)
            if claimed is None:
                cleaned = bool(prepared.cleanup(cfg, allow="no_mutation"))
                if not cleaned:
                    _fence_unclaimed_remote(conn, task.id, "claim_lost_cleanup_unproven")
                if workspace is not None:
                    _kbw._cleanup_worktree_workspace(task.id, str(workspace), branch)
                result.remote_deferred.append((task.id, "claim_lost"))
                continue

            _kbw.set_workspace_path(conn, claimed.id, str(workspace))
            if claimed.branch_name != branch:
                _kbw.set_branch_name(conn, claimed.id, branch or "")
            run_id = int(claimed.current_run_id)
            receipt = prepared.receipt(run_id=run_id)
            _mark_remote_launching(conn, claimed.id, run_id, receipt)
            pid = launch_supervisor(
                claimed, workspace, prepared, cfg=cfg,
                db_path=_connection_db_path(conn, board), board=board,
                claim_lock=str(claimed.claim_lock),
            )
            _set_worker_pid(conn, claimed.id, pid)
            with _kb.write_txn(conn):
                _kb._append_event(
                    conn, claimed.id, "remote_route_prepared", receipt, run_id=run_id,
                )
            result.remote_routed.append(receipt)
            result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
            routed += 1
            active_remote += 1
        except Exception as exc:
            if claimed is None:
                if workspace is not None:
                    _kbw._cleanup_worktree_workspace(task.id, str(workspace), branch)
                cleanup_proven = bool(getattr(exc, "cleanup_proven", True))
                if ((prepared is not None and not prepared.cleanup(cfg, allow="no_mutation"))
                        or not cleanup_proven):
                    _fence_unclaimed_remote(conn, task.id, "preclaim_cleanup_unproven")
            else:
                cleaned = prepared is not None and prepared.cleanup(cfg, allow="no_mutation")
                run_id = int(claimed.current_run_id)
                if cleaned:
                    _mark_remote_direct(conn, claimed.id, run_id)
                    _kb._record_task_failure(
                        conn, claimed.id, "remote supervisor launch failed",
                        outcome="spawn_failed", release_claim=True, end_run=True,
                        expected_run_id=run_id,
                    )
                else:
                    _fence_remote_dispatch(
                        conn, claimed.id, run_id, "launch_failure_cleanup_unproven",
                    )
            result.remote_deferred.append((task.id, type(exc).__name__))


def _active_remote_supervisors(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM task_runs "
        "WHERE launch_mode='remote-codex-supervisor' AND ended_at IS NULL"
    ).fetchone()[0])


def count_active_remote_supervisors(conn: sqlite3.Connection) -> int:
    """Count active remote supervisors independently of local capacity."""
    return _active_remote_supervisors(conn)


def _mark_remote_launching(
    conn: sqlite3.Connection, task_id: str, run_id: int, receipt: Mapping[str, Any],
) -> None:
    encoded = json.dumps(dict(receipt), separators=(",", ":"), sort_keys=True)
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE task_runs SET launch_mode='remote-codex-supervisor', "
            "verification_status='remote-prepared', metadata=? "
            "WHERE id=? AND task_id=? AND ended_at IS NULL",
            (encoded, run_id, task_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("remote run launch fence lost")


def _mark_remote_direct(conn: sqlite3.Connection, task_id: str, run_id: int) -> None:
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET launch_mode='direct', verification_status='not-applicable' "
            "WHERE id=? AND task_id=? AND ended_at IS NULL",
            (run_id, task_id),
        )


def _fence_unclaimed_remote(conn: sqlite3.Connection, task_id: str, reason: str) -> None:
    current = conn.execute(
        "SELECT status, current_run_id FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if current and current["status"] == "running" and current["current_run_id"] is not None:
        _fence_remote_dispatch(conn, task_id, int(current["current_run_id"]), reason)
        return
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status='blocked', block_kind='capability', "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
            "WHERE id=? AND status IN ('ready','todo') AND current_run_id IS NULL",
            (task_id,),
        )
        if cur.rowcount:
            _kb._append_event(conn, task_id, "blocked", {
                "reason": reason[:120], "kind": "capability", "source_status": "ready",
                "remote_fence": True,
            })
            _kb._append_event(conn, task_id, "remote_route_fenced", {
                "contract": "KANBAN-CODEX-HOST-ROUTER-R1:v1",
                "reason": reason[:120], "mutation_state": "ambiguous",
            })


def _fence_remote_dispatch(
    conn: sqlite3.Connection, task_id: str, run_id: int, reason: str,
) -> None:
    """Persist a durable remote mutation fence and prevent local retry."""
    marker = f"remote-fence:{run_id}"
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status='blocked', block_kind='capability', claim_lock=?, "
            "claim_expires=NULL, worker_pid=NULL "
            "WHERE id=? AND status='running' AND current_run_id=?",
            (marker, task_id, run_id),
        )
        if cur.rowcount != 1:
            return
        closed = _kb._end_run(
            conn, task_id, outcome="blocked", status="blocked",
            summary="remote mutation fenced",
            metadata={
                "contract": "KANBAN-CODEX-HOST-ROUTER-R1:v1",
                "reason": reason[:120], "mutation_state": "ambiguous",
            },
        )
        _kb._append_event(conn, task_id, "blocked", {
            "reason": reason[:120], "kind": "capability", "source_status": "running",
            "remote_fence": True,
        }, run_id=closed or run_id)
        _kb._append_event(conn, task_id, "remote_mutation_fenced", {
            "contract": "KANBAN-CODEX-HOST-ROUTER-R1:v1",
            "reason": reason[:120], "mutation_state": "ambiguous", "fence": marker,
        }, run_id=closed or run_id)


def _apply_default_assignee(
    conn: sqlite3.Connection, task_id: str, assignee: str, *, dry_run: bool,
) -> bool:
    """Persist ``kanban.default_assignee`` on an unassigned ready row.

    Mutating the row keeps board state honest: the task is legitimately owned
    by the default, not "unassigned but secretly routed". ``dry_run`` reports
    without writing. Returns False when the write failed.
    """
    if dry_run:
        return True
    try:
        with _kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = ? WHERE id = ? "
                "AND (assignee IS NULL OR assignee = '')",
                (assignee, task_id),
            )
            _kb._append_event(
                conn, task_id, "assigned",
                {"assignee": assignee, "source": "kanban.default_assignee"},
            )
    except Exception:
        _kb._log.debug(
            "kanban dispatch: failed to apply default_assignee=%r to task %s",
            assignee, task_id, exc_info=True,
        )
        return False
    return True


def _run_reclaim_phase(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    stale_timeout_seconds: int,
    failure_limit: int,
    reconcile_orphans: bool,
    board: Optional[str] = None,
) -> None:
    """Reclaim stale/orphaned/crashed/timed-out running tasks, then promote."""
    reap_worker_zombies()
    _kb.reconcile_worker_scope_terminals(conn)
    result.reaped_terminal_workers = reap_terminal_workers(conn)
    result.reclaimed = _kb.release_stale_claims(conn, failure_limit=failure_limit)
    if reconcile_orphans:
        result.reconciled_orphans = reconcile_orphaned_running(conn)
    result.stale = detect_stale_running(conn, stale_timeout_seconds=stale_timeout_seconds)
    result.crashed = detect_crashed_workers(conn, board=board)
    # Side-channel attributes (see detect_crashed_workers); rate-limited tasks
    # went back to ``ready`` and the respawn guard defers them until quota clears.
    result.auto_blocked.extend(getattr(detect_crashed_workers, "_last_auto_blocked", []))
    result.rate_limited.extend(getattr(detect_crashed_workers, "_last_rate_limited", []))
    result.timed_out = enforce_max_runtime(conn)
    result.promoted = _kb.recompute_ready(conn, failure_limit=failure_limit)


def _tick_spawn_budget(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    max_spawn: Optional[int],
    max_in_progress: Optional[int],
    board: Optional[str],
) -> tuple[bool, Optional[int]]:
    """``(may_spawn, spawn_budget)`` for this tick; ``budget None`` = uncapped.

    ``max_spawn`` is a live per-board concurrency cap (running + this tick's
    spawns), not a per-tick budget — a per-tick reading would grow concurrency
    by N every tick. ``max_in_progress`` is a HOST-level cap: running workers on
    every other board count against the same budget, else N boards multiply the
    cap by N — exactly the fan-out the memory-derived default exists to prevent.
    """
    # Count already-running tasks so max_spawn enforces concurrency, not a
    # per-tick budget: "running" tasks stay running until the worker makes a terminal
    # board call (kanban_complete/kanban_block/kanban_request_review) or the TTL reclaims them.
    running_count = 0
    spawn_budget: Optional[int] = None
    if max_spawn is not None or max_in_progress is not None:
        running_count = count_running_tasks(conn)

    # Both ready and review loops consume from the same budget.
    if max_spawn is not None:
        if running_count >= max_spawn:
            return False, None
        spawn_budget = max_spawn - running_count

    if max_in_progress is not None:
        total_running = running_count + count_running_tasks_other_boards(board)
        if total_running >= max_in_progress:
            return False, None
        remaining = max_in_progress - total_running
        if spawn_budget is None or spawn_budget > remaining:
            spawn_budget = remaining

    # Memory-pressure guard: a static cap can't see the host's actual state.
    # critical -> spawn nothing this tick; elevated -> at most one new worker.
    # Reclaim/promotion already ran, so bookkeeping stays live; deferred tasks
    # wait for a later tick. "unknown" imposes no restriction.
    pressure = _memory_pressure_level()
    if pressure == "critical":
        result.memory_pressure = pressure
        _kb._log.warning(
            "kanban dispatch: system memory pressure is critical; "
            "spawning no new workers this tick (deferred, not dropped)"
        )
        return False, None
    if pressure == "elevated":
        result.memory_pressure = pressure
        if spawn_budget is None or spawn_budget > 1:
            _kb._log.warning(
                "kanban dispatch: system memory pressure is elevated; "
                "limiting to at most 1 new worker this tick"
            )
            spawn_budget = 1
    return True, spawn_budget


def _lane_rows(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Unclaimed rows of one lane in dispatch order."""
    return conn.execute(
        "SELECT id, assignee FROM tasks "
        f"WHERE status = '{status}' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()


def _any_spawnable_review(
    conn: sqlite3.Connection,
    review_rows: list[sqlite3.Row],
    *,
    per_profile_cap: Optional[int] = None,
    per_profile_running: Optional[dict[str, int]] = None,
    worker_toolsets: Optional[Iterable[str]] = None,
) -> bool:
    """Mirror review dispatch gates before reserving ready-lane capacity.

    Unavailable profile metadata retains the historic fail-open behavior. A
    review row that :func:`_dispatch_lane_task` would refuse this tick — its
    assignee already at the per-profile cap, or respawn-guarded — cannot
    consume the reservation, so it must not withhold capacity from an
    otherwise ready task (one such row would pin ``ready_budget`` to 0).
    """
    if not review_rows:
        return False
    profile_exists = _profile_exists_fn()
    running = per_profile_running or {}
    for row in review_rows:
        assignee = row["assignee"]
        if not assignee:
            continue
        if profile_exists is not None and not profile_exists(assignee):
            continue
        task = _kb.get_task(conn, row["id"])
        if task is not None and _worker_capabilities_for_task(
            task, assignee=assignee, toolsets_override=worker_toolsets,
        ) is not None:
            continue
        if per_profile_cap is not None and running.get(assignee, 0) >= per_profile_cap:
            continue
        if check_respawn_guard(conn, row["id"], lane="review") is None:
            return True
    return False


def _resolve_default_assignee(default_assignee: Optional[str]) -> Optional[str]:
    """``kanban.default_assignee`` when it names a real profile this home may
    claim (``kanban.dispatch_profiles`` gated, same predicate as the spawn
    gate). Otherwise ``None`` so an unassigned shared-board card is never
    written to. When the profiles module isn't importable trust the
    operator's config: the downstream check still buckets a missing profile
    as nonspawnable."""
    name = (default_assignee or "").strip() or None
    if name:
        profile_exists = _profile_exists_fn()
        if profile_exists is not None and not profile_exists(name):
            return None
    return name


def _dispatch_preview(
    conn: sqlite3.Connection,
    *,
    failure_limit: int,
    max_spawn: Optional[int],
    max_new_spawns: Optional[int],
    max_in_progress: Optional[int],
    default_assignee: Optional[str],
    max_in_progress_per_profile: Optional[int],
    allowed_worker_profiles: Optional[Iterable[str]],
    worker_toolsets: Optional[Iterable[str]],
) -> DispatchResult:
    """Build a dispatch preview in an in-memory copy, never mutating source DB."""
    preview = sqlite3.connect(":memory:")
    preview.row_factory = sqlite3.Row
    try:
        preview.executescript("\n".join(conn.iterdump()))
        result = DispatchResult()
        result.promoted = _kb.recompute_ready(preview, failure_limit=failure_limit)
        rows = preview.execute(
            "SELECT * FROM tasks WHERE status IN ('ready', 'review') "
            "AND claim_lock IS NULL ORDER BY CASE status WHEN 'ready' THEN 0 ELSE 1 END, priority DESC, created_at ASC"
        ).fetchall()
        allowed = frozenset(allowed_worker_profiles) if allowed_worker_profiles is not None else None
        running = int(preview.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0])
        per_profile: dict[str, int] = {}
        if max_in_progress_per_profile:
            for row in preview.execute("SELECT assignee, COUNT(*) FROM tasks WHERE status = 'running' AND assignee IS NOT NULL GROUP BY assignee"):
                per_profile[str(row[0])] = int(row[1])
        default = _resolve_default_assignee(default_assignee)
        for row in rows:
            if max_new_spawns is not None and len(result.spawned) >= max_new_spawns:
                break
            if max_in_progress is not None and running + len(result.spawned) >= max_in_progress:
                break
            if max_spawn is not None and running + len(result.spawned) >= max_spawn:
                break
            assignee = row["assignee"] or default
            if not assignee:
                result.skipped_unassigned.append(row["id"])
                continue
            if row["assignee"] is None and default:
                result.auto_assigned_default.append(row["id"])
            if allowed is not None and assignee not in allowed:
                _record_worker_profile_admission_skip(result, row["id"], assignee)
                continue
            profile_exists = _profile_exists_fn()
            if profile_exists is not None and not profile_exists(assignee):
                result.skipped_nonspawnable.append(row["id"])
                continue
            task = _kb.Task.from_row(row)
            diagnostic = _worker_capabilities_for_task(task, assignee=assignee, toolsets_override=worker_toolsets)
            if diagnostic is not None:
                _record_worker_capability_rejection(result, diagnostic)
                continue
            if max_in_progress_per_profile and per_profile.get(assignee, 0) >= max_in_progress_per_profile:
                result.skipped_per_profile_capped.append((row["id"], assignee, per_profile.get(assignee, 0)))
                continue
            lane = "review" if row["status"] == "review" else "ready"
            if lane == "review" and not review_dispatch_enabled():
                continue
            guard_reason = check_respawn_guard(preview, row["id"], lane=lane)
            if guard_reason is not None:
                result.respawn_guarded.append((row["id"], guard_reason))
                continue
            result.spawned.append((row["id"], assignee, ""))
            per_profile[assignee] = per_profile.get(assignee, 0) + 1
        if parallel_dispatch := bool(max_in_progress is not None and max_in_progress > 1):
            pressure = _kb._memory_pressure_level()
            if pressure == "unknown":
                result.memory_pressure = pressure
                result.spawned.clear()
        return result
    finally:
        preview.close()


def _dispatch_adaptive_locked(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    max_spawn: Optional[int] = None,
    max_new_spawns: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    allowed_worker_profiles: Optional[Iterable[str]] = None,
    worker_toolsets: Optional[Iterable[str]] = None,
    reconcile_orphans: bool = True,
) -> DispatchResult:
    """Adaptive host admission followed by the normal lane claim/spawn path."""
    result = DispatchResult()
    _run_reclaim_phase(
        conn, result, stale_timeout_seconds=stale_timeout_seconds,
        failure_limit=failure_limit, reconcile_orphans=reconcile_orphans,
    )
    ready_rows = _lane_rows(conn, "ready")
    review_rows = _lane_rows(conn, "review") if review_dispatch_enabled() else []
    if not ready_rows and not review_rows:
        return result

    scope_samples = []
    foreign: Optional[_OtherBoardsRunningObservation] = None
    foreign_error = "foreign-board occupancy unavailable"
    for _ in range(2):
        try:
            sample = _kb._read_live_worker_scopes()
            total = sample.get("total")
            if type(total) is not int or total < 0:
                raise ValueError("scope telemetry malformed")
            scope_samples.append(sample)
            try:
                foreign = _kb.observe_running_tasks_other_boards(board)
            except Exception as exc:
                foreign = None
                foreign_error = f"{type(exc).__name__}: {exc}"
            expected = count_running_tasks(conn) + (foreign.running_count if foreign is not None else 0)
            if total == expected:
                break
        except Exception as exc:
            result.admission_blocked = True
            result.admission_reason = "scope_telemetry_unavailable"
            result.admission_metrics = {"error": f"{type(exc).__name__}: {exc}"}
            return result
    expected_total = count_running_tasks(conn) + (foreign.running_count if foreign is not None else 0)
    if not scope_samples or scope_samples[-1].get("total") != expected_total:
        result.admission_blocked = True
        result.admission_reason = "scope_count_transition"
        result.admission_metrics = {
            "db_running": expected_total,
            "scope_running": scope_samples[-1].get("total") if scope_samples else None,
        }
        return result
    if foreign is None:
        result.admission_blocked = True
        result.admission_reason = "foreign_occupancy_unavailable"
        result.admission_metrics = {"error": foreign_error}
        return result

    running_count = count_running_tasks(conn)
    by_profile: dict[str, int] = {}
    for row in conn.execute(
        "SELECT assignee, COUNT(*) FROM tasks WHERE status = 'running' "
        "AND assignee IS NOT NULL GROUP BY assignee"
    ):
        by_profile[str(row[0])] = int(row[1])
    for profile, count in foreign.per_profile_running.items():
        by_profile[str(profile)] = by_profile.get(str(profile), 0) + int(count)
    total_running = running_count + foreign.running_count
    if max_in_progress is not None and total_running >= max_in_progress:
        return result
    spawn_budget: Optional[int] = None
    if max_spawn is not None:
        if running_count >= max_spawn:
            return result
        spawn_budget = max_spawn - running_count
    if max_in_progress is not None:
        remaining = max_in_progress - total_running
        spawn_budget = remaining if spawn_budget is None else min(spawn_budget, remaining)
    if max_new_spawns is not None:
        spawn_budget = max_new_spawns if spawn_budget is None else min(spawn_budget, max_new_spawns)
    pressure = _kb._memory_pressure_level()
    if pressure == "critical" or pressure == "unknown":
        result.memory_pressure = pressure
        return result
    if pressure == "elevated":
        result.memory_pressure = pressure
        spawn_budget = 1 if spawn_budget is None else min(spawn_budget, 1)
    if spawn_budget is not None and spawn_budget <= 0:
        return result

    allowed = frozenset(allowed_worker_profiles) if allowed_worker_profiles is not None else None
    per_profile_cap = max_in_progress_per_profile if isinstance(max_in_progress_per_profile, int) and max_in_progress_per_profile > 0 else None
    profile_exists = _profile_exists_fn()
    native_spawn = spawn_fn is None or spawn_fn is _default_spawn or spawn_fn is _kb._default_spawn
    require_scope = False
    scope_config = None
    if native_spawn:
        try:
            scope_config = _kb._worker_scope_config()
        except Exception as exc:
            result.admission_blocked = True
            result.admission_reason = "scope_config_invalid"
            result.admission_metrics = {"error": f"{type(exc).__name__}: {exc}"}
            return result
        capable, reason, _target = _kb._systemd_scope_preflight(
            require_scope=True, force_probe=True, scope_config=scope_config,
        )
        if not capable:
            result.admission_blocked = True
            result.admission_reason = str(reason or "scope_preflight_failed")
            return result
        require_scope = True

    default_name = _resolve_default_assignee(default_assignee)
    per_profile_running = dict(by_profile)
    review_eligible = _any_spawnable_review(review_rows)
    ready_budget = max(spawn_budget - 1, 0) if spawn_budget is not None and review_eligible else spawn_budget
    spawned = 0
    lane_kwargs = dict(
        dry_run=False, ttl_seconds=ttl_seconds, board=board,
        failure_limit=failure_limit, spawn_fn=spawn_fn,
        per_profile_cap=per_profile_cap, per_profile_running=per_profile_running,
        worker_toolsets=worker_toolsets, require_scope=require_scope,
        scope_config=scope_config,
    )
    for lane, rows, limit in (("ready", ready_rows, ready_budget), ("review", review_rows, spawn_budget)):
        for row in rows:
            if limit is not None and spawned >= limit:
                break
            assignee = row["assignee"]
            if not assignee and lane == "ready" and default_name:
                if _apply_default_assignee(conn, row["id"], default_name, dry_run=False):
                    assignee = default_name
                    result.auto_assigned_default.append(row["id"])
            if not assignee:
                result.skipped_unassigned.append(row["id"])
                continue
            if allowed is not None and assignee not in allowed:
                _record_worker_profile_admission_skip(result, row["id"], assignee)
                continue
            if profile_exists is not None and not profile_exists(assignee):
                result.skipped_nonspawnable.append(row["id"])
                continue
            if per_profile_cap is not None and per_profile_running.get(assignee, 0) >= per_profile_cap:
                result.skipped_per_profile_capped.append((row["id"], assignee, per_profile_running.get(assignee, 0)))
                continue
            if check_respawn_guard(conn, row["id"], lane=lane) is not None:
                result.respawn_guarded.append((row["id"], check_respawn_guard(conn, row["id"], lane=lane)))
                continue
            if _dispatch_lane_task(conn, row, assignee, result, lane=lane, **lane_kwargs):
                spawned += 1
    return result


# The dispatch lock has been released here. Fire the tick observer strictly OUTSIDE the single-writer
# critical section (#56066 sweeper finding / #64231 disposition): a slow subscriber must never extend the
# lock hold and stall a sibling dispatcher's tick.
def _dispatch_once_locked(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_new_spawns: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    allowed_worker_profiles: Optional[Iterable[str]] = None,
    worker_toolsets: Optional[Iterable[str]] = None,
    effective_config: Optional[Mapping[str, Any]] = None,
    selected_boards: Optional[Iterable[str]] = None,
    parallel_dispatch: bool = False,
    reconcile_orphans: bool = True,
    _native_admission_held: bool = False,
    _skip_maintenance: bool = False,
    _dispatch_result: Optional[DispatchResult] = None,
    _native_scope_snapshot: Optional[_WorkerScopeConfig] = None,
) -> DispatchResult:
    """One dispatcher tick: reclaim stale/crashed running tasks, promote
    todo -> ready, then atomically claim each spawnable ready/review row and
    call ``spawn_fn(task, workspace_path, board) -> Optional[int]``, recording
    the PID so later ticks catch crashes before the TTL. Cap semantics:
    :func:`_tick_spawn_budget`."""
    if parallel_dispatch:
        return _dispatch_adaptive_locked(
            conn, spawn_fn=spawn_fn, ttl_seconds=ttl_seconds,
            max_spawn=max_spawn, max_new_spawns=max_new_spawns,
            max_in_progress=max_in_progress, failure_limit=failure_limit,
            stale_timeout_seconds=stale_timeout_seconds, board=board,
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
            allowed_worker_profiles=allowed_worker_profiles,
            worker_toolsets=worker_toolsets, reconcile_orphans=reconcile_orphans,
        )
    result = _dispatch_result if _dispatch_result is not None else DispatchResult()
    native_spawn = (
        spawn_fn is None
        or spawn_fn is _default_spawn
        or spawn_fn is _kb._default_spawn
    )
    native_scope_config = _native_scope_snapshot
    if native_spawn and not dry_run and native_scope_config is None:
        native_scope_config = _kb._worker_scope_config()

    # A required scope is an admission prerequisite.  Do this before any
    # reclaim/recompute mutation so an unavailable required manager cannot
    # change task identity while the dispatcher is unable to launch safely.
    if (
        native_spawn
        and not dry_run
        and not _skip_maintenance
        and native_scope_config is not None
        and native_scope_config.required
    ):
        capable, reason, _target = _kb._systemd_scope_preflight(
            require_scope=True,
            force_probe=True,
            scope_config=native_scope_config,
        )
        if not capable:
            _kb._log.warning(
                "kanban dispatch: native worker scope preflight failed; deferring claim (%s)",
                reason,
            )
            return result

    if not _skip_maintenance:
        _run_reclaim_phase(
            conn, result, stale_timeout_seconds=stale_timeout_seconds,
            failure_limit=failure_limit, reconcile_orphans=reconcile_orphans,
            board=board,
        )
    may_spawn, spawn_budget = _tick_spawn_budget(
        conn, result, max_spawn=max_spawn, max_in_progress=max_in_progress, board=board,
    )
    if not may_spawn:
        return result

    ready_rows = _lane_rows(conn, "ready")
    # Review rows are enumerated up front so the budget split can see whether
    # review work exists at all.
    review_rows = _lane_rows(conn, "review") if review_dispatch_enabled() else []
    if native_spawn and not dry_run and (ready_rows or review_rows):
        # The native admission lock is held by dispatch_once for the complete
        # observation -> claim -> launch transition.  A direct call into this
        # private helper still fails closed instead of silently bypassing it.
        if not _native_admission_held:
            return result
        strict_other = _kb.observe_running_tasks_other_boards(board)
        if strict_other is None:
            result.admission_blocked = True
            result.admission_reason = "foreign_occupancy_unavailable"
            return result

        running_count = count_running_tasks(conn)
        if max_in_progress is not None:
            total_running = running_count + strict_other.running_count
            if total_running >= max_in_progress:
                return result
            remaining = max_in_progress - total_running
            if spawn_budget is None or spawn_budget > remaining:
                spawn_budget = remaining
        per_profile_counts: dict[str, int] = {}
        if max_in_progress_per_profile is not None:
            for prow in conn.execute(
                "SELECT assignee, COUNT(*) FROM tasks "
                "WHERE status = 'running' AND assignee IS NOT NULL GROUP BY assignee"
            ):
                per_profile_counts[str(prow[0])] = int(prow[1])
            for profile, count in strict_other.per_profile_running.items():
                per_profile_counts[str(profile)] = (
                    per_profile_counts.get(str(profile), 0) + int(count)
                )

        default_name = _resolve_default_assignee(default_assignee)
        profile_exists = _profile_exists_fn()
        candidate_count = 0
        seen_by_profile: dict[str, int] = {}
        rows_with_lanes = [("ready", row) for row in ready_rows] + [
            ("review", row) for row in review_rows
        ]
        for lane, row in rows_with_lanes:
            assignee = row["assignee"] or (
                default_name if lane == "ready" else None
            )
            if not assignee:
                continue
            if profile_exists is not None and not profile_exists(assignee):
                continue
            if (
                max_in_progress_per_profile is not None
                and per_profile_counts.get(str(assignee), 0)
                + seen_by_profile.get(str(assignee), 0)
                >= max_in_progress_per_profile
            ):
                continue
            if check_respawn_guard(conn, row["id"], lane=lane) is not None:
                continue
            candidate_count += 1
            seen_by_profile[str(assignee)] = seen_by_profile.get(str(assignee), 0) + 1
            if max_spawn is not None and running_count + candidate_count >= max_spawn:
                break
            if max_in_progress is not None and (
                running_count + strict_other.running_count + candidate_count
                >= max_in_progress
            ):
                break
            if max_new_spawns is not None and candidate_count >= max_new_spawns:
                break

        native_scope_required = bool(
            native_scope_config is not None
            and native_scope_config.required
        ) or (
            candidate_count != 1
            or running_count > 0
            or strict_other.running_count > 0
            or strict_other.has_independent_db
        )
        if native_scope_required:
            capable, reason, _target = _kb._systemd_scope_preflight(
                require_scope=True,
                force_probe=True,
                scope_config=native_scope_config,
            )
            if not capable:
                _kb._log.warning(
                    "kanban dispatch: native worker scope preflight failed; deferring claim (%s)",
                    reason,
                )
                return result
        require_scope = native_scope_required
    else:
        require_scope = False
    # Per-profile cap. Deferred tasks go to skipped_per_profile_capped, not
    # skipped_unassigned — "busy, retry later" differs from "needs routing".
    # Resolved BEFORE the review reservation so the reservation can see which
    # review rows the lane loop would refuse this tick.
    per_profile_cap = max_in_progress_per_profile if (
        # Per-profile concurrency cap (#21582): when set, track how many workers each assignee already has
        # in flight, and refuse to spawn when this would push that assignee past the cap. Prevents fan-out
        # workloads from melting a single profile's local model / API quota / browser pool while leaving
        # other profiles idle.
        isinstance(max_in_progress_per_profile, int)
        and max_in_progress_per_profile > 0
    ) else None
    per_profile_running: dict[str, int] = {}
    if per_profile_cap is not None:
        for prow in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' AND assignee IS NOT NULL "
            "GROUP BY assignee"
        ):
            per_profile_running[prow["assignee"]] = int(prow["n"])
    # Review-lane reservation: the ready loop runs first and would otherwise
    # consume the ENTIRE shared budget, starving reviews under a sustained ready
    # backlog. When spawnable review work exists and there is any budget, hold
    # one slot back.
    ready_budget = spawn_budget
    if spawn_budget is not None and spawn_budget > 0 and _any_spawnable_review(
        conn, review_rows,
        per_profile_cap=per_profile_cap, per_profile_running=per_profile_running,
        worker_toolsets=worker_toolsets,
    ):
        ready_budget = max(spawn_budget - 1, 0)
    lane_kwargs: dict[str, Any] = dict(
        dry_run=dry_run, ttl_seconds=ttl_seconds, board=board,
        failure_limit=failure_limit, spawn_fn=spawn_fn,
        per_profile_cap=per_profile_cap, per_profile_running=per_profile_running,
        require_scope=require_scope,
        scope_config=native_scope_config if native_spawn else None,
        worker_toolsets=worker_toolsets,
    )
    default_assignee = _resolve_default_assignee(default_assignee)
    spawned = 0
    for row in ready_rows:
        if ready_budget is not None and spawned >= ready_budget:
            break
        row_assignee = row["assignee"]
        if not row_assignee:
            # Honour kanban.default_assignee so an unassigned task doesn't
            # park in 'ready' forever.
            if not default_assignee or not _apply_default_assignee(
                conn, row["id"], default_assignee, dry_run=dry_run,
            ):
                result.skipped_unassigned.append(row["id"])
                continue
            row_assignee = default_assignee
            result.auto_assigned_default.append(row["id"])
        if _dispatch_lane_task(conn, row, row_assignee, result, lane="ready", **lane_kwargs):
            spawned += 1

    # A review agent (sdlc-review) approves (→ done) or requests changes
    # (→ ready/todo). Review spawns share max_spawn with ready tasks. The loop
    # checks the FULL shared ``spawn_budget`` — the reservation above caps the
    # ready lane, it grants no extra capacity here.
    for row in review_rows:
        if spawn_budget is not None and spawned >= spawn_budget:
            break
        if not row["assignee"]:
            result.skipped_unassigned.append(row["id"])
            continue
        if _dispatch_lane_task(conn, row, row["assignee"], result, lane="review", **lane_kwargs):
            spawned += 1
    return result


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def worker_log_rotation_config(kanban_cfg: Optional[dict] = None) -> tuple[int, int]:
    """Return ``(rotate_bytes, backup_count)`` for worker log rotation.
    Defaults: rotate at 2 MiB, keep one backup (``.log.1``); both overridable
    from ``config.yaml``.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    max_bytes = _positive_int(kanban_cfg.get("worker_log_rotate_bytes"), DEFAULT_LOG_ROTATE_BYTES, minimum=1)
    backup_count = _positive_int(kanban_cfg.get("worker_log_backup_count"), DEFAULT_LOG_BACKUP_COUNT, minimum=0)
    return max_bytes, backup_count


def _rotated_log_path(log_path: Path, generation: int) -> Path:
    return log_path.with_suffix(log_path.suffix + f".{generation}")


def _rotate_worker_log(
    log_path: Path,
    max_bytes: int,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Rotate ``<log>`` when it exceeds ``max_bytes``: ``<log>`` → ``<log>.1``,
    older generations shift up to ``backup_count``.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size <= max_bytes:
            return
        backup_count = _positive_int(backup_count, DEFAULT_LOG_BACKUP_COUNT, minimum=0)
        if backup_count == 0:
            log_path.unlink()
            return
        oldest = _rotated_log_path(log_path, backup_count)
        with contextlib.suppress(OSError):
            if oldest.exists():
                oldest.unlink()
        for generation in range(backup_count - 1, 0, -1):
            src = _rotated_log_path(log_path, generation)
            if not src.exists():
                continue
            with contextlib.suppress(OSError):
                src.rename(_rotated_log_path(log_path, generation + 1))
        log_path.rename(_rotated_log_path(log_path, 1))
    except OSError:
        pass


def _module_hermes_argv() -> list[str]:
    """Interpreter-bound Hermes CLI invocation (``hermes_cli.main`` is the
    console-script target — there is no top-level ``hermes`` package)."""
    return [sys.executable, "-m", "hermes_cli.main"]


def _absolute_hermes_path(path: str) -> str:
    """Return an absolute filesystem path for a resolved Hermes shim."""
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


def _looks_like_path(value: str) -> bool:
    """Return true when a command override is an explicit path, not a name."""
    expanded = os.path.expanduser(value)
    return (
        expanded.startswith("~")
        or os.path.isabs(expanded)
        or bool(os.path.dirname(expanded))
        or "\\" in expanded
        or bool(re.match(r"^[A-Za-z]:", expanded))
    )


def _is_windows_batch_shim(path: str) -> bool:
    """Return true for Windows shell/batch shims that should not be argv[0]."""
    return path.lower().endswith((".cmd", ".bat"))


def _path_search_names(command: str) -> list[str]:
    """Return executable names to try for an unqualified command."""
    if not _kb._IS_WINDOWS or os.path.splitext(command)[1]:
        return [command]
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    return [command + ext for ext in raw.split(";") if ext]


def _safe_which_no_cwd(command: str) -> Optional[str]:
    """Resolve a bare command from PATH without implicit current-dir search.

    On Windows ``shutil.which`` may search the current directory before PATH
    for bare names — unsafe for a dispatcher. Only explicit PATH entries are
    considered; empty / ``.`` entries are skipped.
    """
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir or raw_dir == ".":
            continue
        directory = os.path.expanduser(raw_dir)
        for name in _path_search_names(command):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and (_kb._IS_WINDOWS or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _hermes_path_argv(path: str) -> list[str]:
    """argv for a resolved Hermes executable path. Windows batch shims
    (``.cmd``/``.bat``) are unsafe as argv[0] because the argument vector
    includes task-derived values; prefer the module form."""
    if _kb._IS_WINDOWS and _is_windows_batch_shim(path):
        return _module_hermes_argv()
    return [_absolute_hermes_path(path)]


def _resolve_hermes_argv() -> list[str]:
    """Resolve the ``hermes`` invocation as argv for ``Popen``: ``$HERMES_BIN``
    (path-like -> absolute; bare names keep PATH semantics, never a
    same-directory file), then the running interpreter's ``sys.executable -m
    hermes_cli.main`` (exactly this install; also covers shim-less cron,
    systemd ``User=``, launchd), then ``which("hermes")`` (Windows: safe PATH
    search, batch shims fall back to the module form) only when ``hermes_cli``
    is not importable. The module argv must win over PATH: a PATH-first lookup
    lets an attacker-planted ``hermes`` shadow the running install (#111569).
    Mirrors ``gateway.run._resolve_hermes_bin``; local because ``hermes_cli``
    sits below ``gateway`` in the dependency order.
    """
    import importlib.util
    import shutil

    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin:
        if _looks_like_path(env_bin):
            return _hermes_path_argv(env_bin)
        resolved_env_bin = _safe_which_no_cwd(env_bin)
        if resolved_env_bin:
            return _hermes_path_argv(resolved_env_bin)
        return _module_hermes_argv()

    try:
        if importlib.util.find_spec("hermes_cli") is not None:
            return _module_hermes_argv()
    except Exception:
        pass

    hermes_bin = _safe_which_no_cwd("hermes") if _kb._IS_WINDOWS else shutil.which("hermes")
    if hermes_bin:
        return _hermes_path_argv(hermes_bin)
    return _module_hermes_argv()


def _worker_terminal_timeout_env(
    max_runtime_seconds: Optional[int],
    current_timeout: Optional[str],
) -> Optional[str]:
    """Return a worker-scoped TERMINAL_TIMEOUT override, if needed.

    When ``max_runtime_seconds`` exceeds the terminal tool's default timeout,
    raise only the child's default so a long command isn't killed by the
    generic terminal default first.
    """
    if max_runtime_seconds is None:
        return None
    try:
        runtime = int(max_runtime_seconds)
    except (TypeError, ValueError):
        return None
    if runtime <= 0:
        return None

    desired = max(1, runtime - KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS)
    try:
        existing = int(str(current_timeout).strip()) if current_timeout else 0
    except (TypeError, ValueError):
        existing = 0
    if existing >= desired:
        return None
    return str(desired)


@contextlib.contextmanager
def _worker_profile_scope(hermes_home: str, *, bind_home: bool = True):
    """Bind an assigned profile's runtime scope (secrets + terminal policy, optionally home) for
    one dispatch-side read or spawn-env build.

    The dispatcher runs detached from any turn, so nothing binds a profile for it: ``load_config``,
    the toolset probes' ``get_secret`` reads and ``build_subprocess_env``'s passthrough resolution
    all fall back to the LAUNCH profile's ambient ``os.environ`` / ``TERMINAL_*``. Binding was
    previously conditional on ``is_multiplex_active()``, so on a single-profile host a worker for
    profile B was built entirely from the dispatcher's own environment.

    ``bind_home=False`` for the spawn-env build: which variables may cross into a child is the
    DISPATCHER's ``terminal.env_passthrough`` policy (#109494, read through the home override) —
    only their VALUES come from the assignee's scope, so that branch binds the secret scope alone.
    Toolset resolution binds the home and the terminal policy, as it always has.

    The secret mapping is never widened: a profile that is not this process's own home gets its own
    ``.env`` + external sources ONLY, while the launch home keeps its established
    env-over-``.env`` precedence (``launch_secret_scope``) so systemd / ``op run`` injection still
    resolves for a standalone dispatcher.
    """
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import get_process_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools.terminal_scope import install_profile_terminal_scope, reset_terminal_scope
    from tui_gateway.launch_profile_policy import launch_secret_scope, launch_terminal_env

    home = Path(hermes_home)
    is_launch_home = str(home.resolve()) == str(Path(get_process_hermes_home()).resolve())
    home_token = set_hermes_home_override(str(home)) if bind_home else None
    secret_token = set_secret_scope(
        launch_secret_scope(home) if is_launch_home else build_profile_secret_scope(home))
    terminal_token = install_profile_terminal_scope(
        home, env_overlay=launch_terminal_env() if is_launch_home else None) if bind_home else None
    try:
        yield
    finally:
        if terminal_token is not None:
            reset_terminal_scope(terminal_token)
        reset_secret_scope(secret_token)
        if home_token is not None:
            reset_hermes_home_override(home_token)


def _resolve_worker_cli_toolsets(hermes_home: Optional[str]) -> Optional[list[str]]:
    """Return the assigned profile's effective CLI toolsets for a worker.

    Resolved at dispatch time and passed as an explicit ``--toolsets`` pin so
    worker startup cannot fall back to a stale root/active-profile config or a
    profile whose top-level ``toolsets`` is only the kanban orchestrator
    surface. ``model_tools`` still appends the task-scoped kanban lifecycle
    tools when ``HERMES_KANBAN_TASK`` is set.
    """
    if not hermes_home:
        return None
    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        with _worker_profile_scope(hermes_home):
            cfg = load_config()
            toolsets = sorted(_get_platform_tools(cfg, "cli"))
        return toolsets or None
    except Exception as exc:
        _kb._log.debug(
            "kanban worker: could not resolve CLI toolsets for HERMES_HOME=%r (%s)",
            hermes_home,
            exc,
        )
        return None


def _normalize_worker_toolset_override(
    toolsets: Optional[Iterable[str]],
) -> Optional[list[str]]:
    if toolsets is None:
        return None
    values = toolsets.split(",") if isinstance(toolsets, str) else toolsets
    return list(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))


def _resolve_worker_capability_tools(
    assignee: str, *, toolsets_override: Optional[Iterable[str]] = None,
) -> Optional[set[str]]:
    toolset_names = _normalize_worker_toolset_override(toolsets_override)
    if toolset_names is None:
        try:
            from hermes_cli.profiles import resolve_profile_env
            profile_home = resolve_profile_env(str(assignee))
        except Exception:
            try:
                from hermes_constants import get_hermes_home
                root = Path(get_hermes_home())
                profile_home = str(
                    root if str(assignee).casefold() == "default"
                    else root / "profiles" / str(assignee).strip().lower()
                )
            except Exception:
                return None
        toolset_names = _resolve_worker_cli_toolsets(profile_home)
        if toolset_names is None:
            return None
    try:
        from toolsets import resolve_toolset
    except Exception:
        return None
    tools: set[str] = set()
    for name in toolset_names:
        normalized_name = str(name).strip()
        if not normalized_name:
            continue
        tools.add(normalized_name.casefold())
        try:
            tools.update(str(tool).casefold() for tool in resolve_toolset(normalized_name))
        except Exception:
            continue
    return tools


def _worker_capabilities_for_task(
    task: "Task", *, assignee: Optional[str] = None,
    toolsets_override: Optional[Iterable[str]] = None,
) -> Optional[dict[str, Any]]:
    try:
        required = _kb.normalize_required_worker_capabilities(task.required_capabilities)
    except ValueError as exc:
        values = sorted(str(value) for value in (task.required_capabilities or []))
        return {
            "task_id": task.id, "assignee": assignee or task.assignee,
            "reason": "invalid_required_capabilities",
            "reason_code": "invalid_required_capabilities",
            "missing_capabilities": values, "required_capabilities": values,
            "available_capabilities": [], "detail": str(exc),
        }
    if not required:
        return None
    candidate = str(assignee or task.assignee or "").strip()
    tools = _resolve_worker_capability_tools(
        candidate, toolsets_override=toolsets_override,
    ) if candidate else None
    if tools is None:
        return {
            "task_id": task.id, "assignee": candidate or None,
            "reason": "missing_capabilities", "reason_code": "worker_toolsets_unresolved",
            "missing_capabilities": list(required), "required_capabilities": list(required),
            "available_capabilities": [],
        }
    available: set[str] = set()
    if {"terminal", "process_manage"} & tools:
        available.update({"terminal", "local_file_hash"})
    if {"read_file", "search_files"} & tools:
        available.add("local_file_read")
    if {"kanban_attach", "kanban_attach_url"} & tools:
        available.add("task_attachment_write")
    if task.workspace_kind in _kb.VALID_WORKSPACE_KINDS and (
        {"terminal", "process_manage", "read_file", "search_files"} & tools
    ):
        available.add("workspace_access")
    missing = sorted(set(required) - available)
    if not missing:
        return None
    return {
        "task_id": task.id, "assignee": candidate or None,
        "reason": "missing_capabilities", "reason_code": "missing_worker_capabilities",
        "missing_capabilities": missing, "required_capabilities": list(required),
        "available_capabilities": sorted(available),
    }
_retagged_workspace_roots: set[str] = set()


def _retag_legacy_worker_sessions(workspaces_root_path: str) -> None:
    """Reclaim pre-tag worker rows in state.db so they leave the session lists.

    Best-effort: the durable gate is ``state_meta`` in
    ``retag_kanban_worker_sessions``; the in-process set avoids reopening
    state.db on every spawn. A tick must never fail because a session DB was
    busy or missing.
    """
    if workspaces_root_path in _retagged_workspace_roots:
        return
    try:
        from hermes_state_registry import acquire, release_or_close

        # Inside the gateway the dispatcher shares the process's registry handle; a bare
        # SessionDB() here was one more writer connection on the same state.db (#100896).
        db = acquire()
        try:
            db.retag_kanban_worker_sessions(workspaces_root_path)
        finally:
            release_or_close(db)
        _retagged_workspace_roots.add(workspaces_root_path)
    except Exception as exc:
        _kb._log.debug("kanban worker: legacy session retag skipped (%s)", exc)


def _worker_argv(
    task: Task, profile_arg: str, hermes_home: Optional[str],
    worker_toolsets: Optional[Iterable[str]] = None,
) -> list[str]:
    """Build the ``hermes -p <profile> --cli ... chat -q ...`` worker command."""
    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,
        # A worker must NEVER boot the interactive TUI: its no-TTY bail-out
        # exits 0 without doing the task → "protocol violation" every attempt.
        "--cli",
        # Workers run under a profile-scoped HERMES_HOME and so see that
        # profile's shell-hook allowlist; pass --accept-hooks explicitly so
        # configured hooks still register.
        "--accept-hooks",
    ]
    # One `--skills X` pair per name: easier to read in `ps` and avoids quoting
    # ambiguity if a skill name contains unusual chars.
    for sk in task.skills or ():
        if sk:
            cmd.extend(["--skills", sk])
    if task.model_override:
        cmd.extend(["-m", task.model_override])
        # Pin the provider too so the worker resolves the model against the
        # intended backend (model X with provider Y is the classic board-stall).
        if task.provider_override:
            cmd.extend(["--provider", task.provider_override])
    # Independent of the model override — a task can run the profile's own
    # model at a different depth.
    if task.reasoning_effort:
        cmd.extend(["--reasoning", task.reasoning_effort])
    pinned_toolsets = (
        _normalize_worker_toolset_override(worker_toolsets)
        if worker_toolsets is not None else _resolve_worker_cli_toolsets(hermes_home)
    )
    if pinned_toolsets is not None:
        cmd.extend(["--toolsets", ",".join(pinned_toolsets)])
    cmd.extend(["chat", "-q", f"work kanban task {task.id}"])
    # goal_mode rides the same `-q` path: cli.py runs the judge loop there too, so the
    # worker log keeps its live tool feed (forcing -Q blanked it).
    return cmd


def _open_worker_log(task: Task, board: Optional[str]):
    """Append-mode per-task log (a re-run on unblock appends, never overwrites),
    rotated first. Anchored at the board root (not the shared kanban root) so
    `hermes kanban log` reads its own file and boards sharing task ids don't
    collide."""
    log_dir = _kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rotate_bytes, backup_count = worker_log_rotation_config()
    _rotate_worker_log(log_path, rotate_bytes, backup_count)
    return open(log_path, "ab")


def _restart_safe_worker_argv(task: Task, command: list[str]) -> list[str]:
    """Wrap a systemd-hosted dispatcher's worker in the shared restart-safe scope.

    Kanban workers are long-lived agentic runs that outlive the dispatcher
    tick, so they never take cron's degraded mode under the managed gateway:
    ``require_restart_safe_scope=True`` makes the helper raise
    ``RestartSafeScopeUnavailable`` there (an infrastructure spawn failure the
    dispatcher does not charge to the card). Under any other systemd unit
    (``Type=oneshot`` dispatch timers, #113612) ``outlives_parent=True`` gets the
    worker its own scope so the unit's cgroup teardown cannot kill it.
    """
    from tools.process_registry import restart_safe_gateway_child_argv

    if task.current_run_id is None:
        # Outside managed systemd this is harmless, but a managed dispatch must
        # never mint an untraceable worker.  Check topology through the shared
        # helper first, using a placeholder suffix that cannot be launched.
        dispatch = restart_safe_gateway_child_argv(
            command,
            unit_suffix=f"kanban-{task.id}-run-missing",
            require_restart_safe_scope=True,
            outlives_parent=True,
        )
        if dispatch.mode != "in_process":
            raise RuntimeError(
                "cannot create restart-safe systemd scope for Kanban worker: "
                "the claimed task has no current run id"
            )
        return command

    return restart_safe_gateway_child_argv(
        command,
        unit_suffix=f"kanban-{task.id}-run-{task.current_run_id}",
        require_restart_safe_scope=True,
        outlives_parent=True,
    ).argv


def _default_spawn(
    task: Task,
    workspace: str,
    *,
    board: Optional[str] = None,
    worker_toolsets: Optional[Iterable[str]] = None,
    require_scope: bool = False,
    scope_config: Optional[_WorkerScopeConfig] = None,
    launch_intent_fn=None,
    clear_launch_intent_fn=None,
) -> Optional[int]:
    """Fire-and-forget ``hermes -p <profile> chat -q ...`` subprocess.

    Returns the child's PID so the dispatcher can detect crashes before the
    claim TTL expires; completion is still observed via the worker's own
    ``complete`` / ``block`` transitions. ``board`` pins the child's
    ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` / workspaces_root to the
    board the task was claimed from, so workers cannot see other boards.
    """
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")

    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    profile_arg = normalize_profile_name(task.assignee)

    from agent.secret_scope import is_multiplex_active
    from tools.environments.local import _is_routed_home, build_subprocess_env, strip_launch_profile_env

    try:
        profile_home = resolve_profile_env(profile_arg)
    except FileNotFoundError:
        # No profile dir (isolated test fixtures) — the CLI resolves it from
        # HERMES_PROFILE (set below) instead.
        profile_home = None

    # Scrub for a ROUTED home, not only under multiplex: the authority test is "does this worker act
    # for another profile", exactly as served_profile_child_env decides it (tools/environments/local.py).
    # Gating on the gateway-wide flag left B's worker inheriting the dispatcher's own OPENAI_API_KEY and
    # systemd-injected tokens on every single-profile host.
    routed = bool(profile_home) and _is_routed_home(profile_home)
    # build_subprocess_env's secret scrub resolves terminal.env_passthrough vars through get_secret(),
    # which without a bound scope reads the LAUNCH profile's ambient environment for a worker spawned
    # on B's behalf (and raises under multiplex) — so bind B's secret scope around the build.
    with (_worker_profile_scope(profile_home, bind_home=False) if profile_home
          else contextlib.nullcontext()):
        env = build_subprocess_env(
            scrub_secrets=is_multiplex_active() or routed,
            inherit_profile_home=True,
        )
    # The dispatcher is detached from every conversation; its worker must never
    # inherit routing mirrored by a previous gateway turn.
    from gateway.session_context import _VAR_MAP
    for key in _VAR_MAP:
        env.pop(key, None)

    # Inject HERMES_HOME so the worker reads the profile-scoped config.yaml:
    # without it the child's get_hermes_home() falls back to the DEFAULT
    # profile root because `hermes -p` applies its override before
    # hermes_constants is imported.
    if profile_home:
        env["HERMES_HOME"] = profile_home
        # A multiplexer dispatching for another profile must not hand it the launch
        # profile's .env settings / TERMINAL_* policy — a standalone dispatcher never would.
        strip_launch_profile_env(env, profile_home)
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    # Tag the session `kanban` so session-browsing surfaces filter it out by
    # source instead of rendering one sidebar row per attempt.
    env["HERMES_SESSION_SOURCE"] = "kanban"
    # TERMINAL_CWD takes precedence over process cwd in file_tools and
    # build_context_files_prompt; without it relative writes land in the gateway
    # user's home and workers load the gateway's AGENTS.md. file_tools rejects
    # relative / sentinel values, so only set a real absolute directory.
    # Pin TERMINAL_CWD to the task's workspace so the worker's file tools and context-file loader anchor on
    # the workspace, not whatever cwd the dispatching gateway happened to export. The worker subprocess is
    # already launched with cwd=workspace, but TERMINAL_CWD takes precedence over the process cwd in both
    # file_tools._resolve_base_dir (#41312 — relative write_file paths were landing in the gateway user's
    # home) and build_context_files_prompt (#34619 — workers loaded the dispatching gateway's AGENTS.md
    # instead of the task's). Setting it to the workspace fixes both: the workspace is where the task's work
    # actually happens.
    if workspace and os.path.isabs(workspace) and os.path.isdir(workspace):
        env["TERMINAL_CWD"] = workspace
    if task.branch_name:
        env["HERMES_KANBAN_BRANCH"] = task.branch_name
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    # Goal-loop mode (Ralph-style /goal judge loop in cli.py quiet-mode path).
    # Only set when enabled so non-goal tasks keep a clean env.
    if task.goal_mode:
        env["HERMES_KANBAN_GOAL_MODE"] = "1"
        if task.goal_max_turns is not None:
            env["HERMES_KANBAN_GOAL_MAX_TURNS"] = str(int(task.goal_max_turns))
    for var in ("TERMINAL_TIMEOUT", "TERMINAL_MAX_FOREGROUND_TIMEOUT"):
        override = _worker_terminal_timeout_env(task.max_runtime_seconds, env.get(var))
        if override is not None:
            env[var] = override
    # Pin the board DB + workspaces root so the worker's kanban paths still
    # match after `hermes -p` rewrites HERMES_HOME (symlink / Docker layouts).
    env["HERMES_KANBAN_DB"] = str(_kb.kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(_kb.workspaces_root(board=board))
    _retag_legacy_worker_sessions(env["HERMES_KANBAN_WORKSPACES_ROOT"])
    # Board slug — defense-in-depth pin if a path is resolved without the
    # DB / workspaces env vars.
    env["HERMES_KANBAN_BOARD"] = _kb._normalize_board_slug(board) or _kb.get_current_board()
    # kanban_comment reads HERMES_PROFILE for its default author; `-p` alone
    # doesn't set the env var.
    env["HERMES_PROFILE"] = profile_arg
    # This is the grant boundary: the dispatcher assigned this new worker's task.
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    env.pop(DELEGATED_CHILD_ENV_MARKER, None)
    # `--cli` is the highest-precedence TUI override; dropping HERMES_TUI covers
    # older hermes builds on PATH that predate the flag's precedence.
    env.pop("HERMES_TUI", None)

    worker_cmd = _worker_argv(
        task, profile_arg, env.get("HERMES_HOME"), worker_toolsets=worker_toolsets,
    )
    if scope_config is None:
        scope_config = _kb._worker_scope_config()
    cmd, scope_unit, scope_target = _kb._systemd_scope_argv(
        worker_cmd, task, board=board, require_scope=require_scope,
        scope_config=scope_config,
    )
    direct_env = dict(env)
    if scope_unit is None:
        # A worker spawned by a managed systemd gateway must leave the
        # gateway's cgroup before startup; otherwise its handoff is killed
        # with the gateway.  This is only the portable direct fallback.
        cmd = _restart_safe_worker_argv(task, worker_cmd)
        from tools.process_registry import systemd_user_bus_env
        env = systemd_user_bus_env(env)
    else:
        if launch_intent_fn is not None:
            launch_intent_fn(scope_unit, scope_target, scope_config)
        for key in ("DBUS_STARTER_ADDRESS", "DBUS_STARTER_BUS_TYPE"):
            env.pop(key, None)
        env.update(_kb._systemd_user_manager_environment(scope_target))
    log_f = _open_worker_log(task, board)
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            cmd,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _kb._IS_WINDOWS else 0,
        )
    except FileNotFoundError:
        log_f.close()
        raise RuntimeError(
            "`hermes` executable not found on PATH. "
            "Install Hermes Agent or activate its venv before running the kanban dispatcher."
        )
    # Intentionally NOT closing log_f: the child keeps writing after return;
    # the OS-level FD stays open in the child until it exits.
    if scope_unit is None:
        if _kb._IS_WINDOWS:
            _live_worker_procs[proc.pid] = proc
        return _WorkerLaunchPid(proc.pid, launch_mode="direct", verification_status="not-applicable")
    try:
        verified_pid = _kb._verify_systemd_scope_worker_pid(
            proc, scope_unit, scope_target, worker_cmd,
        )
        control_group = getattr(verified_pid, "control_group", None)
        if not control_group:
            properties = _kb._systemd_scope_properties(scope_unit, scope_target)
            control_group = properties.get("ControlGroup") if properties else None
        if not control_group:
            raise RuntimeError("systemd scope worker receipt has no control group")
    except Exception as exc:
        with contextlib.suppress(Exception):
            _kb._cleanup_systemd_scope_launch(proc, scope_unit, scope_target)
        raise _kb._WorkerScopeLaunchError(
            str(exc),
            _WorkerLaunchPid(
                proc.pid, launch_mode="systemd-user-scope", scope_unit=scope_unit,
                verification_status="launching", manager_kind=_kb._SYSTEMD_USER_MANAGER_KIND,
                manager_uid=scope_target.uid, launch_acknowledged=False,
                scope_slice=scope_config.slice if scope_config else None,
                memory_high=scope_config.memory_high if scope_config else None,
                memory_max=scope_config.memory_max if scope_config else None,
                memory_swap_max=scope_config.memory_swap_max if scope_config else None,
                tasks_max=scope_config.tasks_max if scope_config else None,
                oom_policy=scope_config.oom_policy if scope_config else None,
                control_group=control_group,
            ),
        ) from exc
    return _WorkerLaunchPid(
        int(verified_pid), launch_mode="systemd-user-scope", scope_unit=scope_unit,
        verification_status="verified", manager_kind=_kb._SYSTEMD_USER_MANAGER_KIND,
        manager_uid=scope_target.uid, launch_acknowledged=True,
        scope_slice=scope_config.slice if scope_config else None,
        memory_high=scope_config.memory_high if scope_config else None,
        memory_max=scope_config.memory_max if scope_config else None,
        memory_swap_max=scope_config.memory_swap_max if scope_config else None,
        tasks_max=scope_config.tasks_max if scope_config else None,
        oom_policy=scope_config.oom_policy if scope_config else None,
        control_group=control_group,
    )


# ---------------------------------------------------------------------------
# Long-lived dispatcher daemon
# ---------------------------------------------------------------------------

def run_daemon(
    *,
    interval: float = 60.0,
    max_spawn: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stop_event=None,
    on_tick=None,
) -> None:
    """Run the dispatcher in a loop until interrupted.

    Calls :func:`dispatch_once` every ``interval`` seconds; exits cleanly on
    SIGINT / SIGTERM so it is systemd-friendly. ``stop_event`` and ``on_tick``
    are test hooks. Each tick resolves ``kanban.max_in_progress`` exactly like
    the gateway dispatcher and ``hermes kanban dispatch`` — the standalone
    daemon must not be the one uncapped entry point.
    """
    import threading

    if stop_event is None:
        stop_event = threading.Event()

    def _handle(_signum, _frame):
        stop_event.set()

    # Install handlers only on the main thread — tests call this inline from
    # worker threads and signal() would raise there.
    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, _handle)

    while not stop_event.is_set():
        try:
            # Re-resolved every tick (config load is mtime-cached) so operator
            # edits apply without a restart.
            max_in_progress = resolve_max_in_progress(configured_max_in_progress())
            with contextlib.closing(_kbc.connect()) as conn:
                res = dispatch_once(
                    conn,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                )
            if on_tick is not None:
                with contextlib.suppress(Exception):
                    on_tick(res)
        except Exception:
            # Don't let any single tick kill the daemon.
            import traceback
            traceback.print_exc()
        stop_event.wait(timeout=interval)


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb  # noqa: E402
from hermes_cli import kanban_db_connect as _kbc  # noqa: E402
from hermes_cli import kanban_db_workspace as _kbw  # noqa: E402
from hermes_cli.kanban_db_worker_scope import (  # noqa: E402
    _SYSTEMD_RESOURCE_VALUE_RE,
    _SYSTEMD_SCOPE_CLEANUP_TIMEOUT,
    _SYSTEMD_SCOPE_MIN_VERSION,
    _SYSTEMD_SCOPE_PROBE_TIMEOUT,
    _SYSTEMD_SCOPE_VERIFY_TIMEOUT,
    _SYSTEMD_USER_MANAGER_KIND,
    _SYSTEMD_WORKER_SCOPE_PREFIX,
    _SYSTEMD_WORKER_SCOPE_RE,
    _SYSTEMD_WORKER_SLICE_RE,
    _WorkerLaunchPid,
    _WorkerScopeConfig,
    _WorkerScopeLaunchError,
    _WorkerScopeMode,
    _WorkerScopeReceipt,
    _WorkerScopeRelease,
    _SystemdUserManagerTarget,
    _VerifiedWorkerPid,
    _abort_unpersisted_worker_launch,
    _clear_worker_launching,
    _cleanup_systemd_scope_launch,
    _current_cgroup_path,
    _ensure_worker_launch_identity,
    _mark_worker_launch_cleanup_pending,
    _persisted_worker_scope,
    _prepare_task_scope_release,
    _process_cgroup_path,
    _process_command_argv,
    _process_command_matches,
    _request_scoped_terminal_transition,
    _scope_absence_confirmed,
    _scope_release_result,
    _scope_release_result_for_receipt,
    _set_worker_launching,
    _stop_persisted_scope_for_release,
    _stop_systemd_scope,
    _systemd_resource_value_bytes,
    _systemd_run_version,
    _systemd_scope_argv,
    _systemd_scope_preflight,
    _systemd_scope_process_ids,
    _systemd_scope_properties,
    _systemd_scope_state,
    _systemd_scope_unit_name,
    _systemd_user_manager_environment,
    _systemd_user_manager_reachable,
    _systemd_user_manager_target_for_cgroup,
    _systemd_user_manager_target_for_uid,
    _termination_metadata_without_pid_signal,
    _valid_scope_resource_receipt,
    _verify_systemd_scope_worker_pid,
    _worker_scope_config,
    _worker_scope_runtime_status,
    reconcile_worker_scope_terminals,
)
