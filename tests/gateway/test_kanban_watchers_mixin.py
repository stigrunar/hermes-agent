"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


@pytest.mark.asyncio
async def test_dispatcher_pregates_incomplete_canonical_config_before_connect(
    tmp_path, monkeypatch, caplog,
):
    """The gateway's real pre-gate rejects incomplete adaptive policy first."""
    import gateway.kanban_watchers as watchers
    from hermes_cli import kanban_db_connect as kbc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BASE_DIR", raising=False)
    (tmp_path / "config.yaml").write_text(
        "kanban:\n  max_in_progress: 2\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        watchers,
        "_acquire_singleton_lock",
        lambda *_a, **_k: pytest.fail("singleton lock acquired before validation"),
    )
    monkeypatch.setattr(
        kbc,
        "connect",
        lambda *_a, **_k: pytest.fail("gateway opened DB before validation"),
    )
    runner = GatewayKanbanWatchersMixin()
    runner._running = False
    with caplog.at_level("ERROR", logger="gateway.run"):
        await runner._kanban_dispatcher_watcher()
    assert "allowed_worker_profiles" in caplog.text


@pytest.mark.asyncio
async def test_dispatcher_passes_one_canonical_snapshot_to_every_board(
    tmp_path, monkeypatch,
):
    import gateway.kanban_watchers as watchers
    import hermes_cli.config as config_module
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BASE_DIR", raising=False)
    config = {"kanban": {"auto_decompose": False}}
    snapshot = {"kanban": {"_canonical_parallel_dispatch": True}}
    monkeypatch.setattr(config_module, "load_config", lambda: config)
    monkeypatch.setattr(
        kanban_db_dispatch, "prepare_dispatch_admission", lambda *_a, **_k: snapshot
    )
    monkeypatch.setattr(
        kanban_db_dispatch, "resolve_worker_profile_admission", lambda *_a, **_k: ["alice"]
    )
    monkeypatch.setattr(
        watchers, "_acquire_singleton_lock", lambda *_a, **_k: (None, "unavailable")
    )
    monkeypatch.setattr(
        kanban_db, "list_boards",
        lambda **_k: [{"slug": "default"}, {"slug": "second"}],
    )
    monkeypatch.setattr(kanban_db_dispatch, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kanban_db_dispatch, "review_dispatch_enabled", lambda: False)
    monkeypatch.setattr(kanban_db_dispatch, "has_spawnable_ready", lambda _conn: False)

    class FakeConnection:
        def close(self):
            return None

    monkeypatch.setattr(kanban_db_connect, "connect", lambda **_k: FakeConnection())
    monkeypatch.setattr(watchers, "_kanban_dispatch_allowed", lambda: True)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(watchers.asyncio, "sleep", no_sleep)

    async def direct_call(function, *args):
        return function(*args)

    monkeypatch.setattr(watchers.asyncio, "to_thread", direct_call)
    runner = GatewayKanbanWatchersMixin()
    runner._running = True
    received = []

    def dispatch_once(_conn, **kwargs):
        received.append(kwargs["effective_config"])
        if len(received) == 2:
            runner._running = False
        return kanban_db_dispatch.DispatchResult()

    monkeypatch.setattr(kanban_db_dispatch, "dispatch_once", dispatch_once)
    await runner._kanban_dispatcher_watcher()
    assert received[0] is snapshot
    assert received[1] is snapshot


def test_gateway_dispatcher_stuck_warning_names_guard_reason(monkeypatch, caplog):
    """The embedded dispatcher's "stuck" warning names the respawn-guard reason
    holding the ready queue (#111910) instead of a bare zero-spawn count."""
    import asyncio
    import logging

    import gateway.kanban_watchers as kw
    from hermes_cli import kanban_db_dispatch as kbd

    held = kbd.DispatchResult(respawn_guarded=[("t_held", "active_pr")])
    runner = object.__new__(kw.GatewayKanbanWatchersMixin)
    runner._running = True
    monkeypatch.setattr(
        runner,
        "_kanban_dispatcher_boot",
        lambda: (lambda: {}, object(), {}, None, None, None, {}),
    )

    class _Dispatcher:
        def __init__(self, *a, **k):
            pass

        def tick_once(self):
            return [("board", held)]

        def ready_nonempty(self):
            return True

    ticks = {"n": 0}

    async def _direct(fn, *args):
        return fn(*args)

    async def _sleep(_delay):
        ticks["n"] += 1
        if ticks["n"] > kw._HEALTH_WINDOW:
            runner._running = False

    monkeypatch.setattr(kw, "_KanbanDispatcher", _Dispatcher)
    monkeypatch.setattr(
        kw,
        "_resolve_dispatcher_settings",
        lambda cfg, kb, **kwargs: type("S", (), {"interval": 1.0})(),
    )
    monkeypatch.setattr(kw, "_to_thread_process_service", _direct)
    monkeypatch.setattr(kw, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(kw, "_resolve_auto_decompose_settings", lambda load_config: (False, 0))
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kw.asyncio, "sleep", _sleep)

    with caplog.at_level(logging.WARNING, logger=kw.logger.name):
        asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=5.0))

    stuck = [r.getMessage() for r in caplog.records if "dispatcher stuck" in r.getMessage()]
    assert stuck, [r.getMessage() for r in caplog.records]
    assert "Last tick held back: active_pr=1." in stuck[0]
