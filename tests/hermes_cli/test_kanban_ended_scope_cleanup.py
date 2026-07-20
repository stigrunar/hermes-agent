"""Ended-run transient scope reconciliation regressions."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _target() -> kb._SystemdUserManagerTarget:
    uid = os.getuid()
    return kb._SystemdUserManagerTarget(
        manager_kind=kb._SYSTEMD_USER_MANAGER_KIND,
        manager_uid=uid,
        runtime_dir=Path(f"/run/user/{uid}"),
        bus_path=Path(f"/run/user/{uid}/bus"),
    )


def _scoped_run(
    conn,
    db_path: Path,
    *,
    title: str,
    legacy: bool = False,
    final_status: str = "done",
) -> tuple[str, int, str]:
    task_id = kb.create_task(conn, title=title, assignee="worker")
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    assert claimed.current_run_id is not None
    run_id = claimed.current_run_id
    opaque_unit = kb._worker_scope_unit_name(task_id, run_id, db_path=db_path)
    if legacy:
        assert kb._set_worker_pid(conn, task_id, 12345)
        conn.execute(
            "UPDATE task_events SET payload=? "
            "WHERE task_id=? AND run_id=? AND kind='spawned'",
            (json.dumps({"pid": 12345}), task_id, run_id),
        )
        unit = kb._legacy_worker_scope_unit_name(task_id, run_id)
    else:
        assert kb._set_worker_pid(
            conn,
            task_id,
            kb._WorkerLaunchPid(
                12345,
                scope_unit=opaque_unit,
                manager_kind=kb._SYSTEMD_USER_MANAGER_KIND,
                manager_uid=os.getuid(),
                launch_acknowledged=True,
            ),
        )
        unit = opaque_unit
    if final_status == "done":
        assert kb.complete_task(
            conn,
            task_id,
            result="finished",
            expected_run_id=run_id,
        )
    elif final_status == "ready":
        assert kb.block_task(
            conn,
            task_id,
            reason="retry later",
            expected_run_id=run_id,
        )
        assert kb.unblock_task(conn, task_id)
        assert kb.get_task(conn, task_id).status == "ready"
    else:  # pragma: no cover - test helper misuse
        raise AssertionError(final_status)
    conn.execute(
        "UPDATE task_runs SET ended_at=? WHERE id=?",
        (int(time.time()) - 30, run_id),
    )
    conn.commit()
    return task_id, run_id, unit


def _install_manager(monkeypatch: pytest.MonkeyPatch, target) -> None:
    monkeypatch.setattr(
        kb,
        "_resolve_systemd_user_manager_target",
        lambda kind, uid, **_kwargs: (
            target
            if kind == target.manager_kind and uid == target.manager_uid
            else None
        ),
    )
    monkeypatch.setattr(kb, "_current_systemd_user_manager_target", lambda: target)


def test_dispatch_collects_ended_legacy_scope_once(kanban_home, monkeypatch):
    db_path = kanban_home / "kanban.db"
    target = _target()
    _install_manager(monkeypatch, target)
    stopped: list[tuple[str, kb._SystemdUserManagerTarget]] = []

    with kb.connect() as conn:
        task_id, run_id, legacy_unit = _scoped_run(
            conn,
            db_path,
            title="legacy descendants",
            legacy=True,
        )
        opaque_unit = kb._worker_scope_unit_name(task_id, run_id, db_path=db_path)

        def state(unit, *, manager_target, **_kwargs):
            assert manager_target is target
            if any(candidate == unit for candidate, _ in stopped):
                return "not-found"
            return "active" if unit == legacy_unit else "not-found"

        monkeypatch.setattr(kb, "_systemd_user_scope_state", state)
        monkeypatch.setattr(
            kb,
            "_stop_systemd_user_scope",
            lambda unit, *, manager_target: (
                stopped.append((unit, manager_target)) or True
            ),
        )

        first = kb.dispatch_once(conn, max_spawn=0)
        second = kb.dispatch_once(conn, max_spawn=0)

        assert opaque_unit != legacy_unit
        assert stopped == [(legacy_unit, target)]
        assert first.scope_cleanup == [{
            "task_id": task_id,
            "run_id": run_id,
            "action": "collected",
            "reason": "ended_scope_stopped",
        }]
        assert second.scope_cleanup == []
        finalized = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == kb._WORKER_SCOPE_CLEANUP_FINAL_EVENT
        ]
        assert len(finalized) == 1
        assert finalized[0].run_id == run_id


def test_dispatch_collects_validated_persisted_scope(kanban_home, monkeypatch):
    db_path = kanban_home / "kanban.db"
    target = _target()
    _install_manager(monkeypatch, target)
    stopped = []
    monkeypatch.setattr(
        kb,
        "_systemd_user_scope_state",
        lambda _unit, *, manager_target, **_kwargs: (
            "active" if manager_target is target else "unknown"
        ),
    )
    monkeypatch.setattr(
        kb,
        "_stop_systemd_user_scope",
        lambda unit, *, manager_target: stopped.append((unit, manager_target)) or True,
    )

    with kb.connect() as conn:
        task_id, run_id, unit = _scoped_run(
            conn,
            db_path,
            title="opaque descendants",
        )
        result = kb.dispatch_once(conn, max_spawn=0)

    assert stopped == [(unit, target)]
    assert result.scope_cleanup == [{
        "task_id": task_id,
        "run_id": run_id,
        "action": "collected",
        "reason": "ended_scope_stopped",
    }]


def test_current_running_run_is_never_selected(kanban_home, monkeypatch):
    db_path = kanban_home / "kanban.db"
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="still running", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        unit = kb._worker_scope_unit_name(
            task_id,
            claimed.current_run_id,
            db_path=db_path,
        )
        assert kb._set_worker_pid(
            conn,
            task_id,
            kb._WorkerLaunchPid(
                12345,
                scope_unit=unit,
                manager_kind=kb._SYSTEMD_USER_MANAGER_KIND,
                manager_uid=os.getuid(),
            ),
        )
        # Even a corrupt/partially-written ended_at must not outweigh the
        # task's live current-run ownership pointer.
        conn.execute(
            "UPDATE task_runs SET ended_at=? WHERE id=?",
            (int(time.time()) - 30, claimed.current_run_id),
        )
        conn.commit()
        monkeypatch.setattr(
            kb,
            "_worker_scope_state",
            lambda *_args, **_kwargs: pytest.fail("live run must not be inspected"),
        )
        monkeypatch.setattr(
            kb,
            "_stop_systemd_user_scope",
            lambda *_args, **_kwargs: pytest.fail("live run must not be stopped"),
        )

        assert kb._reconcile_ended_worker_scopes(
            conn,
            process_effects=True,
            db_path=db_path,
        ) == []
        assert kb.get_task(conn, task_id).status == "running"


def test_dry_run_reports_but_does_not_stop_scope(kanban_home, monkeypatch):
    db_path = kanban_home / "kanban.db"
    target = _target()
    _install_manager(monkeypatch, target)
    monkeypatch.setattr(
        kb,
        "_systemd_user_scope_state",
        lambda _unit, **_kwargs: "active",
    )
    monkeypatch.setattr(
        kb,
        "_stop_systemd_user_scope",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not stop a scope"),
    )

    with kb.connect() as conn:
        task_id, run_id, _ = _scoped_run(conn, db_path, title="preview only")
        result = kb.dispatch_once(conn, dry_run=True, max_spawn=0)
        assert result.scope_cleanup == [{
            "task_id": task_id,
            "run_id": run_id,
            "action": "would_collect",
            "reason": "ended_scope_active",
        }]
        assert not any(
            event.kind == kb._WORKER_SCOPE_CLEANUP_FINAL_EVENT
            for event in kb.list_events(conn, task_id)
        )


def test_post_end_grace_defers_then_collects(kanban_home, monkeypatch):
    db_path = kanban_home / "kanban.db"
    target = _target()
    _install_manager(monkeypatch, target)
    stopped = []
    monkeypatch.setattr(kb, "_systemd_user_scope_state", lambda *_a, **_kw: "active")
    monkeypatch.setattr(
        kb,
        "_stop_systemd_user_scope",
        lambda unit, **_kwargs: stopped.append(unit) or True,
    )

    with kb.connect() as conn:
        task_id, run_id, unit = _scoped_run(conn, db_path, title="final response grace")
        now = int(time.time())
        conn.execute("UPDATE task_runs SET ended_at=? WHERE id=?", (now, run_id))
        conn.commit()

        deferred = kb._reconcile_ended_worker_scopes(
            conn,
            process_effects=True,
            db_path=db_path,
            now=now,
        )
        collected = kb._reconcile_ended_worker_scopes(
            conn,
            process_effects=True,
            db_path=db_path,
            now=now + kb._ENDED_WORKER_SCOPE_POST_END_GRACE_SECONDS + 1,
        )

    assert stopped == [unit]
    assert deferred == [{
        "task_id": task_id,
        "run_id": run_id,
        "action": "deferred",
        "reason": "post_end_grace",
    }]
    assert collected[0]["action"] == "collected"


@pytest.mark.parametrize(
    "case",
    [
        "invalid_receipt",
        "manager_mismatch",
        "cross_board",
        "unknown_state",
        "manager_unavailable",
    ],
)
def test_untrusted_or_unknown_scope_fails_closed(
    kanban_home,
    monkeypatch,
    case,
):
    db_path = kanban_home / "kanban.db"
    target = _target()
    _install_manager(monkeypatch, target)

    with kb.connect() as conn:
        task_id, run_id, unit = _scoped_run(conn, db_path, title=case)
        payload = {
            "pid": 12345,
            "launch_mode": "systemd-user-scope",
            "scope_unit": unit,
            "manager_kind": kb._SYSTEMD_USER_MANAGER_KIND,
            "manager_uid": os.getuid(),
            "launch_acknowledged": True,
        }
        if case == "invalid_receipt":
            payload["scope_unit"] = "not-a-worker.scope"
        elif case == "manager_mismatch":
            payload["manager_uid"] = os.getuid() + 1
        elif case == "cross_board":
            payload["scope_unit"] = kb._worker_scope_unit_name(
                task_id,
                run_id,
                db_path=kanban_home / "other-board" / "kanban.db",
            )
        elif case == "manager_unavailable":
            monkeypatch.setattr(kb, "_systemd_user_scope_possible", lambda: False)
        conn.execute(
            "UPDATE task_events SET payload=? "
            "WHERE task_id=? AND run_id=? AND kind='spawned'",
            (json.dumps(payload), task_id, run_id),
        )
        conn.commit()
        monkeypatch.setattr(
            kb,
            "_systemd_user_scope_state",
            lambda *_args, **_kwargs: "unknown",
        )
        monkeypatch.setattr(
            kb,
            "_stop_systemd_user_scope",
            lambda *_args, **_kwargs: pytest.fail("untrusted scope must not be stopped"),
        )

        result = kb._reconcile_ended_worker_scopes(
            conn,
            process_effects=True,
            db_path=db_path,
        )

        assert result == [{
            "task_id": task_id,
            "run_id": run_id,
            "action": "deferred",
            "reason": "scope_state_unknown",
        }]
        assert kb._task_has_pending_ended_worker_scope(conn, task_id)


@pytest.mark.parametrize(
    ("failure_mode", "failure_reason"),
    [
        ("unknown", "scope_state_unknown"),
        ("stop_incomplete", "scope_stop_incomplete"),
    ],
)
def test_unknown_or_stuck_scope_does_not_block_other_cleanup_or_dispatch(
    kanban_home,
    monkeypatch,
    failure_mode,
    failure_reason,
):
    db_path = kanban_home / "kanban.db"
    target = _target()
    _install_manager(monkeypatch, target)
    stopped = []
    spawned = []

    with kb.connect() as conn:
        unknown_task, unknown_run, unknown_unit = _scoped_run(
            conn,
            db_path,
            title="unknown retry",
            final_status="ready",
        )
        good_task, good_run, good_unit = _scoped_run(
            conn,
            db_path,
            title="collectable",
        )
        unrelated = kb.create_task(conn, title="ordinary work", assignee="worker")

        def state(unit, **_kwargs):
            if unit == unknown_unit and failure_mode == "unknown":
                return "unknown"
            return "active" if unit in {unknown_unit, good_unit} else "not-found"

        monkeypatch.setattr(kb, "_systemd_user_scope_state", state)

        def stop(unit, **_kwargs):
            stopped.append(unit)
            return unit != unknown_unit

        monkeypatch.setattr(
            kb,
            "_stop_systemd_user_scope",
            stop,
        )

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id) or 4242,
            max_spawn=2,
            profile_roster=lambda _name: True,
        )

        assert stopped == (
            [good_unit]
            if failure_mode == "unknown"
            else [unknown_unit, good_unit]
        )
        assert spawned == [unrelated]
        assert kb.get_task(conn, unknown_task).status == "ready"
        assert kb.get_task(conn, good_task).status == "done"
        assert {item["action"] for item in result.scope_cleanup} >= {
            "deferred",
            "collected",
            "spawn_deferred",
        }
        assert any(
            item.get("task_id") == unknown_task
            and item.get("run_id") == unknown_run
            and item.get("reason") == failure_reason
            for item in result.scope_cleanup
        )
        assert any(
            item.get("task_id") == good_task
            and item.get("run_id") == good_run
            and item.get("action") == "collected"
            for item in result.scope_cleanup
        )
