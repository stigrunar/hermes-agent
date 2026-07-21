"""Exact, fail-closed process identity helpers for Kanban worker reaping.

Numeric PIDs are deliberately never treated as ownership.  Every owned
process is represented by ``pid`` plus the kernel-backed process create time.
The helpers in this module have no database knowledge; ``kanban_db`` owns the
durable lease and terminal-transition journal.
"""

from __future__ import annotations

import math
import os
import signal
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: float
    parent_pid: Optional[int] = None
    parent_create_time: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "pid": self.pid,
            "create_time": self.create_time,
        }
        if self.parent_pid is not None:
            value["parent_pid"] = self.parent_pid
        if self.parent_create_time is not None:
            value["parent_create_time"] = self.parent_create_time
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Optional["ProcessIdentity"]:
        try:
            pid_raw = value["pid"]
            create_time_raw = value["create_time"]
            if type(pid_raw) is not int or type(create_time_raw) not in {int, float}:
                return None
            pid = int(pid_raw)
            create_time = float(create_time_raw)
            parent_pid = value.get("parent_pid")
            parent_create_time = value.get("parent_create_time")
            if pid <= 0 or create_time <= 0 or not math.isfinite(create_time):
                return None
            if parent_pid is not None and (
                type(parent_pid) is not int or int(parent_pid) <= 0
            ):
                return None
            if (
                parent_create_time is not None
                and (
                    type(parent_create_time) not in {int, float}
                    or
                    float(parent_create_time) <= 0
                    or not math.isfinite(float(parent_create_time))
                )
            ):
                return None
            return cls(
                pid=pid,
                create_time=create_time,
                parent_pid=(int(parent_pid) if parent_pid is not None else None),
                parent_create_time=(
                    float(parent_create_time)
                    if parent_create_time is not None else None
                ),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return None


@dataclass(frozen=True)
class TreeCapture:
    state: str
    targets: tuple[ProcessIdentity, ...]
    reason: Optional[str] = None


def _psutil():
    import psutil  # type: ignore

    return psutil


def read_identity(pid: int) -> Optional[ProcessIdentity]:
    """Return one exact live identity, or ``None`` when it cannot be read."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        psutil = _psutil()
        proc = psutil.Process(pid)
        create_time = float(proc.create_time())
        if create_time <= 0:
            return None
        parent_pid = int(proc.ppid()) or None
        return ProcessIdentity(pid, create_time, parent_pid=parent_pid)
    except Exception:
        return None


def identity_state(identity: ProcessIdentity) -> str:
    """Return ``alive``, ``gone``, ``reused``, or ``unknown``.

    ``reused`` proves the owned process is gone but the numeric PID now belongs
    to somebody else.  Callers may treat that as non-ownership, never as a
    signalling target.
    """
    try:
        psutil = _psutil()
        proc = psutil.Process(identity.pid)
        observed = float(proc.create_time())
        try:
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return "gone"
        except psutil.NoSuchProcess:
            return "gone"
        except (psutil.AccessDenied, psutil.ZombieProcess):
            return "unknown"
        return "alive" if observed == identity.create_time else "reused"
    except Exception as exc:
        try:
            psutil = _psutil()
            if isinstance(exc, psutil.NoSuchProcess):
                return "gone"
            if isinstance(exc, (psutil.AccessDenied, psutil.ZombieProcess)):
                return "unknown"
        except Exception:
            pass
        return "unknown"


def capture_process_tree(
    root: ProcessIdentity,
    previous: Iterable[ProcessIdentity] = (),
) -> TreeCapture:
    """Capture exact descendants derived from the owned root/previous tree.

    A new process is admitted only when its current PPID is an already-owned
    live identity.  Previously captured detached/reparented descendants remain
    in the set by exact identity.  Session and process-group ids are never used
    for ownership or signalling.
    """
    root_state = identity_state(root)
    prior = {(item.pid, item.create_time): item for item in previous}
    prior[(root.pid, root.create_time)] = root
    if root_state == "reused":
        return TreeCapture("reused", tuple(prior.values()), "root_pid_reused")
    if root_state == "unknown":
        return TreeCapture("unknown", tuple(prior.values()), "root_identity_unknown")
    if root_state == "gone":
        # A root that disappeared before any descendant census is complete is
        # not proof that the tree is gone. A child can fork and reparent
        # between observations, including after the last successful census.
        # Only an independently trusted managed scope can close that gap.
        return TreeCapture(
            "incomplete", tuple(prior.values()),
            "root_gone_before_tree_capture",
        )

    try:
        psutil = _psutil()
        observed: list[ProcessIdentity] = []
        for proc in psutil.process_iter(["pid", "ppid", "create_time", "status"]):
            try:
                info = proc.info
                if info.get("status") == psutil.STATUS_ZOMBIE:
                    continue
                pid = int(info["pid"])
                ppid = int(info.get("ppid") or 0) or None
                create_time = float(info["create_time"])
                if pid <= 0 or create_time <= 0 or not math.isfinite(create_time):
                    return TreeCapture(
                        "unknown", tuple(prior.values()),
                        "process_table_malformed",
                    )
                observed.append(ProcessIdentity(pid, create_time, parent_pid=ppid))
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except (psutil.AccessDenied, KeyError, TypeError, ValueError):
                # We cannot prove whether an unreadable row is a descendant.
                return TreeCapture(
                    "unknown", tuple(prior.values()), "process_table_unreadable"
                )
    except Exception:
        return TreeCapture("unknown", tuple(prior.values()), "process_table_unavailable")

    owned = dict(prior)
    owned_pids = {item.pid for item in owned.values()}
    changed = True
    while changed:
        changed = False
        for item in observed:
            key = (item.pid, item.create_time)
            if key in owned or item.parent_pid not in owned_pids:
                continue
            parent = next(
                (candidate for candidate in owned.values() if candidate.pid == item.parent_pid),
                None,
            )
            if parent is None or identity_state(parent) != "alive":
                continue
            owned[key] = ProcessIdentity(
                item.pid,
                item.create_time,
                parent_pid=parent.pid,
                parent_create_time=parent.create_time,
            )
            owned_pids.add(item.pid)
            changed = True
    return TreeCapture("captured", tuple(owned.values()))


def protected_process_identities(extra_pids: Iterable[int] = ()) -> tuple[set[int], set[tuple[int, float]]]:
    """Return protected PIDs and exact identities for control-plane processes."""
    pids: set[int] = {os.getpid()}
    try:
        for value in extra_pids:
            if type(value) is not int or value <= 0:
                raise ValueError("protected_service_pid_malformed")
            pids.add(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("protected_service_pid_malformed") from exc
    try:
        psutil = _psutil()
        current = psutil.Process(os.getpid())
        pids.update(int(proc.pid) for proc in current.parents())
    except Exception as exc:
        # The DB layer converts this into an unknown protection census and
        # fences the candidate. Silently returning only self would make an
        # unreadable ancestor look safe to reap.
        raise RuntimeError("control_plane_ancestry_unreadable") from exc
    identities: set[tuple[int, float]] = set()
    for pid in pids:
        identity = read_identity(pid)
        if identity is None:
            raise RuntimeError("protected_process_identity_unreadable")
        identities.add((identity.pid, identity.create_time))
    return pids, identities


def signal_exact(
    identity: ProcessIdentity,
    sig: int,
    *,
    protected_pids: set[int],
    protected_identities: set[tuple[int, float]],
    signal_fn=None,
    pre_signal_fn=None,
) -> str:
    """Signal only an exact, currently-owned, non-protected identity."""
    if (
        identity.pid in protected_pids
        or (identity.pid, identity.create_time) in protected_identities
    ):
        return "protected"
    # Tests and callers that need deterministic injection may provide a
    # sender seam. The production path is always kernel-bound: opening a
    # pidfd binds the signal target to this process identity, so PID reuse
    # between the open and delivery cannot redirect the signal. The expected
    # create time is revalidated while that pidfd remains open, before send.
    try:
        if signal_fn is not None:
            state = identity_state(identity)
            if state != "alive":
                return state
            if pre_signal_fn is not None and not pre_signal_fn():
                return "lease_lost"
            state = identity_state(identity)
            if state != "alive":
                return state
            signal_fn(identity.pid, sig)
            return identity_state(identity)
        else:
            pidfd_open = getattr(os, "pidfd_open", None)
            pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
            if pidfd_open is None or pidfd_send_signal is None:
                return "unknown"
            pidfd = pidfd_open(identity.pid, 0)
            try:
                state = identity_state(identity)
                if state != "alive":
                    return state
                if pre_signal_fn is not None and not pre_signal_fn():
                    return "lease_lost"
                state = identity_state(identity)
                if state != "alive":
                    return state
                pidfd_send_signal(pidfd, sig)
            finally:
                os.close(pidfd)
    except ProcessLookupError:
        return "gone"
    except (PermissionError, OSError):
        return "unknown"
    # The identity is checked again after signal delivery. A reused PID can
    # never be carried forward as if the original worker survived.
    return identity_state(identity)


TERM_SIGNAL = signal.SIGTERM
KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
