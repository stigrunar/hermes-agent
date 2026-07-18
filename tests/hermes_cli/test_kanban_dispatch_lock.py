"""Tests for the kanban dispatcher single-writer lock (issue #35240).

A ``hermes gateway run --replace`` / ``gateway restart`` from a shell on a
systemd/launchd host can leave an orphan dispatcher that escapes the
service cgroup, survives ``systemctl restart``, and becomes a second
long-lived writer on the same ``kanban.db`` — the documented root cause of
multi-writer SQLite WAL corruption. ``dispatch_once`` now wraps each tick in
a non-blocking, board-scoped dispatch lock so two dispatchers can never run
a reclaim/spawn/write tick concurrently. The losing dispatcher returns an
empty ``DispatchResult`` with ``skipped_locked=True`` and does no DB writes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def _lock_path(board: str = "default") -> Path:
    db_path = kb.kanban_db_path(board=board)
    return db_path.with_name(db_path.name + ".dispatch.lock")


def _assert_rejected_tick(
    conn,
    *,
    expected_error: str | None = None,
    expected_contention: bool = False,
    task_id: str | None = None,
):
    if task_id is None:
        task_id = kb.create_task(conn, title="must remain ready", assignee="worker")
    before = conn.execute(
        "SELECT status, claim_lock, current_run_id, worker_pid FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    before_runs = conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    spawn_calls: list[str] = []

    def spy_spawn(task, workspace_path, board=None):
        spawn_calls.append(getattr(task, "id", task))
        return 4242

    result = kb.dispatch_once(conn, spawn_fn=spy_spawn)
    after = conn.execute(
        "SELECT status, claim_lock, current_run_id, worker_pid FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    after_runs = conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
    ).fetchone()[0]

    assert result.skipped_locked is expected_contention
    assert result.dispatch_lock_error == expected_error
    assert result.reclaimed == 0
    assert result.promoted == 0
    assert result.spawned == []
    assert spawn_calls == []
    assert tuple(after) == tuple(before)
    assert after["status"] == "ready"
    assert after["claim_lock"] is None
    assert after["current_run_id"] is None
    assert after["worker_pid"] is None
    assert before_runs == after_runs == 0
    return result


def test_uncontended_tick_runs_and_is_not_skipped(conn, monkeypatch):
    """A trusted, uncontended lock preserves the normal spawn path."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    task_id = kb.create_task(conn, title="t", assignee="w")
    spawn_calls: list[str] = []

    def fake_spawn(task, _workspace_path, board=None):
        spawn_calls.append(task.id)
        return 4242

    result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
    task = kb.get_task(conn, task_id)

    assert result.skipped_locked is False
    assert result.dispatch_lock_error is None
    assert [item[0] for item in result.spawned] == [task_id]
    assert spawn_calls == [task_id]
    assert task.status == "running"
    assert task.claim_lock is not None
    assert task.current_run_id is not None
    assert task.worker_pid == 4242
    assert conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
    ).fetchone()[0] == 1


def test_lock_open_failure_fails_closed(conn, monkeypatch):
    """An unusable lock file must never degrade to an unguarded tick."""
    monkeypatch.setattr(
        kb,
        "_open_dispatch_lock_fd",
        lambda _path: (_ for _ in ()).throw(PermissionError("sensitive path")),
    )

    _assert_rejected_tick(conn, expected_error="open_failed")


def test_db_path_resolution_failure_fails_closed(conn, monkeypatch):
    """Failure to derive the board lock identity must stop dispatch."""
    monkeypatch.setattr(
        kb,
        "kanban_db_path",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("sensitive path")),
    )

    _assert_rejected_tick(conn, expected_error="path_resolution_failed")


def test_symlink_lock_path_is_rejected_without_following(conn, kanban_home):
    target = kanban_home / "attacker-controlled-lock"
    target.write_bytes(b"")
    lock_path = _lock_path()
    lock_path.symlink_to(target)

    _assert_rejected_tick(conn, expected_error="untrusted_type")


def test_non_regular_lock_path_is_rejected(conn):
    _lock_path().mkdir()

    _assert_rejected_tick(conn, expected_error="untrusted_type")


def test_fstat_classification_error_fails_closed(conn, monkeypatch):
    monkeypatch.setattr(
        kb,
        "_inspect_dispatch_lock_fd",
        lambda _fd: (_ for _ in ()).throw(OSError("sensitive classification")),
    )

    _assert_rejected_tick(conn, expected_error="classification_failed")


@pytest.mark.skipif(
    kb._IS_WINDOWS or not hasattr(os, "geteuid"),
    reason="POSIX effective owner identity is unavailable",
)
def test_wrong_lock_owner_fails_closed(conn, monkeypatch):
    real_euid = os.geteuid()
    monkeypatch.setattr(kb, "_dispatch_effective_uid", lambda: real_euid + 1)

    _assert_rejected_tick(conn, expected_error="untrusted_owner")


@pytest.mark.skipif(kb._IS_WINDOWS, reason="POSIX permission bits do not apply")
def test_group_or_other_writable_lock_fails_closed(conn):
    lock_path = _lock_path()
    lock_path.touch(mode=0o600)
    lock_path.chmod(0o622)

    _assert_rejected_tick(conn, expected_error="unsafe_permissions")


@pytest.mark.skipif(kb._IS_WINDOWS, reason="uses POSIX flock replacement hook")
def test_inode_replacement_after_acquisition_fails_closed(conn, monkeypatch):
    import fcntl

    lock_path = _lock_path()
    real_flock = fcntl.flock
    replaced = False

    def replace_then_flock(fd, operation):
        nonlocal replaced
        if operation & fcntl.LOCK_EX and not replaced:
            replaced = True
            lock_path.unlink()
            lock_path.touch(mode=0o600)
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", replace_then_flock)

    _assert_rejected_tick(conn, expected_error="identity_mismatch")
    assert replaced is True


@pytest.mark.skipif(kb._IS_WINDOWS, reason="uses POSIX flock failure hook")
def test_lock_acquisition_os_error_fails_closed(conn, monkeypatch):
    import fcntl

    def fail_flock(_fd, operation):
        if operation & fcntl.LOCK_EX:
            raise OSError(5, "sensitive acquisition failure")

    monkeypatch.setattr(fcntl, "flock", fail_flock)

    _assert_rejected_tick(conn, expected_error="acquisition_failed")


def test_held_lock_skips_the_tick_without_writes(conn):
    """While another holder owns the board lock, dispatch_once must skip and
    must NOT invoke spawn_fn (no DB writes happen on a skipped tick)."""
    task_id = kb.create_task(conn, title="must remain ready", assignee="worker")
    db_path = kb.kanban_db_path(board="default")

    # Hold the lock, then attempt a contended tick.
    with kb._dispatch_tick_lock(db_path) as held:
        assert held.acquired is True  # we genuinely acquired it
        result = _assert_rejected_tick(
            conn, expected_contention=True, task_id=task_id
        )

    assert result.skipped_locked is True
    assert result.dispatch_lock_error is None
    assert result.spawned == []


def test_lock_releases_so_next_tick_runs(conn):
    """After the holder releases, the next tick is no longer skipped."""
    kb.create_task(conn, title="t", assignee="w")
    db_path = kb.kanban_db_path(board="default")

    with kb._dispatch_tick_lock(db_path) as held:
        assert held.acquired is True
        assert kb.dispatch_once(conn).skipped_locked is True

    # Lock released — a fresh tick proceeds.
    assert kb.dispatch_once(conn).skipped_locked is False


def test_lock_is_board_scoped(conn):
    """Holding board A's dispatch lock must not block a tick on board B —
    distinct boards have distinct DB files and tick independently."""
    db_default = kb.kanban_db_path(board="default")
    db_other = db_default.with_name("other-board-kanban.db")

    # Two different lock files → both acquirable simultaneously.
    with kb._dispatch_tick_lock(db_default) as held_a:
        assert held_a.acquired is True
        with kb._dispatch_tick_lock(db_other) as held_b:
            assert held_b.acquired is True, "a lock on a different board must be independent"


def test_reentrant_same_path_lock_is_exclusive(conn):
    """A second acquisition of the SAME board's lock from a sibling context
    must report not-held (the flock is exclusive within the host)."""
    db_path = kb.kanban_db_path(board="default")
    with kb._dispatch_tick_lock(db_path) as held_a:
        assert held_a.acquired is True
        with kb._dispatch_tick_lock(db_path) as held_b:
            assert held_b.acquired is False, "same-board lock must be exclusive"
            assert held_b.reason == "contention"
