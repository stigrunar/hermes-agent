"""Telegram-native authentication picker behavior and callback security."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _account(**overrides):
    account = {
        "provider": "openai-codex",
        "provider_label": "OpenAI Codex",
        "label": "Work",
        "fingerprint": "0123456789",
        "plan": "plus",
        "plan_verified": True,
        "identity_verified": True,
        "aliases": [{
            "id": "credential-secret-id",
            "label": "Work",
            "source": "device_code",
            "available": True,
            "priority": 0,
        }],
        "available": True,
        "availability": "available",
        "active": True,
        "target_id": "credential-secret-id",
        "priority": 0,
        "auth_type": "oauth",
        "can_reauthenticate": True,
        "duplicate_count": 1,
    }
    account.update(overrides)
    return account


def _adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    adapter.set_authorization_check(lambda user_id, chat_type, chat_id, **kwargs: True)
    return adapter


def _query(data: str, *, user_id=7, chat_id=42, message_id=101, thread_id=9):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, first_name="Requester"),
        message=SimpleNamespace(
            chat_id=chat_id,
            message_id=message_id,
            message_thread_id=thread_id,
            chat=SimpleNamespace(type="private"),
            text="Authentication accounts",
        ),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )


async def _send_picker(adapter, action):
    sent = {}

    async def _send_message(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(message_id=101)

    adapter._bot.send_message = AsyncMock(side_effect=_send_message)
    result = await adapter.send_auth_picker(
        chat_id="42",
        accounts=[
            _account(),
            _account(
                label="Personal", fingerprint="abcdef0123", plan=None,
                plan_verified=False, active=False, availability="unavailable",
                available=False, duplicate_count=2,
            ),
        ],
        add_providers=[{"provider": "openai-codex", "label": "OpenAI Codex"}],
        session_key="telegram:42:9",
        requester_user_id="7",
        on_auth_action=action,
        metadata={"thread_id": "9"},
    )
    nonce = next(iter(adapter._auth_picker_state))
    return result, sent, nonce


@pytest.mark.asyncio
async def test_root_lists_accounts_without_secrets_and_uses_opaque_callbacks():
    adapter = _adapter()
    result, sent, nonce = await _send_picker(adapter, AsyncMock())

    assert result.success is True
    assert "Work" in sent["text"]
    assert "Personal" in sent["text"]
    assert "OpenAI Codex" in sent["text"]
    assert "acct:0123456789" in sent["text"]
    assert "plus" in sent["text"]
    assert "2 grouped aliases" in sent["text"]
    callback_data = [
        button.callback_data
        for row in sent["reply_markup"].inline_keyboard
        for button in row
    ]
    assert f"ap:{nonce}:o:0" in callback_data
    assert f"ap:{nonce}:a:0" in callback_data
    serialized = sent["text"] + " ".join(callback_data)
    assert "credential-secret-id" not in serialized
    assert "token" not in serialized.lower()


@pytest.mark.asyncio
async def test_callback_rejects_other_requester_and_expired_nonce():
    adapter = _adapter()
    action = AsyncMock()
    _result, _sent, nonce = await _send_picker(adapter, action)

    wrong_user = _query(f"ap:{nonce}:c:0", user_id=8)
    await adapter._handle_callback_query(SimpleNamespace(callback_query=wrong_user), None)
    assert "another private session" in wrong_user.answer.await_args.kwargs["text"]
    action.assert_not_awaited()

    adapter._auth_picker_state[nonce]["created_at"] -= adapter._AUTH_PICKER_TTL_SECONDS + 1
    expired = _query(f"ap:{nonce}:c:0")
    await adapter._handle_callback_query(SimpleNamespace(callback_query=expired), None)
    assert "expired" in expired.answer.await_args.kwargs["text"].lower()
    action.assert_not_awaited()


@pytest.mark.asyncio
async def test_use_requires_confirmation_and_rejects_replay():
    adapter = _adapter()
    action = AsyncMock(return_value="selected")
    _result, _sent, nonce = await _send_picker(adapter, action)

    detail = _query(f"ap:{nonce}:o:0")
    await adapter._handle_callback_query(SimpleNamespace(callback_query=detail), None)
    labels = [
        button.text
        for row in detail.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert labels == ["Use", "Reauthenticate", "◀ Back"]

    request_confirmation = _query(f"ap:{nonce}:u:0")
    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=request_confirmation), None)
    assert "Confirm using" in request_confirmation.edit_message_text.await_args.kwargs["text"]
    action.assert_not_awaited()

    confirmed = _query(f"ap:{nonce}:c:0")
    await adapter._handle_callback_query(SimpleNamespace(callback_query=confirmed), None)
    action.assert_awaited_once_with("use", adapter._auth_picker_state.get(nonce, {}).get("accounts", [
        _account(),
    ])[0])
    assert nonce not in adapter._auth_picker_state

    replay = _query(f"ap:{nonce}:c:0")
    await adapter._handle_callback_query(SimpleNamespace(callback_query=replay), None)
    assert "expired or already used" in replay.answer.await_args.kwargs["text"]
    assert action.await_count == 1
