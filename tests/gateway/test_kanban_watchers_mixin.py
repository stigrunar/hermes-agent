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
async def test_dispatcher_validates_canonical_policy_before_singleton_lock(
    monkeypatch,
):
    import gateway.kanban_watchers as watchers
    import hermes_cli.config as config_module
    from hermes_cli import kanban_db

    monkeypatch.setattr(config_module, "load_config", lambda: {"kanban": {}})
    monkeypatch.setattr(
        kanban_db,
        "prepare_dispatch_admission",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad policy")),
    )
    monkeypatch.setattr(
        watchers,
        "_acquire_singleton_lock",
        lambda *_a, **_k: pytest.fail("singleton lock acquired before validation"),
    )
    monkeypatch.setattr(
        kanban_db,
        "connect",
        lambda *_a, **_k: pytest.fail("gateway opened DB before validation"),
    )
    runner = GatewayKanbanWatchersMixin()
    runner._running = False
    await runner._kanban_dispatcher_watcher()


@pytest.mark.asyncio
async def test_dispatcher_pregates_incomplete_canonical_config_before_connect(
    tmp_path, monkeypatch, caplog,
):
    """The gateway's real pre-gate rejects incomplete adaptive policy first."""
    import gateway.kanban_watchers as watchers
    from hermes_cli import kanban_db

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
        kanban_db,
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
    from hermes_cli import kanban_db

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BASE_DIR", raising=False)
    config = {"kanban": {"auto_decompose": False}}
    snapshot = {"kanban": {"_canonical_parallel_dispatch": True}}
    monkeypatch.setattr(config_module, "load_config", lambda: config)
    monkeypatch.setattr(
        kanban_db, "prepare_dispatch_admission", lambda *_a, **_k: snapshot
    )
    monkeypatch.setattr(
        kanban_db, "resolve_worker_profile_admission", lambda *_a, **_k: ["alice"]
    )
    monkeypatch.setattr(
        watchers, "_acquire_singleton_lock", lambda *_a, **_k: (None, "unavailable")
    )
    monkeypatch.setattr(
        kanban_db, "list_boards",
        lambda **_k: [{"slug": "default"}, {"slug": "second"}],
    )
    monkeypatch.setattr(kanban_db, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kanban_db, "review_dispatch_enabled", lambda: False)
    monkeypatch.setattr(kanban_db, "has_spawnable_ready", lambda _conn: False)

    class FakeConnection:
        def close(self):
            return None

    monkeypatch.setattr(kanban_db, "connect", lambda **_k: FakeConnection())
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
        return kanban_db.DispatchResult()

    monkeypatch.setattr(kanban_db, "dispatch_once", dispatch_once)
    await runner._kanban_dispatcher_watcher()
    assert received == [snapshot, snapshot]
