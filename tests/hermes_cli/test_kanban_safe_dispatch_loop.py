"""Focused tests for the external multi-board safe dispatcher."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


SCRIPT = Path(__file__).parents[2] / "scripts" / "kanban_safe_dispatch_loop.py"
SPEC = importlib.util.spec_from_file_location("kanban_safe_dispatch_loop", SCRIPT)
assert SPEC and SPEC.loader
safe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(safe)


def _load_safe_dispatch_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot_tree(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    paths = [root, *root.rglob("*")]
    for path in sorted(
        paths,
        key=lambda item: item.relative_to(root).as_posix() if item != root else "",
    ):
        relative = "." if path == root else path.relative_to(root).as_posix()
        info = path.lstat()
        common = (info.st_mtime_ns, info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            snapshot[relative] = ("directory", *common)
        elif stat.S_ISREG(info.st_mode):
            data = path.read_bytes()
            snapshot[relative] = (
                "file",
                *common,
                data,
                hashlib.sha256(data).hexdigest(),
            )
        elif stat.S_ISLNK(info.st_mode):
            snapshot[relative] = ("symlink", *common, path.readlink().as_posix())
        else:
            snapshot[relative] = ("other", *common)
    return snapshot


def test_repo_defaults_to_runtime_agent_root_not_script_location(monkeypatch, tmp_path):
    runtime_root = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(runtime_root))
    monkeypatch.delenv("HERMES_REPO", raising=False)

    loaded = _load_safe_dispatch_module("kanban_safe_dispatch_loop_repo_default")

    assert loaded.REPO == runtime_root / "hermes-agent"
    assert loaded.REPO != SCRIPT.parent.parent


def test_write_cursor_replaces_sibling_temp_without_truncating_existing_file(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_SAFE_DISPATCH_STATE_DIR", str(tmp_path))
    cursor = tmp_path / "kanban-safe-dispatcher.cursor"
    cursor.write_text("7\n", encoding="utf-8")
    real_replace = safe.os.replace
    observed = {}

    def replace(source, destination):
        source_path = Path(source)
        observed["source"] = source_path
        observed["destination"] = Path(destination)
        assert source_path.parent == cursor.parent
        assert cursor.read_text(encoding="utf-8") == "7\n"
        real_replace(source, destination)

    monkeypatch.setattr(safe.os, "replace", replace)
    safe._write_cursor(8)

    assert cursor.read_text(encoding="utf-8") == "8\n"
    assert observed["destination"] == cursor
    assert observed["source"] != cursor
    assert not list(tmp_path.glob(f".{cursor.name}.*.tmp"))


def test_unreadable_config_keeps_dispatch_refusal_default(monkeypatch):
    from hermes_cli import config

    def unreadable_config():
        raise OSError("config unavailable")

    monkeypatch.setattr(config, "read_raw_config", unreadable_config)

    assert safe._load_config() == {}
    assert safe._root_dispatch_enabled({}) is True


def test_allowlist_defaults_dedupes_and_validates(monkeypatch):
    monkeypatch.delenv("HERMES_SAFE_DISPATCH_BOARDS", raising=False)
    assert safe._parse_board_allowlist() == ["default"]
    assert safe._parse_board_allowlist(" Default, alpha,ALPHA, default ") == [
        "default",
        "alpha",
    ]
    with pytest.raises(ValueError, match="must not be empty"):
        safe._parse_board_allowlist(" ")
    with pytest.raises(ValueError, match="empty board slug"):
        safe._parse_board_allowlist("alpha,,beta")
    with pytest.raises(ValueError, match="invalid safe-dispatch board"):
        safe._parse_board_allowlist("../secrets")


def test_dispatch_cli_pins_global_board_and_worker_env(monkeypatch):
    captured = {}
    monkeypatch.setattr(safe, "PYTHON", Path("/venv/bin/python"))
    monkeypatch.setattr(safe, "REPO", Path("/repo"))
    monkeypatch.setattr(safe, "ROOT", Path("/hermes"))

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout='{"spawned": []}', stderr="")

    monkeypatch.setattr(safe.subprocess, "run", fake_run)
    result = safe._dispatch_board("alpha", failure_limit=3, dry_run=True)

    assert result["ok"] is True
    assert captured["cmd"] == [
        "/venv/bin/python",
        "-m",
        "hermes_cli.main",
        "kanban",
        "--board",
        "alpha",
        "dispatch",
        "--max",
        "1",
        "--spawn-budget",
        "1",
        "--failure-limit",
        "3",
        "--json",
        "--dry-run",
    ]
    assert captured["kwargs"]["env"]["HERMES_KANBAN_BOARD"] == "alpha"
    assert captured["kwargs"]["env"]["HERMES_HOME"] == "/hermes"
    assert captured["kwargs"].get("shell", False) is False


def test_global_cap_and_persisted_rotation_prevent_first_board_starvation(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SAFE_DISPATCH_MAX", "2")
    monkeypatch.setenv("HERMES_SAFE_DISPATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        safe,
        "_load_config",
        lambda: {"kanban": {"dispatch_in_gateway": False, "max_spawn": 20}},
    )
    monkeypatch.setattr(safe, "_parse_board_allowlist", lambda: ["a", "b", "c"])
    monkeypatch.setattr(safe, "_probe_db", lambda board: (True, "ok"))
    monkeypatch.setattr(safe, "_count_running", lambda board: 0)
    calls = []

    def fake_dispatch(board, *, failure_limit, dry_run, **kwargs):
        calls.append((board, kwargs["spawn_budget"]))
        spawned = [{"task_id": board}] if kwargs["spawn_budget"] else []
        return {"ok": True, "board": board, "payload": {"spawned": spawned}}

    monkeypatch.setattr(safe, "_dispatch_board", fake_dispatch)
    first = safe._dispatch_once()
    second = safe._dispatch_once()

    assert first["ok"] is True
    assert second["ok"] is True
    assert [board for board, budget in calls if budget == 1] == [
        "a", "b", "b", "c"
    ]
    assert [board for board, budget in calls if budget == 0] == [
        "a", "b", "c", "a", "b", "c"
    ]
    assert json.loads((tmp_path / "kanban-safe-dispatcher.cursor").read_text()) == 2


def test_dry_run_does_not_advance_cursor(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SAFE_DISPATCH_STATE_DIR", str(tmp_path))
    (tmp_path / "kanban-safe-dispatcher.cursor").write_text("1\n")
    monkeypatch.setattr(
        safe,
        "_load_config",
        lambda: {"kanban": {"dispatch_in_gateway": False}},
    )
    monkeypatch.setattr(safe, "_parse_board_allowlist", lambda: ["a", "b"])
    monkeypatch.setattr(safe, "_probe_db", lambda board: (True, "ok"))
    monkeypatch.setattr(safe, "_count_running", lambda board: 0)
    dry_runs = []

    def fake_dispatch(board, *, failure_limit, dry_run, **kwargs):
        dry_runs.append((dry_run, kwargs["spawn_budget"]))
        spawned = [{"task_id": board}] if kwargs["spawn_budget"] else []
        return {"ok": True, "board": board, "payload": {"spawned": spawned}}

    monkeypatch.setattr(safe, "_dispatch_board", fake_dispatch)
    result = safe._dispatch_once(dry_run=True)

    assert result["ok"] is True
    assert dry_runs == [(True, 0), (True, 0), (True, 1)]
    assert (tmp_path / "kanban-safe-dispatcher.cursor").read_text() == "1\n"


def test_probe_uses_each_board_db_and_dispatch_fails_closed_on_bad_board(monkeypatch, tmp_path):
    good_db = tmp_path / "good.db"
    kb.init_db(db_path=good_db)
    paths = {"good": good_db, "bad": tmp_path / "missing.db"}
    monkeypatch.setattr(safe, "_board_db_path", paths.__getitem__)

    assert safe._probe_db("good") == (True, "ok")
    healthy, reason = safe._probe_db("bad")
    assert healthy is False
    assert "bad" in reason and "missing db" in reason

    monkeypatch.setattr(
        safe,
        "_load_config",
        lambda: {"kanban": {"dispatch_in_gateway": False}},
    )
    monkeypatch.setattr(safe, "_parse_board_allowlist", lambda: ["good", "bad"])
    dispatched = []
    monkeypatch.setattr(
        safe,
        "_dispatch_board",
        lambda board, **kwargs: dispatched.append(board),
    )
    monkeypatch.setattr(safe, "_count_running", lambda board: 0)
    result = safe._dispatch_once()
    assert result["ok"] is False
    assert dispatched == []
    assert result["boards"][0]["board"] == "bad"


def test_probe_and_count_use_strict_readonly_without_sidecars(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    with kb.connect_closing(db_path=db_path) as conn:
        task_id = kb.create_task(conn, title="running", assignee="alice")
        assert kb.claim_task(conn, task_id) is not None
    paths = {"default": db_path}
    monkeypatch.setattr(safe, "_board_db_path", paths.__getitem__)
    before = _snapshot_tree(tmp_path)

    assert safe._probe_db("default") == (True, "ok")
    assert safe._count_running("default") == 1

    assert _snapshot_tree(tmp_path) == before


def test_probe_and_count_fail_closed_on_legacy_schema(monkeypatch, tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
    monkeypatch.setattr(safe, "_board_db_path", lambda board: db_path)

    healthy, reason = safe._probe_db("legacy")
    assert healthy is False
    assert "legacy" in reason or "uninitialized" in reason
    with pytest.raises(RuntimeError, match="legacy|uninitialized"):
        safe._count_running("legacy")


def _seed_running_board(path: Path, running: int, ready: int = 0) -> None:
    kb.init_db(db_path=path)
    with kb.connect_closing(db_path=path) as conn:
        for index in range(running):
            task_id = kb.create_task(
                conn, title=f"running-{index}", assignee="alice"
            )
            assert kb.claim_task(conn, task_id) is not None
        for index in range(ready):
            kb.create_task(conn, title=f"ready-{index}", assignee="alice")


def test_full_cap_runs_zero_spawn_maintenance_on_every_board(monkeypatch, tmp_path):
    alpha = tmp_path / "alpha.db"
    beta = tmp_path / "beta.db"
    _seed_running_board(alpha, running=1)
    _seed_running_board(beta, running=1)
    paths = {"alpha": alpha, "beta": beta}
    monkeypatch.setenv("HERMES_SAFE_DISPATCH_MAX", "2")
    monkeypatch.setattr(
        safe, "_load_config", lambda: {"kanban": {"dispatch_in_gateway": False}}
    )
    monkeypatch.setattr(safe, "_parse_board_allowlist", lambda: ["alpha", "beta"])
    monkeypatch.setattr(safe, "_board_db_path", paths.__getitem__)
    monkeypatch.setattr(safe, "_probe_db", lambda board: (True, "ok"))
    calls = []

    def fake_dispatch(board, **kwargs):
        calls.append((board, kwargs))
        return {
            "ok": True,
            "board": board,
            "payload": {"spawned": [], "reclaimed": 0},
        }

    monkeypatch.setattr(safe, "_dispatch_board", fake_dispatch)

    result = safe._dispatch_once()

    assert result["ok"] is True
    assert result["running"] == 2
    assert [board for board, _ in calls] == ["alpha", "beta"]
    assert all(kwargs["spawn_budget"] == 0 for _, kwargs in calls)
    assert all(summary["spawned"] == 0 for summary in result["boards"])


def test_stale_reclaim_frees_one_global_slot_for_same_tick_spawn(
    monkeypatch, tmp_path, all_assignees_spawnable
):
    db_path = tmp_path / "default.db"
    _seed_running_board(db_path, running=1, ready=1)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET claim_expires=? WHERE status='running'",
            (int(time.time()) - 1,),
        )
        conn.execute(
            "UPDATE task_runs SET claim_expires=? WHERE status='running'",
            (int(time.time()) - 1,),
        )

    monkeypatch.setenv("HERMES_SAFE_DISPATCH_MAX", "1")
    monkeypatch.setenv("HERMES_SAFE_DISPATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        safe, "_load_config", lambda: {"kanban": {"dispatch_in_gateway": False}}
    )
    monkeypatch.setattr(safe, "_parse_board_allowlist", lambda: ["default"])
    monkeypatch.setattr(safe, "_board_db_path", lambda board: db_path)
    monkeypatch.setattr(safe, "_probe_db", lambda board: (True, "ok"))
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(kb, "resolve_workspace", lambda task, board=None: workspace)
    calls = []

    def native_dispatch(board, *, failure_limit, dry_run, max_spawn, spawn_budget):
        calls.append(spawn_budget)
        with kb.connect_closing(db_path=db_path) as conn:
            result = kb.dispatch_once(
                conn,
                spawn_fn=lambda task, resolved_workspace: None,
                dry_run=dry_run,
                max_spawn=max_spawn,
                spawn_budget=spawn_budget,
                failure_limit=failure_limit,
                board=board,
            )
        return {
            "ok": True,
            "board": board,
            "payload": {
                "reclaimed": result.reclaimed,
                "crashed": result.crashed,
                "timed_out": result.timed_out,
                "stale": result.stale,
                "auto_blocked": result.auto_blocked,
                "promoted": result.promoted,
                "spawned": [
                    {"task_id": task_id, "assignee": assignee}
                    for task_id, assignee, _ in result.spawned
                ],
            },
        }

    monkeypatch.setattr(safe, "_dispatch_board", native_dispatch)

    result = safe._dispatch_once()

    assert result["ok"] is True
    assert calls == [0, 1]
    assert result["payload"]["reclaimed"] == 1
    assert len(result["payload"]["spawned"]) == 1
    assert result["running"] == 1
    with kb.connect_readonly_closing(db_path=db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='running'"
        ).fetchone()[0] == 1


def test_later_board_is_maintained_after_earlier_board_uses_final_slot(
    monkeypatch, tmp_path
):
    paths = {board: tmp_path / f"{board}.db" for board in ("alpha", "beta")}
    for path in paths.values():
        _seed_running_board(path, running=0, ready=1)
    monkeypatch.setenv("HERMES_SAFE_DISPATCH_MAX", "1")
    monkeypatch.setenv("HERMES_SAFE_DISPATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        safe, "_load_config", lambda: {"kanban": {"dispatch_in_gateway": False}}
    )
    monkeypatch.setattr(safe, "_parse_board_allowlist", lambda: ["alpha", "beta"])
    monkeypatch.setattr(safe, "_board_db_path", paths.__getitem__)
    monkeypatch.setattr(safe, "_probe_db", lambda board: (True, "ok"))
    calls = []

    def fake_dispatch(board, **kwargs):
        calls.append((board, kwargs["spawn_budget"]))
        spawned = (
            [{"task_id": "alpha-task"}]
            if board == "alpha" and kwargs["spawn_budget"] == 1
            else []
        )
        return {"ok": True, "board": board, "payload": {"spawned": spawned}}

    monkeypatch.setattr(safe, "_dispatch_board", fake_dispatch)

    result = safe._dispatch_once()

    assert result["ok"] is True
    assert calls == [("alpha", 0), ("beta", 0), ("alpha", 1)]
    assert len(result["payload"]["spawned"]) == 1
    assert result["running"] == 1


def test_global_cap_counts_running_rows_across_all_allowlisted_boards(monkeypatch, tmp_path):
    alpha = tmp_path / "alpha.db"
    beta = tmp_path / "beta.db"
    _seed_running_board(alpha, running=1)
    _seed_running_board(beta, running=1)
    paths = {"alpha": alpha, "beta": beta}
    monkeypatch.setenv("HERMES_SAFE_DISPATCH_MAX", "2")
    monkeypatch.setenv("HERMES_SAFE_DISPATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(safe, "_load_config", lambda: {"kanban": {"dispatch_in_gateway": False}})
    monkeypatch.setattr(safe, "_parse_board_allowlist", lambda: ["alpha", "beta"])
    monkeypatch.setattr(safe, "_board_db_path", paths.__getitem__)
    monkeypatch.setattr(safe, "_probe_db", lambda board: (True, "ok"))
    calls = []

    def fake_dispatch(board, **kwargs):
        calls.append((board, kwargs))
        return {"ok": True, "board": board, "payload": {"spawned": []}}

    monkeypatch.setattr(safe, "_dispatch_board", fake_dispatch)

    result = safe._dispatch_once()
    assert result["ok"] is True
    assert result["running"] == 2
    assert [board for board, kwargs in calls] == ["alpha", "beta"]
    assert all(kwargs["spawn_budget"] == 0 for _, kwargs in calls)


def test_remaining_global_slots_use_one_spawn_budget_and_per_board_live_cap(
    monkeypatch, tmp_path
):
    alpha = tmp_path / "alpha.db"
    beta = tmp_path / "beta.db"
    _seed_running_board(alpha, running=1)
    _seed_running_board(beta, running=0, ready=1)
    paths = {"alpha": alpha, "beta": beta}
    monkeypatch.setenv("HERMES_SAFE_DISPATCH_MAX", "2")
    monkeypatch.setenv("HERMES_SAFE_DISPATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(safe, "_load_config", lambda: {"kanban": {"dispatch_in_gateway": False}})
    monkeypatch.setattr(safe, "_parse_board_allowlist", lambda: ["alpha", "beta"])
    monkeypatch.setattr(safe, "_board_db_path", paths.__getitem__)
    monkeypatch.setattr(safe, "_probe_db", lambda board: (True, "ok"))
    calls = []

    def fake_dispatch(board, **kwargs):
        calls.append((board, kwargs))
        spawned = (
            [{"task_id": "new"}]
            if board == "beta" and kwargs["spawn_budget"] == 1
            else []
        )
        return {"ok": True, "board": board, "payload": {"spawned": spawned}}

    monkeypatch.setattr(safe, "_dispatch_board", fake_dispatch)
    result = safe._dispatch_once()
    assert len(result["payload"]["spawned"]) == 1
    assert calls == [
        ("alpha", {"failure_limit": 2, "dry_run": False, "max_spawn": 2, "spawn_budget": 0}),
        ("beta", {"failure_limit": 2, "dry_run": False, "max_spawn": 1, "spawn_budget": 0}),
        ("alpha", {"failure_limit": 2, "dry_run": False, "max_spawn": 2, "spawn_budget": 1}),
        ("beta", {"failure_limit": 2, "dry_run": False, "max_spawn": 1, "spawn_budget": 1}),
    ]


def test_reclaimed_rows_cannot_create_an_overspawn_within_one_tick(monkeypatch, tmp_path):
    alpha = tmp_path / "alpha.db"
    beta = tmp_path / "beta.db"
    _seed_running_board(alpha, running=1)
    _seed_running_board(beta, running=1)
    paths = {"alpha": alpha, "beta": beta}
    monkeypatch.setenv("HERMES_SAFE_DISPATCH_MAX", "2")
    monkeypatch.setenv("HERMES_SAFE_DISPATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(safe, "_load_config", lambda: {"kanban": {"dispatch_in_gateway": False}})
    monkeypatch.setattr(safe, "_parse_board_allowlist", lambda: ["alpha", "beta"])
    monkeypatch.setattr(safe, "_board_db_path", paths.__getitem__)
    monkeypatch.setattr(safe, "_probe_db", lambda board: (True, "ok"))
    calls = []

    def fake_dispatch(board, **kwargs):
        calls.append((board, kwargs["spawn_budget"]))
        if board == "alpha" and kwargs["spawn_budget"] == 0:
            with sqlite3.connect(alpha) as conn:
                conn.execute("DELETE FROM tasks WHERE status = 'running'")
        spawned = [{"task_id": board}] if kwargs["spawn_budget"] else []
        return {"ok": True, "board": board, "payload": {"spawned": spawned}}

    monkeypatch.setattr(safe, "_dispatch_board", fake_dispatch)
    result = safe._dispatch_once()
    assert len(result["payload"]["spawned"]) == 1
    assert calls == [("alpha", 0), ("beta", 0), ("alpha", 1)]


def test_embedded_dispatcher_refusal_is_a_clean_skip(monkeypatch):
    monkeypatch.setattr(
        safe,
        "_load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )
    result = safe._dispatch_once()
    assert result["ok"] is True
    assert result["skipped"] == "embedded_dispatcher_enabled"


def test_summary_and_interesting_include_board_maintenance_and_errors():
    result = {
        "ok": True,
        "max_spawn": 2,
        "boards": [
            {
                "board": "alpha",
                "ok": True,
                "spawned": 1,
                "payload": {"spawned": [{"task_id": "t1"}], "reclaimed": 2},
            }
        ],
        "payload": {"spawned": [{"task_id": "t1", "board": "alpha"}], "reclaimed": 2},
    }
    assert safe._interesting(result) is True
    summary = json.loads(safe._summarize(result))
    assert summary["boards"][0]["board"] == "alpha"
    assert summary["boards"][0]["reclaimed"] == 2
    assert safe._interesting({"ok": False, "error": "board alpha failed"}) is True


def test_dry_run_subprocess_is_exactly_filesystem_side_effect_free(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "kanban.db"
    kb.init_db(db_path=db_path)
    (home / "config.yaml").write_text(
        "kanban:\n  dispatch_in_gateway: false\n  failure_limit: 2\n",
        encoding="utf-8",
    )
    before = _snapshot_tree(home)

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {
            "HERMES_HOME",
            "HERMES_PROFILE",
            "HERMES_REPO",
            "HERMES_PYTHON",
            "HERMES_KANBAN_BOARD",
            "HERMES_KANBAN_DB",
            "HERMES_KANBAN_HOME",
            "HERMES_KANBAN_WORKSPACES_ROOT",
            "HERMES_SAFE_DISPATCH_BOARDS",
            "HERMES_SAFE_DISPATCH_MAX",
            "HERMES_SAFE_DISPATCH_STATE_DIR",
        }
    }
    env.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_REPO": str(SCRIPT.parents[1]),
            "HERMES_PYTHON": sys.executable,
            "HERMES_SAFE_DISPATCH_MAX": "1",
        }
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--once", "--dry-run"],
        cwd=str(SCRIPT.parents[1]),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert _snapshot_tree(home) == before
