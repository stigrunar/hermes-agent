"""Behavioral proof for bounded Telegram send receipt metadata."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._bot = MagicMock()
    adapter._rich_messages_enabled = False
    return adapter


@pytest.mark.asyncio
async def test_send_reports_returned_primary_message_and_thread() -> None:
    adapter = _adapter()
    adapter._bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=701, message_thread_id=42)
    )

    result = await adapter.send(
        "-100123", "delivered", metadata={"thread_id": "42", "notify": True}
    )

    assert result.success is True
    assert result.message_id == "701"
    assert result.raw_response["requested_thread_id"] == 42
    assert result.raw_response["message_thread_id"] == 42
    assert result.raw_response["message_receipts"] == [
        {"message_id": "701", "message_thread_id": 42}
    ]


@pytest.mark.asyncio
async def test_split_send_reports_bounded_per_message_thread_evidence() -> None:
    adapter = _adapter()
    object.__setattr__(adapter, "MAX_MESSAGE_LENGTH", 80)
    next_message_id = 800

    async def send_message(**_kwargs):
        nonlocal next_message_id
        next_message_id += 1
        return SimpleNamespace(message_id=next_message_id, message_thread_id=77)

    adapter._bot.send_message = AsyncMock(side_effect=send_message)

    result = await adapter.send(
        "-100123",
        "word " * 30,
        metadata={"thread_id": "77", "notify": True},
    )

    assert result.success is True
    assert result.message_id == "801"
    assert len(result.raw_response["message_ids"]) > 1
    assert result.raw_response["message_thread_id"] == 77
    assert result.raw_response["message_receipts"] == [
        {"message_id": message_id, "message_thread_id": 77}
        for message_id in result.raw_response["message_ids"]
    ]


@pytest.mark.asyncio
async def test_plain_fallback_uses_returned_message_evidence() -> None:
    adapter = _adapter()
    adapter._bot.send_message = AsyncMock(
        side_effect=[
            RuntimeError("Markdown parse error"),
            SimpleNamespace(message_id=901, message_thread_id=None),
        ]
    )

    result = await adapter.send("123", "plain fallback", metadata={"notify": True})

    assert result.success is True
    assert result.message_id == "901"
    assert result.raw_response["message_thread_id"] is None
    assert result.raw_response["message_receipts"] == [
        {"message_id": "901", "message_thread_id": None}
    ]
