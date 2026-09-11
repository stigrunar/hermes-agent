"""Focused proof for durable Telegram flood-control deferral."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from gateway import delivery_ledger as dl
from gateway.config import Platform
from gateway.platforms.base import SendResult


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    monkeypatch.setattr(dl.random, "uniform", lambda _low, _high: 0.0)
    # Native deadline scheduling adds a small production slack.  Keep this
    # invariant test focused on the claim/send path without sleeping for the
    # full slack interval.
    monkeypatch.setattr(dl, "FLOOD_RETRY_SLACK_SECONDS", 0.0)


@pytest_asyncio.fixture(autouse=True)
async def _shutdown_test_executor():
    """Synchronously retire the executor created after importing gateway.run."""
    yield
    loop = asyncio.get_running_loop()
    executor = loop._default_executor
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)
        loop._default_executor = None


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
    # Importing gateway.run has process-wide provider/plugin side effects.
    # Keep collection inert so unrelated suites retain their own baseline.
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner._flood_redelivery_tasks = {}
    runner._flood_redelivery_wakes = {}
    runner._background_tasks = set()
    runner._running = True
    runner._active_profile_name = lambda: active_profile
    runner._clear_resume_pending_for_claimed_obligations = AsyncMock(
        side_effect=lambda rows, require_success=False: rows
    )
    return runner


def _native_task(runner, profile="default"):
    """Return the task owned by the native per-adapter flood queue."""
    return runner._flood_redelivery_tasks[(Platform.TELEGRAM.value, profile)]


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
    runner._schedule_flood_redelivery(Platform.TELEGRAM, profile="default")
    task = _native_task(runner)
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
    runner._schedule_flood_redelivery(Platform.TELEGRAM, profile="default")
    task = _native_task(runner)
    await _wait_for(lambda: _row()[:2] == ("deferred", 1))
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
    runner._schedule_flood_redelivery(Platform.TELEGRAM, profile="default")
    task = _native_task(runner)
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
    runner._schedule_flood_redelivery(Platform.TELEGRAM, profile="default")
    task = _native_task(runner)
    await _wait_for(lambda: _row()[0] == "failed")
    state, attempts, error, _due = _row()
    assert (state, attempts, error) == ("failed", 1, "forbidden")
    await _cancel(task)


@pytest.mark.asyncio
async def test_retry_after_uncertain_send_has_duplicate_warning():
    _record_due()
    adapter = MagicMock()
    adapter.send = AsyncMock(side_effect=[
        SendResult(success=False, retryable=True, retry_after=0),
        SendResult(success=True, message_id="42"),
    ])
    runner = _runner(adapter)
    runner._schedule_flood_redelivery(Platform.TELEGRAM, profile="default")
    task = _native_task(runner)
    await _wait_for(lambda: _row()[0] == "delivered")
    from gateway.delivery_ledger import RECONNECTED_MARKER
    assert adapter.send.await_args_list[0].kwargs["content"] == "answer ob-1"
    assert adapter.send.await_args_list[1].kwargs["content"] == (
        RECONNECTED_MARKER + "answer ob-1"
    )
    await _cancel(task)


@pytest.mark.asyncio
async def test_resume_clear_failure_gets_bounded_due_retry():
    _record_due()
    runner = _runner(MagicMock())
    runner._clear_resume_pending_for_claimed_obligations = AsyncMock(return_value=[])
    runner._schedule_flood_redelivery(Platform.TELEGRAM, profile="default")
    task = _native_task(runner)
    await _wait_for(
        lambda: _row()[0] == "deferred"
        and _row()[2] == "resume_pending_clear_failed"
        and _row()[3] > 100.0
    )
    assert _row()[1] == 0
    await _cancel(task)


@pytest.mark.asyncio
async def test_named_active_primary_profile_uses_its_exact_adapter_and_queue():
    _record_due(profile="dollyops")
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="7"))
    runner = _runner(adapter, active_profile="dollyops")
    runner._schedule_flood_redelivery(Platform.TELEGRAM, profile="dollyops")
    task = _native_task(runner, "dollyops")
    await _wait_for(lambda: _row()[0] == "delivered")
    runner._clear_resume_pending_for_claimed_obligations.assert_awaited()
    adapter.send.assert_awaited_once()
    await _cancel(task)


@pytest.mark.asyncio
async def test_unowned_legacy_deferred_row_is_recovered_by_native_timer():
    """A deferred row left by an older process survives restart without an owner."""
    _record_due()
    with dl._connect() as conn:
        conn.execute(
            "UPDATE delivery_obligations SET owner_pid=NULL, owner_started_at=NULL "
            "WHERE obligation_id=?",
            ("ob-1",),
        )
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="8"))
    runner = _runner(adapter)
    runner._schedule_flood_redelivery(Platform.TELEGRAM, profile="default")
    task = _native_task(runner)
    await _wait_for(lambda: _row()[0] == "delivered")
    adapter.send.assert_awaited_once()
    await _cancel(task)


@pytest.mark.asyncio
async def test_ingress_refusal_is_persisted_then_native_timer_redelivers_ack():
    """The finalizer records a flood refusal, then the native timer gets its ACK."""
    from gateway.platforms.base import BasePlatformAdapter

    class _FinalizerAdapter(BasePlatformAdapter):
        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return SendResult(success=True)

        async def get_chat_info(self, chat_id):
            return {}

    dl.record_obligation(
        obligation_id="ob-1",
        session_key="agent:default:telegram:dm:C1",
        platform="telegram",
        chat_id="C1",
        thread_id=None,
        content="answer ob-1",
        adapter_profile="default",
    )
    dl.mark_attempting("ob-1")
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="9"))
    runner = _runner(adapter)
    delivery_adapter = _FinalizerAdapter.__new__(_FinalizerAdapter)
    delivery_adapter.gateway_runner = runner
    delivery_adapter._owner_profile = "default"
    event = SimpleNamespace(source=SimpleNamespace(platform=Platform.TELEGRAM))

    await delivery_adapter._finalize_delivery_obligation(
        "ob-1",
        SendResult(
            success=False,
            error="flood_control:0.01",
            raw_response={"delivery_state": "deferred"},
        ),
        event,
        delivery_adapter,
    )
    assert _row()[0] == "failed"
    task = _native_task(runner)
    await _wait_for(lambda: _row()[0] == "delivered")
    adapter.send.assert_awaited_once()
    await _cancel(task)


@pytest.mark.asyncio
async def test_ingress_record_cancellation_waits_for_threaded_persistence(monkeypatch):
    from gateway.platforms.base import BasePlatformAdapter

    class _RecordAdapter(BasePlatformAdapter):
        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return SendResult(success=True)

        async def get_chat_info(self, chat_id):
            return {}

    runner = _runner(MagicMock())
    adapter = _RecordAdapter.__new__(_RecordAdapter)
    adapter.gateway_runner = runner
    adapter._owner_profile = "default"
    adapter.typed_command_prefix = "!"
    event = SimpleNamespace(
        text="question",
        message_id="m1",
        source=SimpleNamespace(
            platform=Platform.TELEGRAM, chat_id="C1", thread_id=None,
        ),
    )
    started = threading.Event()
    release = threading.Event()
    original = dl.record_obligation

    def blocking_record(**kwargs):
        started.set()
        assert release.wait(timeout=5.0)
        return original(**kwargs)

    monkeypatch.setattr(dl, "record_obligation", blocking_record)
    task = asyncio.create_task(
        adapter._record_delivery_obligation(event, "agent:default:telegram:dm:C1", "answer", adapter, False)
    )
    await _wait_for(started.is_set)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "cancellation detached the threaded ledger write"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    obligation_id = dl.compute_obligation_id(
        "agent:default:telegram:dm:C1", "m1", "answer"
    )
    assert _row(obligation_id)[0] == "failed"


@pytest.mark.asyncio
async def test_ingress_finalization_cancellation_waits_for_threaded_ack(monkeypatch):
    from gateway.platforms.base import BasePlatformAdapter

    class _FinalizeAdapter(BasePlatformAdapter):
        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return SendResult(success=True)

        async def get_chat_info(self, chat_id):
            return {}

    dl.record_obligation(
        obligation_id="ob-1", session_key="agent:default:telegram:dm:C1",
        platform="telegram", chat_id="C1", thread_id=None, content="answer ob-1",
        adapter_profile="default",
    )
    dl.mark_attempting("ob-1")
    runner = _runner(MagicMock())
    adapter = _FinalizeAdapter.__new__(_FinalizeAdapter)
    adapter.gateway_runner = runner
    adapter._owner_profile = "default"
    event = SimpleNamespace(source=SimpleNamespace(platform=Platform.TELEGRAM, chat_id="C1"))
    started = threading.Event()
    release = threading.Event()
    original = dl.mark_delivered

    def blocking_mark_delivered(oid):
        started.set()
        assert release.wait(timeout=5.0)
        return original(oid)

    monkeypatch.setattr(dl, "mark_delivered", blocking_mark_delivered)
    task = asyncio.create_task(
        adapter._finalize_delivery_obligation(
            "ob-1", SendResult(success=True), event, adapter,
        )
    )
    await _wait_for(started.is_set)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "cancellation detached the threaded finalization"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _row()[0] == "delivered"


@pytest.mark.asyncio
async def test_flood_finalization_cancellation_still_arms_timer(monkeypatch):
    from gateway.platforms.base import BasePlatformAdapter

    class _FloodAdapter(BasePlatformAdapter):
        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return SendResult(success=True)

        async def get_chat_info(self, chat_id):
            return {}

    dl.record_obligation(
        obligation_id="ob-1", session_key="agent:default:telegram:dm:C1",
        platform="telegram", chat_id="C1", thread_id=None, content="answer ob-1",
        adapter_profile="default",
    )
    dl.mark_attempting("ob-1")
    runner = _runner(MagicMock())
    schedule = MagicMock()
    runner._schedule_flood_redelivery = schedule
    adapter = _FloodAdapter.__new__(_FloodAdapter)
    adapter.gateway_runner = runner
    adapter._owner_profile = "default"
    event = SimpleNamespace(source=SimpleNamespace(platform=Platform.TELEGRAM, chat_id="C1"))
    started = threading.Event()
    release = threading.Event()
    original = dl.mark_failed

    def blocking_mark_failed(oid, error):
        started.set()
        assert release.wait(timeout=5.0)
        return original(oid, error)

    monkeypatch.setattr(dl, "mark_failed", blocking_mark_failed)
    task = asyncio.create_task(
        adapter._finalize_delivery_obligation(
            "ob-1", SendResult(success=False, error="flood_control:30"), event, adapter,
        )
    )
    await _wait_for(started.is_set)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    schedule.assert_called_once_with(Platform.TELEGRAM, profile="default")
    assert _row()[0] == "failed"


@pytest.mark.asyncio
async def test_cancellation_waits_for_real_executor_ledger_call(monkeypatch):
    _record_due()
    started = threading.Event()
    release = threading.Event()

    def blocking_claim(*, profile):
        started.set()
        assert release.wait(timeout=5.0)
        return None

    monkeypatch.setattr(dl, "claim_due_deferred", blocking_claim)
    runner = _runner(MagicMock())
    runner._schedule_flood_redelivery(Platform.TELEGRAM, profile="default")
    task = _native_task(runner)
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
    runner._schedule_flood_redelivery(Platform.TELEGRAM, profile="default")
    first = _native_task(runner)
    runner._schedule_flood_redelivery(Platform.TELEGRAM, profile="default")
    second = _native_task(runner)
    runner._schedule_flood_redelivery(Platform.TELEGRAM, profile="reviewer")
    named = _native_task(runner, "reviewer")
    assert first is second
    assert named is not first
    assert set(runner._flood_redelivery_tasks) == {
        (Platform.TELEGRAM.value, "default"),
        (Platform.TELEGRAM.value, "reviewer"),
    }
    await _cancel(first)
    await _cancel(named)


@pytest.mark.asyncio
async def test_stopping_gateway_does_not_spawn_late_drain():
    runner = _runner(MagicMock())
    runner._running = False
    assert runner._schedule_flood_redelivery(Platform.TELEGRAM, profile="default") is None
    assert runner._flood_redelivery_tasks == {}


@pytest.mark.asyncio
async def test_native_deferred_path_routes_ledger_operations_through_wrapper():
    """The real timer path keeps every DB operation behind its async seam."""
    _record_due()
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="42"))
    runner = _runner(adapter)
    calls = []
    real_ledger_call = runner._ledger_call

    async def recording_ledger_call(operation, *args, **kwargs):
        calls.append(operation.__name__)
        return await real_ledger_call(operation, *args, **kwargs)

    runner._ledger_call = recording_ledger_call
    runner._schedule_flood_redelivery(Platform.TELEGRAM, profile="default")
    task = _native_task(runner)
    await _wait_for(lambda: _row()[0] == "delivered")
    await _cancel(task)
    assert {
        "pending_flood_retries", "claim_due_deferred", "mark_delivered",
    } <= set(calls)
