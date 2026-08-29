"""Regression tests for #33488 (CLI max_in_progress / max_spawn / per-profile
config passthrough) and #29415 (kanban_swarm humanizer skill ref).

These two fixes are bundled because they're both small, both touch the
kanban dispatcher's CLI surface, and they each guard against a silent
operator footgun that only manifests in long-running setups.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Spin up a fresh HERMES_HOME with a clean kanban DB."""
    test_home = tempfile.mkdtemp(prefix="kanban_cli_passthrough_")
    os.makedirs(os.path.join(test_home, "profiles", "default"), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    monkeypatch.setenv("HERMES_KANBAN_HOME", test_home)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BASE_DIR", raising=False)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    yield test_home


def test_cli_dispatch_passes_max_in_progress_from_config(isolated_kanban_home, monkeypatch):
    """#33488: hermes kanban dispatch must pass kanban.max_in_progress from
    config to dispatch_once. Without this, the global concurrency cap is
    unreachable from the CLI even though it works from the gateway."""
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    # Configure max_in_progress in the loaded config.
    fake_config = {
        "kanban": {
            "max_in_progress": 3,
            "max_spawn": 5,
            "default_assignee": "default",
            "max_in_progress_per_profile": 2,
        }
    }
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: fake_config
    )
    monkeypatch.setattr(
        kanban_db, "prepare_dispatch_admission", lambda cfg, **_kwargs: cfg
    )

    captured = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kanban_db.DispatchResult()

    monkeypatch.setattr(kanban_db, "dispatch_once", fake_dispatch_once)

    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False)
    kb_cli._cmd_dispatch(args)

    # Every config value must have reached dispatch_once.
    assert captured.get("max_in_progress") == 3, (
        f"CLI must pass kanban.max_in_progress from config; got {captured.get('max_in_progress')!r}"
    )
    assert captured.get("max_spawn") == 5, (
        f"CLI must pass kanban.max_spawn from config when --max is not provided; got {captured.get('max_spawn')!r}"
    )
    assert captured.get("default_assignee") == "default"
    assert captured.get("max_in_progress_per_profile") == 2


def test_cli_max_flag_overrides_config_max_spawn(isolated_kanban_home, monkeypatch):
    """--max on the CLI takes precedence over kanban.max_spawn in config.
    The CLI flag is the explicit operator signal; config is the default."""
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    fake_config = {"kanban": {"max_spawn": 10}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: fake_config)
    monkeypatch.setattr(
        kanban_db, "prepare_dispatch_admission", lambda cfg, **_kwargs: cfg
    )

    captured = {}
    monkeypatch.setattr(
        kanban_db, "dispatch_once",
        lambda conn, **kw: (captured.update(kw), kanban_db.DispatchResult())[1],
    )

    args = argparse.Namespace(dry_run=True, max=2, failure_limit=2, json=False)
    kb_cli._cmd_dispatch(args)

    assert captured.get("max_spawn") == 2, (
        f"CLI --max=2 must override config kanban.max_spawn=10; got {captured.get('max_spawn')!r}"
    )


def test_cli_spawn_budget_is_separate_from_live_cap(
    isolated_kanban_home, monkeypatch,
):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"kanban": {"max_spawn": 4}}
    )
    monkeypatch.setattr(
        kanban_db, "prepare_dispatch_admission", lambda cfg, **_kwargs: cfg
    )
    captured = {}
    monkeypatch.setattr(
        kanban_db, "dispatch_once",
        lambda conn, **kw: (captured.update(kw), kanban_db.DispatchResult())[1],
    )
    args = argparse.Namespace(
        dry_run=True, max=3, spawn_budget=1, failure_limit=2, json=False
    )
    kb_cli._cmd_dispatch(args)
    assert captured["max_spawn"] == 3
    assert captured["max_new_spawns"] == 1


def test_cli_dispatch_pregates_canonical_policy_before_init_or_connect(
    isolated_kanban_home, monkeypatch, capsys,
):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BASE_DIR", raising=False)
    with open(os.path.join(isolated_kanban_home, "config.yaml"), "w", encoding="utf-8") as handle:
        handle.write("kanban:\n  max_in_progress: 2\n")
    monkeypatch.setattr(
        kanban_db,
        "init_db",
        lambda *_args, **_kwargs: pytest.fail("CLI initialized DB before admission"),
    )
    monkeypatch.setattr(
        kanban_db,
        "connect_closing",
        lambda *_args, **_kwargs: pytest.fail("CLI opened DB before admission"),
    )
    monkeypatch.setattr(
        kanban_db,
        "connect_readonly_closing",
        lambda *_args, **_kwargs: pytest.fail("CLI opened preview DB before admission"),
    )
    args = argparse.Namespace(
        kanban_action="dispatch",
        dry_run=True,
        max=None,
        spawn_budget=None,
        failure_limit=2,
        json=False,
    )
    assert kb_cli.kanban_command(args) == 1
    assert "allowed_worker_profiles" in capsys.readouterr().err
    assert not os.path.exists(os.path.join(isolated_kanban_home, "kanban.db"))


def test_cli_dispatch_passes_the_prepared_snapshot_by_identity(
    isolated_kanban_home, monkeypatch,
):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    snapshot = {"kanban": {"_canonical_parallel_dispatch": True}}
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"kanban": {}}
    )
    monkeypatch.setattr(
        kanban_db, "prepare_dispatch_admission", lambda *_a, **_k: snapshot
    )

    class Scope:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return None

    received = {}
    monkeypatch.setattr(kanban_db, "connect_readonly_closing", lambda: Scope())
    monkeypatch.setattr(
        kanban_db,
        "dispatch_once",
        lambda _conn, **kwargs: (
            received.update(kwargs), kanban_db.DispatchResult()
        )[1],
    )
    args = argparse.Namespace(
        dry_run=True,
        max=None,
        spawn_budget=None,
        failure_limit=2,
        json=False,
    )
    kb_cli._cmd_dispatch(args)
    assert received["effective_config"] is snapshot


def test_forced_daemon_validates_before_database_initialization(
    isolated_kanban_home, monkeypatch,
):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"max_in_progress": "wide"}},
    )
    monkeypatch.setattr(
        kanban_db, "init_db", lambda *_a, **_k: pytest.fail("DB initialized")
    )
    args = argparse.Namespace(
        force=True, max=None, interval=1.0, failure_limit=2,
        pidfile=None, verbose=False,
    )
    with pytest.raises(ValueError, match="positive integer"):
        kb_cli._cmd_daemon(args)


def test_cli_forced_daemon_wrapper_pregates_before_auto_init(
    isolated_kanban_home, monkeypatch, capsys,
):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"max_in_progress": "wide"}},
    )
    monkeypatch.setattr(
        kanban_db, "init_db", lambda *_a, **_k: pytest.fail("DB initialized")
    )
    args = argparse.Namespace(
        kanban_action="daemon", force=True, max=None, interval=1.0,
        failure_limit=2, pidfile=None, verbose=False,
    )
    assert kb_cli.kanban_command(args) == 1
    assert "positive integer" in capsys.readouterr().err
