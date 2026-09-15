"""Gateway `/auth` registry, privacy, picker, and native-action behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class _PickerAdapter:
    def __init__(self):
        self.calls = []

    async def send_auth_picker(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(success=True)


def _event(text="/auth", *, chat_type="dm", user_id="7"):
    return MessageEvent(
        text=text,
        message_id="message-1",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-1",
            chat_type=chat_type,
            user_id=user_id,
            thread_id="topic-1" if chat_type == "dm" else None,
        ),
    )


def _runner(adapter=None, *, config=None):
    runner = object.__new__(GatewayRunner)
    runner.config = config or GatewayConfig()
    runner.adapters = {Platform.TELEGRAM: adapter} if adapter else {}
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._adapter_for_source = lambda source: adapter
    runner._session_key_for_source = lambda source: "telegram:chat-1:topic-1"
    runner._reply_metadata = lambda event: {"thread_id": "topic-1"}
    return runner


def _payload():
    return {
        "accounts": [{
            "provider": "openai-codex",
            "provider_label": "OpenAI Codex",
            "label": "Work",
            "fingerprint": "0123456789",
            "available": True,
            "availability": "available",
            "active": True,
            "target_id": "credential-id",
            "aliases": [],
            "duplicate_count": 1,
            "can_reauthenticate": True,
        }],
        "add_providers": [{"provider": "openai-codex", "label": "OpenAI Codex"}],
        "active_provider": "openai-codex",
    }


def test_registry_help_and_idle_dispatch_expose_auth():
    from hermes_cli.commands import COMMANDS, SUBCOMMANDS, is_gateway_known_command, resolve_command

    command = resolve_command("auth")
    assert command is not None
    assert command.busy_policy == "reject"
    assert is_gateway_known_command("auth") is True
    assert "/auth" in COMMANDS
    assert SUBCOMMANDS["/auth"] == ["status", "use", "reauth", "add"]
    assert _runner()._gateway_idle_command_handlers()["auth"].__func__ is GatewayRunner._handle_auth_command


@pytest.mark.asyncio
async def test_bare_telegram_auth_opens_native_picker_without_model_path():
    adapter = _PickerAdapter()
    runner = _runner(adapter)
    runner._auth_picker_payload = AsyncMock(return_value=_payload())

    result = await runner._handle_auth_command(_event())

    assert result is None
    runner._auth_picker_payload.assert_awaited_once()
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call["requester_user_id"] == "7"
    assert call["session_key"] == "telegram:chat-1:topic-1"
    assert call["can_mutate"] is True
    assert call["accounts"] == _payload()["accounts"]


@pytest.mark.asyncio
async def test_group_auth_returns_private_handoff_without_read_or_mutation():
    runner = _runner(_PickerAdapter())
    runner._auth_picker_payload = AsyncMock()
    runner._run_auth_native_action = AsyncMock()

    result = await runner._handle_auth_command(_event("/auth add openai-codex", chat_type="group"))

    assert "direct chat" in result
    runner._auth_picker_payload.assert_not_awaited()
    runner._run_auth_native_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_admin_dm_can_view_status_but_cannot_mutate():
    adapter = _PickerAdapter()
    config = GatewayConfig(platforms={
        Platform.TELEGRAM: PlatformConfig(
            enabled=True, token="test", extra={"allow_admin_from": ["admin"]}),
    })
    runner = _runner(adapter, config=config)
    runner._auth_picker_payload = AsyncMock(return_value=_payload())
    runner._run_auth_native_action = AsyncMock()

    status = await runner._handle_auth_command(_event("/auth status"))
    denied = await runner._handle_auth_command(_event("/auth add openai-codex"))

    assert "Authentication accounts" in status
    assert "Only a gateway admin" in denied
    runner._run_auth_native_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_typed_use_requires_confirmation_before_native_action():
    runner = _runner()
    runner._run_auth_native_action = AsyncMock(return_value="selected")
    confirmation = {}

    async def _request(**kwargs):
        confirmation.update(kwargs)
        runner._run_auth_native_action.assert_not_awaited()
        return "confirmation pending"

    runner._request_slash_confirm = _request
    result = await runner._handle_auth_command(
        _event("/auth use openai-codex credential-id"))

    assert result == "confirmation pending"
    runner._run_auth_native_action.assert_not_awaited()
    assert "credential-pool priority" in confirmation["message"]

    applied = await confirmation["handler"]("once")
    assert applied == "selected"
    runner._run_auth_native_action.assert_awaited_once_with(
        confirmation["event"], "use",
        {"provider": "openai-codex", "target_id": "credential-id"},
    )


@pytest.mark.asyncio
async def test_native_use_evicts_idle_provider_agents_and_preserves_running_agent(monkeypatch):
    runner = _runner()
    idle_agent = SimpleNamespace(provider="openai-codex", _credential_pool=None)
    running_agent = SimpleNamespace(provider="openai-codex", _credential_pool=None)
    runner._agent_cache = {"idle": idle_agent, "running": running_agent}
    runner._running_agent_ids = lambda: {id(running_agent)}
    evicted = []
    runner._evict_cached_agent = lambda key: (evicted.append(key), runner._agent_cache.pop(key, None))
    monkeypatch.setattr(
        "hermes_cli.auth_commands.use_auth_account",
        lambda provider, target: {
            "provider": "openai-codex",
            "label": "Work",
            "fingerprint": "0123456789",
            "credential_id": target,
        },
    )

    result = await runner._run_auth_native_action(
        _event(), "use", {"provider": "grok-oauth", "target_id": "credential-id"})

    assert evicted == ["idle"]
    assert "Invalidated 1 idle cached agent" in result
    assert "1 in-flight session" in result
    assert "No gateway restart is required" in result
