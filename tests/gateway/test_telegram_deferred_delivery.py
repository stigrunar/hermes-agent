"""Focused proof for durable Telegram flood-control deferral."""

import asyncio
import inspect
import re
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    monkeypatch.setattr(dl.random, "uniform", lambda _low, _high: 0.0)


def _record_due(oid="ob-1", *, profile="default", now=100.0):
    dl.record_obligation(
        obligation_id=oid,
        session_key=f"agent:{profile}:telegram:dm:C1",
        platform="telegram",
        chat_id="C1",
        thread_id=None,
        content=f"answer {oid}",
        adapter_profile=profile,
    )
    dl.mark_deferred(oid, 0, now=now)


def _row(oid="ob-1"):
    with dl._connect() as conn:
        row = conn.execute(
            """SELECT state, attempts, last_error, retry_not_before
               FROM delivery_obligations WHERE obligation_id=?""",
            (oid,),
        ).fetchone()
    return row


async def _wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.01)


def _runner(adapter, *, active_profile="default"):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner._telegram_deferred_drains = {}
    runner._telegram_deferred_wakeups = {}
    runner._active_profile_name = lambda: active_profile
    runner._clear_resume_pending_for_claimed_obligations = AsyncMock(
        side_effect=lambda rows, require_success=False: rows
    )
    return runner


async def _cancel(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_ack_transitions_deferred_row_to_delivered():
    _record_due()
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="42"))
    runner = _runner(adapter)
    task = runner._schedule_telegram_deferred_delivery(profile="default")
    await _wait_for(lambda: _row()[0] == "delivered")
    assert adapter.send.await_count == 1
    await _cancel(task)


@pytest.mark.asyncio
async def test_repeated_flood_reschedules_same_obligation():
    _record_due()
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=SendResult(
        success=False, error="flood_control:30.0",
        raw_response={"delivery_state": "deferred"},
        error_kind="rate_limited", retry_after=30.0, retryable=False,
    ))
    runner = _runner(adapter)
    task = runner._schedule_telegram_deferred_delivery(profile="default")
    await _wait_for(lambda: _row()[0] == "deferred" and _row()[1] == 1)
    state, attempts, error, due = _row()
    assert (state, attempts, error) == ("deferred", 1, "flood_control")
    assert due is not None
    assert adapter.send.await_count == 1
    await _cancel(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, SendResult(success=False, retryable=True)])
async def test_transient_failure_remains_durable_and_retryable(result):
    _record_due()
    adapter = MagicMock()
    adapter.send = (
        AsyncMock(side_effect=ConnectionError("offline"))
        if result is None else AsyncMock(return_value=result)
    )
    runner = _runner(adapter)
    task = runner._schedule_telegram_deferred_delivery(profile="default")
    await _wait_for(lambda: _row()[0] == "deferred" and _row()[2] == "transient_delivery")
    state, attempts, error, due = _row()
    assert (state, attempts, error) == ("deferred", 1, "transient_delivery")
    assert due is not None
    await _cancel(task)


@pytest.mark.asyncio
async def test_explicit_permanent_failure_is_terminal():
    _record_due()
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=SendResult(
        success=False, retryable=False, error_kind="forbidden",
    ))
    runner = _runner(adapter)
    task = runner._schedule_telegram_deferred_delivery(profile="default")
    await _wait_for(lambda: _row()[0] == "failed")
    state, attempts, error, _due = _row()
    assert (state, attempts, error) == ("failed", 1, "forbidden")
    await _cancel(task)


@pytest.mark.asyncio
async def test_named_active_primary_profile_uses_its_exact_adapter_and_queue():
    _record_due(profile="dollyops")
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="7"))
    runner = _runner(adapter, active_profile="dollyops")
    task = runner._schedule_telegram_deferred_delivery(profile="dollyops")
    await _wait_for(lambda: _row()[0] == "delivered")
    runner._clear_resume_pending_for_claimed_obligations.assert_awaited()
    adapter.send.assert_awaited_once()
    await _cancel(task)


@pytest.mark.asyncio
async def test_cancellation_waits_for_real_executor_ledger_call(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocking_claim(*, profile):
        started.set()
        assert release.wait(timeout=5.0)
        return None

    monkeypatch.setattr(dl, "claim_due_deferred", blocking_claim)
    runner = _runner(MagicMock())
    task = asyncio.create_task(
        runner._drain_telegram_deferred_delivery("default", asyncio.Event())
    )
    await _wait_for(started.is_set)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done(), "drain detached its still-running executor call"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_one_coalesced_drain_task_per_profile():
    runner = _runner(MagicMock())
    first = runner._schedule_telegram_deferred_delivery(profile="default")
    second = runner._schedule_telegram_deferred_delivery(profile="default")
    named = runner._schedule_telegram_deferred_delivery(profile="reviewer")
    assert first is second
    assert named is not first
    assert set(runner._telegram_deferred_drains) == {"default", "reviewer"}
    await _cancel(first)
    await _cancel(named)


def test_every_drain_ledger_operation_uses_cancellation_safe_wrapper():
    source = inspect.getsource(GatewayRunner._drain_telegram_deferred_delivery)
    assert source.count("asyncio.to_thread(") == 1
    for operation in (
        "claim_due_deferred", "release_deferred_claim", "mark_delivered",
        "mark_deferred", "mark_deferred_failed", "next_deferred_due",
    ):
        assert re.search(rf"_ledger_call\(\s*{operation}\b", source)
