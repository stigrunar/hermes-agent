"""Regression tests for independent kanban notifier/dispatcher ownership."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner


def _make_runner(*, connected=True, multiplex=False):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = (
        {Platform.TELEGRAM: MagicMock()} if connected and not multiplex else {}
    )
    runner._profile_adapters = (
        {"writer": {Platform.DISCORD: MagicMock()}} if connected and multiplex else {}
    )
    runner._kanban_sub_fail_counts = {}
    runner._active_profile_name = lambda: "default"
    return runner


def _config(**kanban):
    return {"kanban": kanban}


def test_notify_false_disables_before_board_poll():
    runner = _make_runner()
    with (
        patch(
            "hermes_cli.config.load_config",
            return_value=_config(notify_in_gateway=False),
        ),
        patch("gateway.kanban_watchers._acquire_singleton_lock") as acquire,
        patch("hermes_cli.kanban_db.list_boards") as list_boards,
        patch("hermes_cli.kanban_db.connect") as connect,
    ):
        asyncio.run(runner._kanban_notifier_watcher())

    acquire.assert_not_called()
    list_boards.assert_not_called()
    connect.assert_not_called()


def test_notify_default_true_is_backward_compatible():
    runner = _make_runner()

    async def stop_after_start(_delay):
        runner._running = False

    with (
        patch("hermes_cli.config.load_config", return_value=_config()),
        patch("gateway.kanban_watchers.asyncio.sleep", side_effect=stop_after_start),
    ):
        asyncio.run(runner._kanban_notifier_watcher())


@pytest.mark.parametrize("lock_state", ["contended", "unavailable"])
def test_non_owner_does_not_enumerate_or_open_boards(lock_state):
    runner = _make_runner()
    sleep_calls = []
    real_sleep = asyncio.sleep

    async def stop_after_retry(delay):
        sleep_calls.append(delay)
        await real_sleep(0)
        if delay != 5:
            runner._running = False

    with (
        patch("hermes_cli.config.load_config", return_value=_config()),
        patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            return_value=(None, lock_state),
        ) as acquire,
        patch("hermes_cli.kanban_db.list_boards") as list_boards,
        patch("hermes_cli.kanban_db.connect") as connect,
        patch(
            "gateway.kanban_watchers.asyncio.sleep",
            side_effect=stop_after_retry,
        ),
    ):
        asyncio.run(runner._kanban_notifier_watcher(interval=0.1))

    acquire.assert_called_once()
    list_boards.assert_not_called()
    connect.assert_not_called()
    assert sleep_calls == [5, 0.1, 0.1]


def test_disconnected_process_does_not_acquire_but_multiplex_adapter_does():
    runner = _make_runner(connected=False)
    acquire = MagicMock(return_value=(MagicMock(), "held"))
    sleep_calls = []
    real_sleep = asyncio.sleep

    async def connect_multiplex_after_wait(delay):
        sleep_calls.append(delay)
        await real_sleep(0)
        if delay == 0.1:
            runner._profile_adapters = {
                "writer": {Platform.DISCORD: MagicMock()}
            }

    async def owner_once(*, interval, notifier_profile):
        runner._running = False

    with (
        patch("hermes_cli.config.load_config", return_value=_config()),
        patch("gateway.kanban_watchers._acquire_singleton_lock", acquire),
        patch(
            "gateway.kanban_watchers.asyncio.sleep",
            side_effect=connect_multiplex_after_wait,
        ),
        patch.object(
            runner, "_kanban_notifier_owner_loop", side_effect=owner_once,
        ),
    ):
        asyncio.run(runner._kanban_notifier_watcher(interval=0.1))

    assert sleep_calls == [5, 0.1, 0.1]
    acquire.assert_called_once()


def test_owner_loop_with_no_adapters_returns_before_board_poll():
    runner = _make_runner(connected=False)
    with (
        patch("hermes_cli.kanban_db.list_boards") as list_boards,
        patch("hermes_cli.kanban_db.connect") as connect,
    ):
        asyncio.run(
            runner._kanban_notifier_owner_loop(
                interval=0.1, notifier_profile="default",
            )
        )

    list_boards.assert_not_called()
    connect.assert_not_called()


def test_owner_releases_lease_on_cancellation():
    runner = _make_runner()
    lock_handle = MagicMock()
    real_sleep = asyncio.sleep
    owner_started = asyncio.Event()

    async def no_delay(_delay):
        await real_sleep(0)

    async def waiting_owner(*, interval, notifier_profile):
        owner_started.set()
        await asyncio.Event().wait()

    async def scenario():
        watcher = asyncio.create_task(runner._kanban_notifier_watcher(interval=0.1))
        await owner_started.wait()
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher

    with (
        patch("hermes_cli.config.load_config", return_value=_config()),
        patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            return_value=(lock_handle, "held"),
        ),
        patch(
            "gateway.kanban_watchers._release_singleton_lock",
        ) as release,
        patch("gateway.kanban_watchers.asyncio.sleep", side_effect=no_delay),
        patch.object(
            runner, "_kanban_notifier_owner_loop", side_effect=waiting_owner,
        ),
    ):
        asyncio.run(scenario())

    release.assert_called_once_with(lock_handle)



def test_owner_loss_releases_and_waiter_can_take_over_then_reconnect():
    runner = _make_runner()
    first_lock = MagicMock(name="first_lock")
    second_lock = MagicMock(name="second_lock")
    acquisitions = iter(
        [
            (first_lock, "held"),
            (None, "contended"),
            (second_lock, "held"),
        ]
    )
    owner_calls = 0
    real_sleep = asyncio.sleep

    async def owner_lifecycle(*, interval, notifier_profile):
        nonlocal owner_calls
        owner_calls += 1
        if owner_calls == 1:
            runner.adapters.clear()
            return
        runner._running = False

    async def reconnect_or_retry(_delay):
        await real_sleep(0)
        if not runner.adapters:
            runner.adapters[Platform.TELEGRAM] = MagicMock()

    with (
        patch("hermes_cli.config.load_config", return_value=_config()),
        patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            side_effect=lambda _path: next(acquisitions),
        ) as acquire,
        patch(
            "gateway.kanban_watchers._release_singleton_lock",
        ) as release,
        patch(
            "gateway.kanban_watchers.asyncio.sleep",
            side_effect=reconnect_or_retry,
        ),
        patch.object(
            runner, "_kanban_notifier_owner_loop", side_effect=owner_lifecycle,
        ),
    ):
        asyncio.run(runner._kanban_notifier_watcher(interval=0.1))

    assert acquire.call_count == 3
    assert release.call_args_list == [
        ((first_lock,),),
        ((second_lock,),),
    ]
    assert owner_calls == 2


def test_profile_notifier_leases_are_isolated(tmp_path):
    from gateway.kanban_watchers import _profile_notifier_lock_path

    default_lock = _profile_notifier_lock_path(tmp_path, "default")
    design_lock = _profile_notifier_lock_path(tmp_path, "dollydesign")

    assert default_lock != design_lock
    assert default_lock.parent == design_lock.parent == tmp_path / "kanban"


def test_connected_profiles_include_primary_and_multiplex_adapters():
    from gateway.kanban_watchers import _connected_kanban_profiles

    runner = _make_runner()
    runner._profile_adapters = {
        "writer": {Platform.DISCORD: MagicMock()},
        "offline": {},
    }

    assert _connected_kanban_profiles(runner) == {"default", "writer"}


def test_dispatcher_watcher_remains_disabled_by_config(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)
    runner = _make_runner()

    with (
        patch(
            "hermes_cli.config.load_config",
            return_value=_config(dispatch_in_gateway=False),
        ),
        patch("hermes_cli.kanban_db.dispatch_once") as dispatch_once,
    ):
        asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=2))

    dispatch_once.assert_not_called()
