"""Focused proof for the adaptive global Kanban admission contract."""

from __future__ import annotations

import pytest
import yaml
import multiprocessing
import os
import sqlite3

from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BASE_DIR", raising=False)
    kb.init_db()
    return home


def _parallel_config(**kanban):
    return {
        "kanban": {
            "max_spawn": 3,
            "safe_dispatch_admission": {
                "allowed_worker_profiles": ["alice", "bob"],
            },
            **kanban,
        }
    }


def _install_canonical(home, config):
    (home / "config.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )


def _enable_adaptive(home, monkeypatch, **kanban):
    config = _parallel_config(**{"max_in_progress": 2, **kanban})
    _install_canonical(home, config)
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists",
        lambda name: name in {"alice", "bob"},
    )
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda sample=None: "ok")
    return config


def _scope_counts(total):
    return {"active": total, "activating": 0, "deactivating": 0, "total": total}


def _tree_snapshot(root):
    return {
        path.relative_to(root): (
            path.read_bytes(),
            (
                path.stat().st_mode, path.stat().st_ino,
                path.stat().st_uid, path.stat().st_gid,
                path.stat().st_size, path.stat().st_mtime_ns,
                path.stat().st_ctime_ns,
            ),
        )
        for path in root.rglob("*") if path.is_file()
    }


def _cross_process_dispatch(home, board, gate, output):
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_KANBAN_HOME"] = str(home)
    import hermes_cli.profiles as profiles

    profiles.profile_exists = lambda name: name in {"alice", "bob"}
    kb._memory_pressure_level = lambda sample=None: "ok"

    def live_scope_count():
        total = 0
        seen = set()
        for metadata in kb.list_boards(include_archived=False):
            path = kb.kanban_db_path(board=metadata["slug"]).resolve()
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            connection = sqlite3.connect(
                path.as_uri() + "?mode=ro", uri=True, timeout=2
            )
            try:
                total += connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
                ).fetchone()[0]
            finally:
                connection.close()
        return _scope_counts(total)

    kb._read_live_worker_scopes = live_scope_count
    gate.wait(timeout=10)
    with kb.connect(board=board) as connection:
        result = kb.dispatch_once(
            connection,
            board=board,
            spawn_fn=lambda *_a, **_k: os.getpid(),
        )
    output.put((len(result.spawned), result.skipped_locked))


def test_caps_are_strict_and_cli_cannot_widen():
    cfg = _parallel_config(max_in_progress=2)
    assert kb.resolve_dispatch_caps(cfg, max_spawn=9) == (3, 2)
    with pytest.raises(ValueError, match="positive integer"):
        kb.resolve_dispatch_caps({"kanban": {"max_spawn": "3"}})


def test_parallel_admission_requires_canonical_allowlist():
    with pytest.raises(ValueError, match="allowed_worker_profiles"):
        kb.resolve_worker_profile_admission(
            {"kanban": {"max_in_progress": 2}},
            max_in_progress=2,
        )


@pytest.mark.parametrize("allowlist", [None, [], "alice", [" alice"], ["alice", "alice"]])
def test_adaptive_allowlist_is_strict_and_nonempty(
    isolated_home, monkeypatch, allowlist,
):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    config = {
        "kanban": {
            "max_in_progress": 2,
            "safe_dispatch_admission": {
                "allowed_worker_profiles": allowlist,
            },
        }
    }
    _install_canonical(isolated_home, config)
    with pytest.raises(ValueError, match="allowed_worker_profiles"):
        kb.dispatch_once(None, effective_config=config)


def test_canonical_loader_failure_precedes_lock_and_database(
    isolated_home, monkeypatch,
):
    import hermes_cli.config as config_module

    (isolated_home / "config.yaml").write_text("kanban: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        config_module, "load_config_readonly",
        lambda: (_ for _ in ()).throw(RuntimeError("loader failed")),
    )
    monkeypatch.setattr(
        kb, "_allocation_lock",
        lambda *_a, **_k: pytest.fail("allocation lock opened"),
    )
    with pytest.raises(ValueError, match="could not load canonical"):
        kb.dispatch_once(None, max_in_progress=2)


def test_canonical_snapshot_does_not_alias_loader_cache(isolated_home, monkeypatch):
    import hermes_cli.config as config_module

    cfg = _parallel_config(max_in_progress=2)
    _install_canonical(isolated_home, cfg)
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda name: name in {"alice", "bob"}
    )
    monkeypatch.setattr(config_module, "load_config_readonly", lambda: cfg)
    resolved = kb._canonical_dispatch_config(cfg)
    assert resolved is not cfg
    with pytest.raises(TypeError):
        resolved["kanban"]["max_spawn"] = 1
    assert cfg["kanban"]["max_spawn"] == 3


def test_malformed_canonical_config_fails_closed_before_dispatch(isolated_home):
    (isolated_home / "config.yaml").write_text("kanban: [", encoding="utf-8")
    with pytest.raises(ValueError, match="canonical Hermes config"):
        kb.dispatch_once(
            None,  # config validation must precede any DB/connection use
            max_spawn=2,
            effective_config=_parallel_config(max_spawn=2),
        )


def test_parallel_dispatch_blocks_on_scope_count_transition(
    isolated_home, monkeypatch,
):
    _install_canonical(isolated_home, _parallel_config(max_in_progress=2))
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda name: name in {"alice", "bob"}
    )
    monkeypatch.setattr(
        kb, "_read_live_worker_scopes", lambda: {"active": 1, "activating": 0,
                                                   "deactivating": 0, "total": 1}
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="work", assignee="alice")
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *args, **kwargs: 123,
            effective_config=_parallel_config(max_in_progress=2),
        )
        assert result.admission_blocked is True
        assert result.admission_reason == "scope_count_transition"
        assert result.admission_metrics["db_running"] == 0
        assert kb.get_task(conn, task_id).status == "ready"


def test_parallel_dispatch_uses_global_occupancy_for_custom_spawn(
    isolated_home, monkeypatch,
):
    _install_canonical(isolated_home, _parallel_config(max_in_progress=2))
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda name: name in {"alice", "bob"}
    )
    monkeypatch.setattr(
        kb, "_read_live_worker_scopes", lambda: {"active": 0, "activating": 0,
                                                   "deactivating": 0, "total": 0}
    )
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda sample=None: "ok")
    spawned = []
    with kb.connect() as conn:
        for name in ("one", "two", "three"):
            kb.create_task(conn, title=name, assignee="alice")
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace, board=None: spawned.append(task.id) or 123,
            max_spawn=2,
            effective_config=_parallel_config(max_spawn=2),
        )
    assert len(spawned) == 1
    assert len(result.spawned) == 1


def test_missing_canonical_config_allows_serial_but_rejects_parallel(
    isolated_home, monkeypatch,
):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    with kb.connect() as conn:
        kb.create_task(conn, title="serial", assignee="alice")
        result = kb.dispatch_once(
            conn, spawn_fn=lambda *_a, **_k: os.getpid(), max_spawn=1
        )
        assert len(result.spawned) == 1
    with pytest.raises(ValueError, match="canonical Hermes config"):
        kb.dispatch_once(None, max_in_progress=2)


def test_effective_config_cannot_create_adaptive_policy(
    isolated_home, monkeypatch,
):
    _install_canonical(isolated_home, {"kanban": {}})
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    monkeypatch.setattr(
        kb,
        "_allocation_lock",
        lambda *_args, **_kwargs: pytest.fail("allocation lock opened"),
    )
    caller_config = {
        "kanban": {
            "max_in_progress": 2,
            "safe_dispatch_admission": {
                "allowed_worker_profiles": ["alice", "bob"],
            },
        }
    }
    with pytest.raises(ValueError, match="without canonical"):
        kb.dispatch_once(None, effective_config=caller_config)
    with pytest.raises(ValueError, match="without canonical"):
        kb.dispatch_once(None, max_in_progress=2)


def test_explicit_allowlist_cannot_create_adaptive_policy(
    isolated_home, monkeypatch,
):
    _install_canonical(isolated_home, {"kanban": {}})
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    with pytest.raises(ValueError, match="without canonical"):
        kb.dispatch_once(
            None,
            max_in_progress=1,
            allowed_worker_profiles=["alice"],
        )


def test_canonical_parallel_cap_cannot_be_rescued_by_caller_allowlist(
    isolated_home,
):
    _install_canonical(isolated_home, {"kanban": {"max_in_progress": 2}})
    caller_config = {
        "kanban": {
            "safe_dispatch_admission": {
                "allowed_worker_profiles": ["alice", "bob"],
            },
        }
    }
    with pytest.raises(ValueError, match="allowed_worker_profiles"):
        kb.dispatch_once(None, effective_config=caller_config)


def test_canonical_policy_is_inherited_when_effective_config_is_omitted(
    isolated_home, monkeypatch,
):
    canonical = _enable_adaptive(isolated_home, monkeypatch)
    snapshot = kb._canonical_dispatch_config(None)
    assert snapshot["kanban"]["max_in_progress"] == 2
    assert tuple(
        snapshot["kanban"]["safe_dispatch_admission"]["allowed_worker_profiles"]
    ) == ("alice", "bob")
    assert kb.resolve_worker_profile_admission(snapshot) == ["alice", "bob"]
    assert canonical["kanban"]["max_in_progress"] == 2


def test_canonical_serial_policy_does_not_require_adaptive_allowlist(
    isolated_home,
):
    _install_canonical(isolated_home, {"kanban": {}})
    snapshot = kb._canonical_dispatch_config(
        {"kanban": {"max_in_progress": 1}}
    )
    assert snapshot["kanban"]["max_in_progress"] == 1
    assert kb._parallel_dispatch_required(snapshot) is False
    assert kb.resolve_worker_profile_admission(snapshot) is None


def test_max_spawn_alone_is_not_host_adaptive_policy(
    isolated_home, monkeypatch,
):
    config = {"kanban": {"max_spawn": 3}}
    _install_canonical(isolated_home, config)
    snapshot = kb._canonical_dispatch_config(config)
    assert kb._parallel_dispatch_required(snapshot) is False


def test_canonical_policy_rejects_widening_and_allows_narrowing(
    isolated_home, monkeypatch,
):
    canonical = _enable_adaptive(isolated_home, monkeypatch)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    with pytest.raises(ValueError, match="cannot widen"):
        kb._canonical_dispatch_config(
            {
                "kanban": {
                    "safe_dispatch_admission": {
                        "allowed_worker_profiles": ["alice", "bob", "mallory"]
                    }
                }
            }
        )
    narrowed = kb._canonical_dispatch_config(
        {
            "kanban": {
                "max_in_progress": 1,
                "safe_dispatch_admission": {
                    "allowed_worker_profiles": ["alice"]
                },
            }
        }
    )
    assert narrowed["kanban"]["max_in_progress"] == 1
    assert tuple(narrowed["kanban"]["safe_dispatch_admission"][
        "allowed_worker_profiles"
    ]) == ("alice",)
    assert canonical["kanban"]["max_in_progress"] == 2
    widened_cap = kb._canonical_dispatch_config(
        {
            "kanban": {
                "max_in_progress": 3,
                "safe_dispatch_admission": {
                    "allowed_worker_profiles": ["alice"],
                },
            }
        }
    )
    assert widened_cap["kanban"]["max_in_progress"] == 2
    assert tuple(
        widened_cap["kanban"]["safe_dispatch_admission"][
            "allowed_worker_profiles"
        ]
    ) == ("alice",)


def test_prepare_dispatch_admission_freezes_and_narrows_before_db_work(
    isolated_home, monkeypatch,
):
    _enable_adaptive(isolated_home, monkeypatch)
    monkeypatch.setattr(
        kb,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("admission opened a DB"),
    )
    narrowed = kb.prepare_dispatch_admission(
        {
            "kanban": {
                "max_in_progress": 1,
                "safe_dispatch_admission": {
                    "allowed_worker_profiles": ["alice"],
                },
            }
        },
        max_in_progress=1,
    )
    assert isinstance(narrowed, kb._CanonicalAdmissionSnapshot)
    assert narrowed["kanban"]["max_in_progress"] == 1
    assert tuple(
        narrowed["kanban"]["safe_dispatch_admission"][
            "allowed_worker_profiles"
        ]
    ) == ("alice",)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    with pytest.raises(ValueError, match="cannot widen"):
        kb.prepare_dispatch_admission(
            {
                "kanban": {
                    "max_in_progress": 3,
                    "safe_dispatch_admission": {
                        "allowed_worker_profiles": ["alice", "bob", "mallory"]
                    },
                }
            }
        )


def test_allowlist_only_policy_resolves_derived_host_cap(
    isolated_home, monkeypatch,
):
    config = _parallel_config()
    _install_canonical(isolated_home, config)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    monkeypatch.setattr(kb, "resolve_max_in_progress", lambda _value: 2)
    snapshot = kb._canonical_dispatch_config(config)
    assert snapshot["kanban"]["max_in_progress"] == 2
    assert kb._parallel_dispatch_required(snapshot)


def test_resolved_snapshot_survives_later_config_replacement(
    isolated_home, monkeypatch,
):
    config = _enable_adaptive(isolated_home, monkeypatch)
    snapshot = kb._canonical_dispatch_config(config)
    _install_canonical(
        isolated_home,
        _parallel_config(
            max_in_progress=2,
            safe_dispatch_admission={"allowed_worker_profiles": ["bob"]},
        ),
    )
    assert kb._canonical_dispatch_config(snapshot) is snapshot
    assert kb.resolve_worker_profile_admission(snapshot) == ["alice", "bob"]


def test_config_replacement_during_load_fails_closed(isolated_home, monkeypatch):
    import hermes_cli.config as config_module

    config = _parallel_config(max_in_progress=2)
    _install_canonical(isolated_home, config)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)

    def replacing_loader():
        replacement = isolated_home / "config.yaml.tmp"
        replacement.write_text(
            yaml.safe_dump(_parallel_config(max_in_progress=3)),
            encoding="utf-8",
        )
        os.replace(replacement, isolated_home / "config.yaml")
        return config

    monkeypatch.setattr(config_module, "load_config_readonly", replacing_loader)
    with pytest.raises(ValueError, match="changed while loading"):
        kb._canonical_dispatch_config(config)


def test_adaptive_first_native_worker_requires_scope_before_claim(
    isolated_home, monkeypatch,
):
    config = _enable_adaptive(isolated_home, monkeypatch)
    monkeypatch.setattr(kb, "_read_live_worker_scopes", lambda: _scope_counts(0))
    preflight = []
    monkeypatch.setattr(
        kb,
        "_systemd_scope_preflight",
        lambda **kwargs: preflight.append(kwargs) or (True, None, "systemd-run"),
    )
    received = []

    def fake_spawn(
        _task, _workspace, *, require_scope=False, scope_config=None,
        launch_intent_fn=None, clear_launch_intent_fn=None,
    ):
        received.append({"require_scope": require_scope})
        return os.getpid()

    monkeypatch.setattr(
        kb,
        "_default_spawn",
        fake_spawn,
    )
    with kb.connect() as conn:
        kb.create_task(conn, title="first", assignee="alice")
        result = kb.dispatch_once(conn, effective_config=config)
    assert len(result.spawned) == 1
    assert preflight and all(call["require_scope"] is True for call in preflight)
    assert received[0]["require_scope"] is True


def test_excluded_review_does_not_reserve_adaptive_ready_slot(
    isolated_home, monkeypatch,
):
    config = _enable_adaptive(isolated_home, monkeypatch)
    monkeypatch.setattr(kb, "_read_live_worker_scopes", lambda: _scope_counts(0))
    spawned = []
    with kb.connect() as conn:
        ready = kb.create_task(conn, title="ready", assignee="alice")
        review = kb.create_task(conn, title="review", assignee="mallory")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review,))
        conn.commit()
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, *_a, **_k: spawned.append(task.id) or os.getpid(),
            effective_config=config,
        )
    assert spawned == [ready]
    assert result.spawned[0][0] == ready


def test_caller_board_list_cannot_hide_other_board_or_profile_occupancy(
    isolated_home, monkeypatch,
):
    config = _enable_adaptive(
        isolated_home, monkeypatch, max_in_progress_per_profile=1
    )
    kb.create_board("second")
    with kb.connect(board="second") as other:
        task_id = kb.create_task(other, title="busy", assignee="alice")
        assert kb.claim_task(other, task_id) is not None
        assert other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    monkeypatch.setattr(kb, "_read_live_worker_scopes", lambda: _scope_counts(1))
    spawned = []
    with kb.connect() as conn:
        kb.create_task(conn, title="same profile", assignee="alice")
        kb.create_task(conn, title="other profile", assignee="bob")
        result = kb.dispatch_once(
            conn,
            board="default",
            selected_boards=["default"],
            spawn_fn=lambda task, *_a, **_k: spawned.append(task.assignee) or os.getpid(),
            effective_config=config,
        )
    assert spawned == ["bob"]
    assert result.skipped_per_profile_capped[0][1:] == ("alice", 1)


def test_dry_run_is_select_only_before_every_mutator(
    isolated_home, monkeypatch,
):
    config = _enable_adaptive(isolated_home, monkeypatch)
    with kb.connect() as conn:
        kb.create_task(conn, title="preview", assignee="alice")
    db_path = kb.kanban_db_path()
    before = {
        path.relative_to(isolated_home): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in isolated_home.rglob("*") if path.is_file()
    }
    for name in (
        "_allocation_lock", "_dispatch_tick_lock", "reap_worker_zombies",
        "reconcile_worker_scope_terminals", "reconcile_orphaned_running",
        "release_stale_claims", "_read_live_worker_scopes",
        "_systemd_scope_preflight", "_fire_dispatch_tick_hook",
    ):
        monkeypatch.setattr(
            kb, name,
            lambda *a, _name=name, **k: pytest.fail(f"mutator called: {_name}"),
        )
    with kb.connect_readonly_closing() as preview:
        result = kb.dispatch_once(
            preview, dry_run=True, effective_config=config
        )
    after = {
        path.relative_to(isolated_home): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in isolated_home.rglob("*") if path.is_file()
    }
    assert len(result.spawned) == 1
    assert before == after


def test_dry_run_promotes_stale_dependency_only_in_private_snapshot(
    isolated_home, monkeypatch,
):
    config = _enable_adaptive(isolated_home, monkeypatch)
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="alice")
        child_id = kb.create_task(
            conn, title="child", assignee="alice", parents=[parent_id]
        )
        assert kb.complete_task(conn, parent_id, result="parent done")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ?", (child_id,)
            )
        assert kb.get_task(conn, child_id).status == "todo"

        db_path = kb.kanban_db_path()
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        conn.execute("PRAGMA wal_autocheckpoint=0")
        # Ensure the live WAL connection has materialized its shared-memory
        # sidecar before the byte/metadata snapshot.
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
        assert db_path.is_file()

        before = {
            path.relative_to(isolated_home): (
                path.read_bytes(),
                (
                    path.stat().st_mode, path.stat().st_ino,
                    path.stat().st_uid, path.stat().st_gid,
                    path.stat().st_size, path.stat().st_mtime_ns,
                    path.stat().st_ctime_ns,
                ),
            )
            for path in isolated_home.rglob("*") if path.is_file()
        }
        source_type = type(conn)
        monkeypatch.setattr(
            source_type,
            "backup",
            lambda *_args, **_kwargs: pytest.fail(
                "dry-run must not backup directly from a disk-backed source"
            ),
        )
        result = kb.dispatch_once(
            conn, dry_run=True, effective_config=config,
        )
        after = {
            path.relative_to(isolated_home): (
                path.read_bytes(),
                (
                    path.stat().st_mode, path.stat().st_ino,
                    path.stat().st_uid, path.stat().st_gid,
                    path.stat().st_size, path.stat().st_mtime_ns,
                    path.stat().st_ctime_ns,
                ),
            )
            for path in isolated_home.rglob("*") if path.is_file()
        }

        assert result.promoted == 1
        assert [task_id for task_id, _assignee, _workspace in result.spawned] == [
            child_id
        ]
        assert kb.get_task(conn, child_id).status == "todo"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (child_id,)
        ).fetchone()[0] == 0
        assert before == after


def test_adaptive_unknown_memory_is_restrictive(
    isolated_home, monkeypatch,
):
    config = _enable_adaptive(isolated_home, monkeypatch)
    monkeypatch.setattr(kb, "_read_live_worker_scopes", lambda: _scope_counts(0))
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda sample=None: "unknown")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="wait", assignee="alice")
        result = kb.dispatch_once(
            conn, spawn_fn=lambda *_a, **_k: pytest.fail("spawned"),
            effective_config=config,
        )
        assert result.memory_pressure == "unknown"
        assert kb.get_task(conn, task_id).status == "ready"


def test_one_scope_transition_reprobe_can_recover(
    isolated_home, monkeypatch,
):
    config = _enable_adaptive(isolated_home, monkeypatch)
    samples = iter((_scope_counts(1), _scope_counts(0)))
    monkeypatch.setattr(kb, "_read_live_worker_scopes", lambda: next(samples))
    maintenance_calls = []
    monkeypatch.setattr(
        kb,
        "release_stale_claims",
        lambda _conn: maintenance_calls.append("release_stale_claims") or 0,
    )
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: None)
    monkeypatch.setattr(kb, "reconcile_worker_scope_terminals", lambda _conn: [])
    spawned = []
    with kb.connect() as conn:
        kb.create_task(conn, title="recover", assignee="alice")
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, *_a, **_k: spawned.append(task.id) or os.getpid(),
            effective_config=config,
        )
    assert len(spawned) == 1
    assert result.admission_blocked is False
    assert maintenance_calls == ["release_stale_claims"]


def test_adaptive_empty_queue_maintains_before_host_observation(
    isolated_home, monkeypatch,
):
    config = _enable_adaptive(isolated_home, monkeypatch)
    maintenance_calls = []
    monkeypatch.setattr(
        kb,
        "release_stale_claims",
        lambda _conn: maintenance_calls.append("release_stale_claims") or 0,
    )
    monkeypatch.setattr(
        kb,
        "observe_running_tasks_other_boards",
        lambda *_args, **_kwargs: pytest.fail("foreign observer called"),
    )
    monkeypatch.setattr(
        kb,
        "_read_live_worker_scopes",
        lambda: pytest.fail("scope observer called"),
    )
    with kb.connect() as conn:
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: pytest.fail("spawned"),
            effective_config=config,
        )
    assert maintenance_calls == ["release_stale_claims"]
    assert result.spawned == []
    assert result.admission_blocked is False


def test_adaptive_unknown_host_observation_preserves_candidate_maintenance(
    isolated_home, monkeypatch,
):
    config = _enable_adaptive(isolated_home, monkeypatch)
    maintenance_calls = []
    monkeypatch.setattr(
        kb,
        "release_stale_claims",
        lambda _conn: maintenance_calls.append("release_stale_claims") or 0,
    )
    monkeypatch.setattr(
        kb,
        "observe_running_tasks_other_boards",
        lambda *_args, **_kwargs: kb._OtherBoardsRunningObservation(
            running_count=0, has_independent_db=False,
        ),
    )
    monkeypatch.setattr(
        kb,
        "_read_live_worker_scopes",
        lambda: (_ for _ in ()).throw(RuntimeError("scope telemetry unavailable")),
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="defer", assignee="alice")
        before = kb.get_task(conn, task_id)
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: pytest.fail("spawned"),
            effective_config=config,
        )
        after = kb.get_task(conn, task_id)
        assert after is not None and before is not None
        assert after.status == before.status == "ready"
        assert after.claim_lock == before.claim_lock
        assert after.current_run_id == before.current_run_id
        assert after.workspace_path == before.workspace_path
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
    assert maintenance_calls == ["release_stale_claims"]
    assert result.admission_blocked is True
    assert result.admission_reason == "scope_telemetry_unavailable"
    assert result.spawned == []


def test_adaptive_unknown_observation_preserves_dependency_promotion(
    isolated_home, monkeypatch,
):
    config = _enable_adaptive(isolated_home, monkeypatch)
    maintenance_calls = []
    monkeypatch.setattr(
        kb,
        "release_stale_claims",
        lambda _conn: maintenance_calls.append("release_stale_claims") or 0,
    )
    monkeypatch.setattr(
        kb,
        "observe_running_tasks_other_boards",
        lambda *_args, **_kwargs: kb._OtherBoardsRunningObservation(
            running_count=0, has_independent_db=False,
        ),
    )
    monkeypatch.setattr(
        kb,
        "_read_live_worker_scopes",
        lambda: (_ for _ in ()).throw(RuntimeError("scope telemetry unavailable")),
    )
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="alice")
        child_id = kb.create_task(
            conn, title="child", assignee="alice", parents=[parent_id]
        )
        assert kb.complete_task(conn, parent_id, result="parent done")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ?", (child_id,)
            )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: pytest.fail("spawned"),
            effective_config=config,
        )
        child = kb.get_task(conn, child_id)
        assert child is not None and child.status == "ready"
        assert child.claim_lock is None
        assert child.current_run_id is None
        assert child.workspace_path is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (child_id,)
        ).fetchone()[0] == 0
    assert maintenance_calls == ["release_stale_claims"]
    assert result.promoted == 1
    assert result.admission_blocked is True
    assert result.admission_reason == "scope_telemetry_unavailable"
    assert result.spawned == []


def test_other_board_occupancy_requires_scope_before_native_claim(
    isolated_home, monkeypatch,
):
    config = _enable_adaptive(isolated_home, monkeypatch)
    kb.create_board("second")
    with kb.connect(board="second") as other:
        busy = kb.create_task(other, title="busy", assignee="bob")
        assert kb.claim_task(other, busy) is not None
        assert other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    monkeypatch.setattr(kb, "_read_live_worker_scopes", lambda: _scope_counts(1))
    monkeypatch.setattr(
        kb, "_systemd_scope_preflight",
        lambda **_kwargs: (False, "unsupported", None),
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="native", assignee="alice")
        result = kb.dispatch_once(conn, effective_config=config)
        assert result.spawned == []
        assert kb.get_task(conn, task_id).status == "ready"


def test_clean_foreign_observer_returns_exact_profile_occupancy_without_sidecars(
    isolated_home,
):
    kb.create_board("second")
    second_root = kb.board_dir("second")
    with kb.connect(board="second") as other:
        task_id = kb.create_task(other, title="foreign-running", assignee="alice")
        assert kb.claim_task(other, task_id) is not None
        assert other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    before = _tree_snapshot(second_root)

    observation = kb.observe_running_tasks_other_boards("default")

    assert observation is not None
    assert observation.running_count == 1
    assert dict(observation.per_profile_running) == {"alice": 1}
    assert observation.has_independent_db is True
    assert _tree_snapshot(second_root) == before


@pytest.mark.skipif(os.name == "nt", reason="fork-based lock proof is POSIX-only")
def test_cross_process_board_entrypoints_cannot_overallocate(
    isolated_home, monkeypatch,
):
    config = _enable_adaptive(isolated_home, monkeypatch, max_in_progress=1)
    _install_canonical(isolated_home, config)
    kb.create_board("second")
    with kb.connect() as first:
        kb.create_task(first, title="first", assignee="alice")
        assert first.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    with kb.connect(board="second") as second:
        kb.create_task(second, title="second", assignee="bob")
        assert second.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

    context = multiprocessing.get_context("fork")
    gate = context.Barrier(2)
    output = context.Queue()
    processes = [
        context.Process(
            target=_cross_process_dispatch,
            args=(isolated_home, board, gate, output),
        )
        for board in ("default", "second")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    receipts = [output.get(timeout=2) for _ in processes]
    assert sum(spawned for spawned, _locked in receipts) == 1
    with kb.connect() as first, kb.connect(board="second") as second:
        assert kb.count_running_tasks(first) + kb.count_running_tasks(second) == 1
