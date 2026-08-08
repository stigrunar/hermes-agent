from hermes_state import AsyncSessionDB
"""Tests for gateway /usage command — agent cache lookup and output fields."""

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_agent(**overrides):
    """Create a mock AIAgent with realistic session counters."""
    agent = MagicMock()
    defaults = {
        "model": "anthropic/claude-sonnet-4.6",
        "provider": "openrouter",
        "base_url": None,
        "session_total_tokens": 50_000,
        "session_api_calls": 5,
        "session_prompt_tokens": 40_000,
        "session_completion_tokens": 10_000,
        "session_input_tokens": 35_000,
        "session_output_tokens": 10_000,
        "session_cache_read_tokens": 5_000,
        "session_cache_write_tokens": 2_000,
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(agent, k, v)

    # Rate limit state
    rl = MagicMock()
    rl.has_data = True
    agent.get_rate_limit_state.return_value = rl

    # Context compressor
    ctx = MagicMock()
    ctx.last_prompt_tokens = 30_000
    ctx.context_length = 200_000
    ctx.compression_count = 1
    agent.context_compressor = ctx

    return agent


def _make_runner(session_key, agent=None, cached_agent=None):
    """Build a bare GatewayRunner with just the fields _handle_usage_command needs."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner.session_store = MagicMock()

    if agent is not None:
        runner._running_agents[session_key] = agent

    if cached_agent is not None:
        runner._agent_cache[session_key] = (cached_agent, "sig")

    # Wire helper
    runner._session_key_for_source = MagicMock(return_value=session_key)

    return runner


SK = "agent:main:telegram:private:12345"


class TestUsageCachedAgent:
    """The main fix: /usage should find agents in _agent_cache between turns."""

    @pytest.mark.asyncio
    async def test_cached_agent_shows_detailed_usage(self):
        agent = _make_mock_agent()
        runner = _make_runner(SK, cached_agent=agent)
        event = MagicMock()

        with patch("agent.rate_limit_tracker.format_rate_limit_compact", return_value="RPM: 50/60"):
            result = await runner._handle_usage_command(event)

        assert "claude-sonnet-4.6" in result
        assert "35,000" in result  # input tokens
        assert "10,000" in result  # output tokens
        assert "50,000" in result  # total
        assert "30,000" in result  # context
        assert "Compressions: 1" in result
        # Cost and cache-hit reporting is removed everywhere.
        assert "$" not in result
        assert "Cache read" not in result
        assert "Cache write" not in result
        assert "Cost" not in result

    @pytest.mark.asyncio
    async def test_running_agent_preferred_over_cache(self):
        """When agent is in both dicts, the running one wins."""
        running = _make_mock_agent(session_api_calls=10, session_total_tokens=80_000)
        cached = _make_mock_agent(session_api_calls=5, session_total_tokens=50_000)
        runner = _make_runner(SK, agent=running, cached_agent=cached)
        event = MagicMock()

        with patch("agent.rate_limit_tracker.format_rate_limit_compact", return_value="RPM: 50/60"), \
             patch("agent.usage_pricing.estimate_usage_cost") as mock_cost:
            mock_cost.return_value = MagicMock(amount_usd=None, status="unknown")
            result = await runner._handle_usage_command(event)

        assert "80,000" in result   # running agent's total
        assert "API calls: 10" in result


class TestUsageAccountSection:
    """Account-limits section appended to /usage output (PR #2486)."""


    @pytest.mark.asyncio
    async def test_usage_command_uses_persisted_provider_when_agent_not_running(self, monkeypatch):
        runner = _make_runner(SK)
        runner._session_db = AsyncSessionDB(MagicMock())
        runner._session_db._db.get_session.return_value = {
            "billing_provider": "openai-codex",
            "billing_base_url": "https://chatgpt.com/backend-api/codex",
        }
        session_entry = MagicMock()
        session_entry.session_id = "sess-1"
        runner.session_store.get_or_create_session.return_value = session_entry
        runner.session_store.load_transcript.return_value = [
            {"role": "user", "content": "earlier"},
        ]

        calls = []

        async def _fake_to_thread(fn, *args, **kwargs):
            # /usage dispatches BOTH the account fetch (fetch_account_usage, called
            # with the provider positionally) and the Nous credits fetch
            # (nous_credits_lines, markdown-only) through to_thread — record every
            # call rather than last-wins so we can pick out the account fetch.
            calls.append({"args": args, "kwargs": kwargs})
            return fn(*args, **kwargs)

        monkeypatch.setattr("gateway.run.asyncio.to_thread", _fake_to_thread)
        monkeypatch.setattr(
            "gateway.slash_commands.fetch_account_usage",
            lambda provider, base_url=None, api_key=None: object(),
        )
        monkeypatch.setattr(
            "gateway.slash_commands.render_account_usage_lines",
            lambda snapshot, markdown=False: [
                "📈 **Account limits**",
                "Provider: openai-codex (Pro)",
            ],
        )
        # The credits block routes through the shared nous_credits_lines() helper;
        # stub it so this account-section test stays hermetic (no portal/auth lookup).
        monkeypatch.setattr("agent.account_usage.nous_credits_lines", lambda markdown=False: [])

        event = MagicMock()
        result = await runner._handle_usage_command(event)

        account_call = next(c for c in calls if c["args"] == ("openai-codex",))
        assert account_call["kwargs"]["base_url"] == "https://chatgpt.com/backend-api/codex"
        assert "📊 **Session Info**" in result
        assert "📈 **Account limits**" in result


class TestUsageReset:
    """`/usage reset [--force]` — banked Codex reset redemption via the gateway."""

    def _event(self, args):
        event = MagicMock()
        event.get_command_args.return_value = args
        return event

    @pytest.mark.asyncio
    async def test_reset_dispatches_redeem_for_codex_agent(self, monkeypatch):
        agent = _make_mock_agent(provider="openai-codex",
                                 base_url="https://chatgpt.com/backend-api/codex",
                                 api_key="tok")
        runner = _make_runner(SK, cached_agent=agent)

        seen = {}

        def fake_redeem(*, base_url=None, api_key=None, force=False):
            seen.update(base_url=base_url, api_key=api_key, force=force)
            from agent.account_usage import CodexResetRedeemResult
            return CodexResetRedeemResult(status="reset", message="✅ redeemed", available_count=1)

        monkeypatch.setattr("agent.account_usage.redeem_codex_reset_credit", fake_redeem)

        result = await runner._handle_usage_command(self._event("reset"))

        assert result == "✅ redeemed"
        assert seen["force"] is False
        assert seen["api_key"] == "tok"


class TestUsageContextBreakdown:
    """The /usage output includes the per-category context breakdown."""

    @pytest.mark.asyncio
    async def test_breakdown_lines_rendered_for_live_agent(self):
        agent = _make_mock_agent()
        runner = _make_runner(SK, cached_agent=agent)
        session_entry = MagicMock()
        session_entry.session_id = "sess-bd"
        runner.session_store.get_or_create_session.return_value = session_entry
        runner.session_store.load_transcript.return_value = [
            {"role": "user", "content": "hi"},
        ]
        event = MagicMock()

        fake_payload = {
            "categories": [
                {"id": "system_prompt", "label": "System prompt", "tokens": 4000, "color": "x"},
                {"id": "tool_definitions", "label": "Tool definitions", "tokens": 6000, "color": "x"},
                {"id": "conversation", "label": "Conversation", "tokens": 0, "color": "x"},
            ],
            "estimated_total": 10000,
            "context_max": 200000,
            "context_percent": 5,
            "context_used": 30000,
            "model": "anthropic/claude-sonnet-4.6",
        }

        with patch("agent.rate_limit_tracker.format_rate_limit_compact", return_value="RPM: 50/60"), \
             patch("agent.context_breakdown.compute_session_context_breakdown", return_value=fake_payload):
            result = await runner._handle_usage_command(event)

        # Localized header + at least the two non-zero category labels appear,
        # each labelled as a percentage of the estimated total.
        assert "Context breakdown" in result
        assert "System prompt" in result
        assert "Tool definitions" in result
        assert "4,000" in result   # system prompt tokens, comma-formatted
        assert "40%" in result     # 4000 / 10000
        assert "60%" in result     # 6000 / 10000
        # Zero-token category is dropped, not rendered.
        assert "Conversation" not in result


class TestTelegramAuthCommand:
    def _event(self, args=""):
        from gateway.config import Platform

        event = MagicMock()
        event.get_command_args.return_value = args
        event.source.platform = Platform.TELEGRAM
        event.source.chat_type = "dm"
        event.source.chat_id = "12345"
        return event

    def _runner(self):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=None)
        runner._adapter_for_source = MagicMock(return_value=adapter)
        runner._thread_metadata_for_source = MagicMock(return_value={})
        return runner, adapter

    @pytest.mark.asyncio
    async def test_auth_list_is_registered_and_never_exposes_tokens(self, monkeypatch):
        from hermes_cli.commands import is_gateway_known_command

        assert is_gateway_known_command("auth")
        runner, _adapter = self._runner()
        entry = MagicMock()
        entry.id = "abc123"
        entry.label = "private-pro-fallback"
        entry.auth_type = "oauth"
        entry.source = "manual:device_code"
        entry.last_status = None
        entry.access_token = "SECRET_ACCESS_TOKEN"
        pool = MagicMock()
        pool.entries.return_value = [entry]
        pool.peek.return_value = entry
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)
        monkeypatch.setattr(
            "hermes_cli.auth.read_credential_pool",
            lambda provider=None: {"openai-codex": [{"access_token": "SECRET_ACCESS_TOKEN"}]},
        )

        result = await runner._handle_auth_command(self._event("list openai-codex"))

        assert "private-pro-fallback" in result
        assert "SECRET_ACCESS_TOKEN" not in result

    @pytest.mark.asyncio
    async def test_auth_rejects_non_dm_surface(self):
        from gateway.config import Platform

        runner, _adapter = self._runner()
        event = self._event("list")
        event.source.chat_type = "group"
        result = await runner._handle_auth_command(event)
        assert "only" in result.lower()
        assert "Telegram DM" in result

    @pytest.mark.asyncio
    async def test_auth_reset_clears_local_pool_status(self, monkeypatch):
        runner, _adapter = self._runner()
        pool = MagicMock()
        pool.reset_statuses.return_value = 3
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

        result = await runner._handle_auth_command(self._event("reset openai-codex"))

        assert result == "Reset local cooldown/status on 3 openai-codex credential(s)."
        pool.reset_statuses.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_auth_add_codex_delivers_device_code_and_persists_without_token_echo(self, monkeypatch):
        from agent.credential_pool import PooledCredential

        runner, adapter = self._runner()
        pool = MagicMock()
        pool.entries.side_effect = [[], []]
        pool.resolve_target.return_value = (None, None, "not found")

        added_entry = None

        def add_entry(entry):
            nonlocal added_entry
            added_entry = entry
            return entry

        pool.add_entry.side_effect = add_entry
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)
        monkeypatch.setattr("hermes_cli.auth.mark_provider_active_if_unset", lambda provider: None)

        def fake_login(*, device_code_callback=None):
            assert device_code_callback is not None
            device_code_callback("https://auth.openai.com/codex/device", "ABCD-EFGH")
            return {
                "tokens": {"access_token": "SECRET_ACCESS_TOKEN", "refresh_token": "SECRET_REFRESH_TOKEN"},
                "base_url": "https://chatgpt.com/backend-api/codex",
                "last_refresh": 123.0,
            }

        monkeypatch.setattr("hermes_cli.auth._codex_device_code_login", fake_login)

        result = await runner._handle_auth_command(
            self._event("add openai-codex --label new-pro")
        )

        assert "new-pro" in result
        assert "SECRET_ACCESS_TOKEN" not in result
        assert isinstance(added_entry, PooledCredential)
        assert added_entry.access_token == "SECRET_ACCESS_TOKEN"
        sent = adapter.send.await_args.args[1]
        assert "ABCD-EFGH" in sent
        assert "SECRET_ACCESS_TOKEN" not in sent

    @pytest.mark.asyncio
    async def test_auth_reauth_replaces_tokens_and_clears_stale_cooldown(self, monkeypatch):
        from agent.credential_pool import PooledCredential

        runner, _adapter = self._runner()
        existing = PooledCredential(
            provider="openai-codex",
            id="abc123",
            label="private-pro-fallback",
            auth_type="oauth",
            priority=1,
            source="manual:device_code",
            access_token="OLD_TOKEN",
            refresh_token="OLD_REFRESH",
            last_status="exhausted",
            last_status_at=123.0,
            last_error_code=429,
            last_error_reason="rate_limit",
        )
        pool = MagicMock()
        pool.resolve_target.return_value = (2, existing, None)
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

        def fake_login(*, device_code_callback=None):
            device_code_callback("https://auth.openai.com/codex/device", "ZXCV-1234")
            return {
                "tokens": {"access_token": "NEW_TOKEN", "refresh_token": "NEW_REFRESH"},
                "base_url": "https://chatgpt.com/backend-api/codex",
                "last_refresh": 456.0,
            }

        monkeypatch.setattr("hermes_cli.auth._codex_device_code_login", fake_login)

        result = await runner._handle_auth_command(
            self._event("reauth openai-codex private-pro-fallback")
        )

        updated = pool._replace_entry.call_args.args[1]
        assert updated.access_token == "NEW_TOKEN"
        assert updated.refresh_token == "NEW_REFRESH"
        assert updated.last_status is None
        assert updated.last_error_code is None
        assert "private-pro-fallback" in result
        pool._persist.assert_called_once_with()

