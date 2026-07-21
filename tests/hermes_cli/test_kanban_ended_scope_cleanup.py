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
        task_status = "done"
        run_status = "completed"
        run_outcome = "completed"
    elif final_status == "ready":
        assert kb.block_task(
            conn,
            task_id,
            reason="retry later",
            expected_run_id=run_id,
        )
        task_status = "ready"
        run_status = "blocked"
        run_outcome = "blocked"
    else:  # pragma: no cover - test helper misuse
        raise AssertionError(final_status)
    # These tests exercise cleanup of already-ended historical scopes, not the
    # Phase B reaper itself. Materialize the post-reap fixture explicitly so
    # scope cleanup cannot run before terminal finalization.
    ended_at = int(time.time()) - 30
    conn.execute(
        "UPDATE tasks SET status=?, current_run_id=NULL, claim_lock=NULL, "
        "claim_expires=NULL, worker_pid=NULL, last_heartbeat_at=NULL, "
        "completed_at=? WHERE id=?",
        (
            task_status,
            ended_at if task_status == "done" else None,
            task_id,
        ),
    )
    conn.execute(
        "UPDATE task_runs SET status=?, outcome=?, ended_at=?, "
        "reap_state='finalized', reap_completed_at=? WHERE id=?",
        (run_status, run_outcome, ended_at, ended_at, run_id),
    )
    assert kb.get_task(conn, task_id).status == task_status
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


def test_classified_legacy_direct_run_with_unavailable_manager_respawns_once(
    kanban_home,
    monkeypatch,
):
    """The reviewer canary has a finite, explicit compatibility escape."""
    db_path = kanban_home / "kanban.db"
    spawned = []
    monkeypatch.setattr(kb, "_systemd_user_scope_possible", lambda: False)
    monkeypatch.setattr(kb, "_local_legacy_classifier_identity", lambda: "host-a")
    monkeypatch.setattr(
        kb,
        "_locally_present_legacy_worker_scope",
        lambda _units: (None, "not-found"),
    )
    monkeypatch.setattr(
        kb,
        "_systemd_user_scope_state",
        lambda *_args, **_kwargs: pytest.fail(
            "an unavailable manager must not be queried"
        ),
    )
    monkeypatch.setattr(
        kb,
        "_stop_systemd_user_scope",
        lambda *_args, **_kwargs: pytest.fail(
            "a classified direct run must not stop a scope"
        ),
    )

    with kb.connect() as conn:
        task_id, run_id, _ = _scoped_run(
            conn,
            db_path,
            title="legacy direct compatibility",
            legacy=True,
            final_status="ready",
        )

        first = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id) or 4242,
            max_spawn=1,
            profile_roster=lambda _name: True,
        )
        second = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id) or 4242,
            max_spawn=1,
            profile_roster=lambda _name: True,
        )

        assert kb.get_task(conn, task_id).status == "ready"
        assert spawned == []
        assert first.scope_cleanup == [
            {
                "task_id": task_id,
                "run_id": run_id,
                "action": "deferred",
                "reason": "scope_state_unknown",
            },
            {
                "task_id": task_id,
                "action": "spawn_deferred",
                "reason": "cleanup_pending",
            },
        ]
        assert second.scope_cleanup == first.scope_cleanup
        assert kb._task_has_pending_ended_worker_scope(conn, task_id)

        assert kb.classify_legacy_worker_run(
            conn,
            task_id,
            run_id,
            launch_mode="direct",
            expected_pid=12345,
        )
        assert kb.classify_legacy_worker_run(
            conn,
            task_id,
            run_id,
            launch_mode="direct",
            expected_pid=12345,
        )

        third = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id) or 4242,
            max_spawn=1,
            profile_roster=lambda _name: True,
        )
        fourth = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id) or 4242,
            max_spawn=1,
            profile_roster=lambda _name: True,
        )

        assert spawned == [task_id]
        assert kb.get_task(conn, task_id).status == "running"
        assert third.scope_cleanup == []
        assert fourth.spawned == []
        finalized = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == kb._WORKER_SCOPE_CLEANUP_FINAL_EVENT
            and event.run_id == run_id
        ]
        assert len(finalized) == 1


def test_classified_legacy_run_with_local_scope_evidence_stays_fenced(
    kanban_home,
    monkeypatch,
):
    db_path = kanban_home / "kanban.db"
    monkeypatch.setattr(kb, "_local_legacy_classifier_identity", lambda: "host-a")
    monkeypatch.setattr(kb, "_systemd_user_scope_possible", lambda: False)
    monkeypatch.setattr(
        kb,
        "_stop_systemd_user_scope",
        lambda *_args, **_kwargs: pytest.fail(
            "cgroup presence without a manager cannot authorize a stop"
        ),
    )

    with kb.connect() as conn:
        task_id, run_id, legacy_unit = _scoped_run(
            conn,
            db_path,
            title="legacy scope cgroup evidence",
            legacy=True,
            final_status="ready",
        )
        assert kb.classify_legacy_worker_run(
            conn,
            task_id,
            run_id,
            launch_mode="direct",
            expected_pid=12345,
        )
        monkeypatch.setattr(
            kb,
            "_locally_present_legacy_worker_scope",
            lambda units: (
                (legacy_unit, "active")
                if legacy_unit in units
                else (None, "not-found")
            ),
        )

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args: pytest.fail("evidenced scope must fence respawn"),
            max_spawn=1,
            profile_roster=lambda _name: True,
        )

        assert result.scope_cleanup == [
            {
                "task_id": task_id,
                "run_id": run_id,
                "action": "deferred",
                "reason": "scope_state_unknown",
            },
            {
                "task_id": task_id,
                "action": "spawn_deferred",
                "reason": "cleanup_pending",
            },
        ]
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb._task_has_pending_ended_worker_scope(conn, task_id)


def test_cgroup_without_v2_marker_keeps_classified_legacy_run_fenced(
    kanban_home,
    tmp_path,
    monkeypatch,
):
    db_path = kanban_home / "kanban.db"
    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir()
    monkeypatch.setattr(kb, "_CGROUP_V2_ROOT", cgroup_root)
    monkeypatch.setattr(kb, "_systemd_user_scope_possible", lambda: False)
    monkeypatch.setattr(kb, "_local_legacy_classifier_identity", lambda: "host-a")
    monkeypatch.setattr(
        kb,
        "_stop_systemd_user_scope",
        lambda *_args, **_kwargs: pytest.fail(
            "unknown cgroup evidence must not authorize a stop"
        ),
    )

    with kb.connect() as conn:
        task_id, run_id, legacy_unit = _scoped_run(
            conn,
            db_path,
            title="legacy direct without cgroup v2 proof",
            legacy=True,
            final_status="ready",
        )
        assert kb.classify_legacy_worker_run(
            conn,
            task_id,
            run_id,
            launch_mode="direct",
            expected_pid=12345,
        )

        assert kb._locally_present_legacy_worker_scope([legacy_unit]) == (
            None,
            "unknown",
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args: pytest.fail(
                "unknown cgroup evidence must fence respawn"
            ),
            max_spawn=1,
            profile_roster=lambda _name: True,
        )

        assert result.scope_cleanup == [
            {
                "task_id": task_id,
                "run_id": run_id,
                "action": "deferred",
                "reason": "scope_state_unknown",
            },
            {
                "task_id": task_id,
                "action": "spawn_deferred",
                "reason": "cleanup_pending",
            },
        ]
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb._task_has_pending_ended_worker_scope(conn, task_id)


def test_local_cgroup_evidence_finds_exact_legacy_scope_with_descendants(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "cgroup"
    (root / "cgroup.controllers").parent.mkdir(parents=True)
    (root / "cgroup.controllers").write_text("cpu memory\n", encoding="utf-8")
    user_root = (
        root
        / "user.slice"
        / f"user-{os.getuid()}.slice"
        / f"user@{os.getuid()}.service"
    )
    unit = "hermes-kanban-worker-0123456789abcdef0123456789abcdef.scope"
    scope = user_root / "app.slice" / unit
    scope.mkdir(parents=True)
    (scope / "cgroup.procs").write_text("4242\n", encoding="utf-8")
    monkeypatch.setattr(kb, "_CGROUP_V2_ROOT", root)

    assert kb._locally_present_legacy_worker_scope([unit]) == (unit, "active")
    assert kb._locally_present_legacy_worker_scope([
        "hermes-kanban-worker-ffffffffffffffffffffffffffffffff.scope"
    ]) == (None, "not-found")
    monkeypatch.setattr(kb, "_LEGACY_SCOPE_CGROUP_SCAN_LIMIT", 0)
    assert kb._locally_present_legacy_worker_scope([
        "hermes-kanban-worker-ffffffffffffffffffffffffffffffff.scope"
    ]) == (None, "unknown")


def test_local_cgroup_evidence_rejects_exact_scope_with_unexpected_owner(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "cgroup"
    (root / "cgroup.controllers").parent.mkdir(parents=True)
    (root / "cgroup.controllers").write_text("cpu memory\n", encoding="utf-8")
    user_root = (
        root
        / "user.slice"
        / f"user-{os.getuid()}.slice"
        / f"user@{os.getuid()}.service"
    )
    unit = "hermes-kanban-worker-0123456789abcdef0123456789abcdef.scope"
    scope = user_root / "app.slice" / unit
    scope.mkdir(parents=True)
    real_lstat = os.lstat

    def unexpected_scope_owner(path):
        info = real_lstat(path)
        if Path(path) == scope:
            fields = list(info)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(kb, "_CGROUP_V2_ROOT", root)
    monkeypatch.setattr(os, "lstat", unexpected_scope_owner)

    assert kb._locally_present_legacy_worker_scope([unit]) == (None, "unknown")


@pytest.mark.parametrize(
    "case",
    [
        "cross_host",
        "cross_board",
        "malformed_classification",
        "tampered_receipt",
        "duplicate_classification",
    ],
)
def test_untrusted_legacy_direct_classification_fails_closed(
    kanban_home,
    monkeypatch,
    case,
):
    db_path = kanban_home / "kanban.db"
    monkeypatch.setattr(kb, "_local_legacy_classifier_identity", lambda: "host-a")
    monkeypatch.setattr(kb, "_systemd_user_scope_possible", lambda: False)
    monkeypatch.setattr(
        kb,
        "_locally_present_legacy_worker_scope",
        lambda _u: (None, "not-found"),
    )
    monkeypatch.setattr(
        kb,
        "_stop_systemd_user_scope",
        lambda *_args, **_kwargs: pytest.fail(
            "untrusted classification must not stop a scope"
        ),
    )

    with kb.connect() as conn:
        task_id, run_id, _ = _scoped_run(
            conn,
            db_path,
            title=f"untrusted classification {case}",
            legacy=True,
            final_status="ready",
        )
        assert kb.classify_legacy_worker_run(
            conn,
            task_id,
            run_id,
            launch_mode="direct",
            expected_pid=12345,
        )
        if case == "cross_host":
            monkeypatch.setattr(
                kb,
                "_local_legacy_classifier_identity",
                lambda: "host-b",
            )
        elif case == "cross_board":
            row = conn.execute(
                "SELECT payload FROM task_events "
                "WHERE task_id=? AND run_id=? AND kind=?",
                (task_id, run_id, kb._LEGACY_WORKER_LAUNCH_CLASSIFIED_EVENT),
            ).fetchone()
            payload = json.loads(row["payload"])
            payload["board_identity"] = "0" * 64
            conn.execute(
                "UPDATE task_events SET payload=? "
                "WHERE task_id=? AND run_id=? AND kind=?",
                (
                    json.dumps(payload),
                    task_id,
                    run_id,
                    kb._LEGACY_WORKER_LAUNCH_CLASSIFIED_EVENT,
                ),
            )
        elif case == "malformed_classification":
            conn.execute(
                "UPDATE task_events SET payload=? "
                "WHERE task_id=? AND run_id=? AND kind=?",
                (
                    json.dumps({"version": 1}),
                    task_id,
                    run_id,
                    kb._LEGACY_WORKER_LAUNCH_CLASSIFIED_EVENT,
                ),
            )
        elif case == "tampered_receipt":
            conn.execute(
                "UPDATE task_events SET payload=? "
                "WHERE task_id=? AND run_id=? AND kind='spawned'",
                ('{"pid":12345}', task_id, run_id),
            )
        else:
            row = conn.execute(
                "SELECT payload FROM task_events "
                "WHERE task_id=? AND run_id=? AND kind=?",
                (task_id, run_id, kb._LEGACY_WORKER_LAUNCH_CLASSIFIED_EVENT),
            ).fetchone()
            conn.execute(
                "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    run_id,
                    kb._LEGACY_WORKER_LAUNCH_CLASSIFIED_EVENT,
                    row["payload"],
                    int(time.time()),
                ),
            )
        conn.commit()

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


def test_legacy_classification_rejects_non_pid_only_or_wrong_pid(
    kanban_home,
    monkeypatch,
):
    db_path = kanban_home / "kanban.db"
    monkeypatch.setattr(kb, "_local_legacy_classifier_identity", lambda: "host-a")

    with kb.connect() as conn:
        task_id, run_id, _ = _scoped_run(
            conn,
            db_path,
            title="malformed legacy receipt",
            legacy=True,
        )
        with pytest.raises(ValueError, match="expected PID"):
            kb.classify_legacy_worker_run(
                conn,
                task_id,
                run_id,
                launch_mode="direct",
                expected_pid=54321,
            )
        conn.execute(
            "UPDATE task_events SET payload=? "
            "WHERE task_id=? AND run_id=? AND kind='spawned'",
            (json.dumps({"pid": 12345, "host": "elsewhere"}), task_id, run_id),
        )
        conn.commit()
        with pytest.raises(ValueError, match="exact PID-only"):
            kb.classify_legacy_worker_run(
                conn,
                task_id,
                run_id,
                launch_mode="direct",
                expected_pid=12345,
            )


def test_classified_legacy_direct_dry_run_does_not_mutate_real_db(
    kanban_home,
    monkeypatch,
):
    db_path = kanban_home / "kanban.db"
    monkeypatch.setattr(kb, "_local_legacy_classifier_identity", lambda: "host-a")
    monkeypatch.setattr(kb, "_systemd_user_scope_possible", lambda: False)
    monkeypatch.setattr(
        kb,
        "_locally_present_legacy_worker_scope",
        lambda _u: (None, "not-found"),
    )
    monkeypatch.setattr(
        kb,
        "_stop_systemd_user_scope",
        lambda *_args, **_kwargs: pytest.fail("preview must not stop a scope"),
    )

    with kb.connect() as conn:
        task_id, run_id, _ = _scoped_run(
            conn,
            db_path,
            title="classified preview",
            legacy=True,
            final_status="ready",
        )
        assert kb.classify_legacy_worker_run(
            conn,
            task_id,
            run_id,
            launch_mode="direct",
            expected_pid=12345,
        )

        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_spawn=1,
            profile_roster=lambda _name: True,
        )

        assert [task for task, _profile, _workspace in result.spawned] == [task_id]
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb._task_has_pending_ended_worker_scope(conn, task_id)
        assert not any(
            event.kind == kb._WORKER_SCOPE_CLEANUP_FINAL_EVENT
            and event.run_id == run_id
            for event in kb.list_events(conn, task_id)
        )


def test_modern_direct_receipt_remains_not_applicable_and_not_classifiable(
    kanban_home,
    monkeypatch,
):
    monkeypatch.setattr(kb, "_systemd_user_scope_possible", lambda: False)
    monkeypatch.setattr(kb, "_local_legacy_classifier_identity", lambda: "host-a")
    monkeypatch.setattr(
        kb,
        "_systemd_user_scope_state",
        lambda *_args, **_kwargs: pytest.fail("modern direct must not query systemd"),
    )
    spawned = []

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="modern direct", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        assert kb._set_worker_pid(conn, task_id, 12345)
        assert kb.block_task(
            conn,
            task_id,
            reason="retry direct",
            expected_run_id=run_id,
        )
        ended_at = int(time.time()) - 30
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (task_id,),
        )
        conn.execute(
            "UPDATE task_runs SET status='blocked', outcome='blocked', ended_at=?, "
            "reap_state='finalized', reap_completed_at=? WHERE id=?",
            (ended_at, ended_at, run_id),
        )
        conn.commit()

        with pytest.raises(ValueError, match="PID-only"):
            kb.classify_legacy_worker_run(
                conn,
                task_id,
                run_id,
                launch_mode="direct",
                expected_pid=12345,
            )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id) or 4242,
            max_spawn=1,
            profile_roster=lambda _name: True,
        )

        assert spawned == [task_id]
        assert result.scope_cleanup == []
        final = next(
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == kb._WORKER_SCOPE_CLEANUP_FINAL_EVENT
            and event.run_id == run_id
        )
        assert final.payload["launch_mode"] == "direct"
        assert final.payload["disposition"] == "not_applicable"


def test_legacy_classifier_is_reachable_through_kanban_command(
    kanban_home,
    monkeypatch,
):
    from hermes_cli import kanban as kanban_cli

    db_path = kanban_home / "kanban.db"
    monkeypatch.setattr(kb, "_local_legacy_classifier_identity", lambda: "host-a")
    with kb.connect() as conn:
        task_id, run_id, _ = _scoped_run(
            conn,
            db_path,
            title="operator classification command",
            legacy=True,
        )

    first = kanban_cli.run_slash(
        f"classify-legacy-run {task_id} {run_id} "
        "--launch-mode direct --pid 12345"
    )
    second = kanban_cli.run_slash(
        f"classify-legacy-run {task_id} {run_id} "
        "--launch-mode direct --pid 12345"
    )

    assert "Classified legacy run" in first
    assert "Classified legacy run" in second
    with kb.connect() as conn:
        events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == kb._LEGACY_WORKER_LAUNCH_CLASSIFIED_EVENT
            and event.run_id == run_id
        ]
    assert len(events) == 1


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
