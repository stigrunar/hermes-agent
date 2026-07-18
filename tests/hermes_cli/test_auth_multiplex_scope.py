"""Profile-owner isolation for auth selection, status, runtime, and login."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import secret_scope as ss


@pytest.fixture(autouse=True)
def _reset_profile_state(monkeypatch, tmp_path):
    """Keep every auth read inside synthetic homes and reset global mode."""
    default_home = tmp_path / "synthetic-default-home"
    default_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setenv("HOME", str(tmp_path / "synthetic-os-home"))
    ss.set_multiplex_active(False)

    from hermes_cli import config as config_mod

    config_mod._LOAD_CONFIG_CACHE.clear()
    yield
    config_mod._LOAD_CONFIG_CACHE.clear()
    ss.set_multiplex_active(False)


def _clear_provider_env(monkeypatch) -> None:
    from hermes_cli.auth import PROVIDER_REGISTRY

    names = {"OPENAI_API_KEY", "OPENROUTER_API_KEY"}
    for provider in PROVIDER_REGISTRY.values():
        names.update(provider.api_key_env_vars)
        if provider.base_url_env_var:
            names.add(provider.base_url_env_var)
    for name in names:
        monkeypatch.delenv(name, raising=False)


def _write_profile(home: Path, *, suffix: str) -> tuple[str, str]:
    home.mkdir(parents=True)
    key = f"synthetic-profile-{suffix}-deepseek-key"
    base_url = f"https://synthetic-profile-{suffix}.invalid/v1"
    (home / ".env").write_text(
        f"DEEPSEEK_API_KEY={key}\nDEEPSEEK_BASE_URL={base_url}\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        "model:\n  provider: auto\n  default: deepseek-chat\n",
        encoding="utf-8",
    )
    return key, base_url


def test_two_profile_homes_ignore_hostile_ambient_selection_and_endpoint(
    monkeypatch, tmp_path
):
    from gateway.run import _profile_runtime_scope
    from hermes_cli.auth import (
        get_api_key_provider_status,
        resolve_api_key_provider_credentials,
        resolve_provider,
    )

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-hostile-openai-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-hostile-deepseek-key")
    monkeypatch.setenv(
        "DEEPSEEK_BASE_URL", "https://synthetic-hostile-endpoint.invalid/v1"
    )
    profiles = []
    for suffix in ("a", "b"):
        home = tmp_path / f"synthetic-profile-{suffix}"
        key, base_url = _write_profile(home, suffix=suffix)
        profiles.append((home, key, base_url))

    ss.set_multiplex_active(True)
    for home, expected_key, expected_url in profiles:
        with _profile_runtime_scope(home):
            assert resolve_provider("auto") == "deepseek"
            status = get_api_key_provider_status("deepseek")
            runtime = resolve_api_key_provider_credentials("deepseek")

        assert status["configured"] is True
        assert status["key_source"] == "DEEPSEEK_API_KEY"
        assert status["base_url"] == expected_url
        assert runtime["api_key"] == expected_key
        assert runtime["base_url"] == expected_url


def test_config_accessors_fail_closed_before_dotenv_or_ambient(monkeypatch):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_env",
        lambda: {"SYNTHETIC_PROFILE_KEY": "synthetic-wrong-dotenv-key"},
    )
    monkeypatch.setenv(
        "SYNTHETIC_PROFILE_KEY", "synthetic-hostile-ambient-key"
    )
    ss.set_multiplex_active(True)

    with pytest.raises(ss.UnscopedSecretError):
        config_mod.get_env_value_prefer_dotenv("SYNTHETIC_PROFILE_KEY")
    with pytest.raises(ss.UnscopedSecretError):
        config_mod.get_env_value("SYNTHETIC_PROFILE_KEY")


def test_fallback_key_env_is_profile_owned(monkeypatch):
    from hermes_cli.fallback_config import resolve_entry_api_key

    entry = {"key_env": "SYNTHETIC_FALLBACK_API_KEY"}
    monkeypatch.setenv(
        "SYNTHETIC_FALLBACK_API_KEY", "synthetic-hostile-fallback-key"
    )
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(
        {"SYNTHETIC_FALLBACK_API_KEY": "synthetic-profile-fallback-key"}
    )
    try:
        assert resolve_entry_api_key(entry) == "synthetic-profile-fallback-key"
    finally:
        ss.reset_secret_scope(token)

    with pytest.raises(ss.UnscopedSecretError):
        resolve_entry_api_key(entry)


def test_setup_provider_detection_ignores_hostile_ambient(monkeypatch):
    from hermes_cli import auth, main

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-hostile-setup-key")
    monkeypatch.setattr(
        auth, "get_auth_status", lambda _provider: {"logged_in": False}
    )
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({})
    try:
        assert main._has_any_provider_configured() is False
    finally:
        ss.reset_secret_scope(token)

    with pytest.raises(ss.UnscopedSecretError):
        main._has_any_provider_configured()


def test_installed_scope_wins_over_dotenv_and_ambient(monkeypatch):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_env",
        lambda: {"SYNTHETIC_PROFILE_KEY": "synthetic-wrong-dotenv-key"},
    )
    monkeypatch.setenv(
        "SYNTHETIC_PROFILE_KEY", "synthetic-hostile-ambient-key"
    )
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(
        {"SYNTHETIC_PROFILE_KEY": "synthetic-owner-profile-key"}
    )
    try:
        assert config_mod.get_env_value_prefer_dotenv(
            "SYNTHETIC_PROFILE_KEY"
        ) == "synthetic-owner-profile-key"
        assert config_mod.get_env_value(
            "SYNTHETIC_PROFILE_KEY"
        ) == "synthetic-owner-profile-key"
    finally:
        ss.reset_secret_scope(token)


def test_single_profile_legacy_precedence_is_preserved(monkeypatch):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_env",
        lambda: {"SYNTHETIC_PROFILE_KEY": "synthetic-dotenv-key"},
    )
    monkeypatch.setenv("SYNTHETIC_PROFILE_KEY", "synthetic-ambient-key")

    assert config_mod.get_env_value_prefer_dotenv(
        "SYNTHETIC_PROFILE_KEY"
    ) == "synthetic-dotenv-key"
    assert config_mod.get_env_value(
        "SYNTHETIC_PROFILE_KEY"
    ) == "synthetic-ambient-key"


def test_unscoped_status_and_provider_selection_propagate_boundary(monkeypatch):
    from hermes_cli.auth import _get_azure_foundry_auth_status, resolve_provider

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-hostile-openai-key")
    monkeypatch.setenv(
        "AZURE_FOUNDRY_API_KEY", "synthetic-hostile-azure-key"
    )
    ss.set_multiplex_active(True)

    with pytest.raises(ss.UnscopedSecretError):
        resolve_provider("auto")
    with pytest.raises(ss.UnscopedSecretError):
        _get_azure_foundry_auth_status()


def test_scoped_empty_azure_status_rejects_hostile_ambient(monkeypatch):
    from hermes_cli.auth import _get_azure_foundry_auth_status

    monkeypatch.setenv(
        "AZURE_FOUNDRY_API_KEY", "synthetic-hostile-azure-key"
    )
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({})
    try:
        status = _get_azure_foundry_auth_status()
    finally:
        ss.reset_secret_scope(token)

    assert status["logged_in"] is False


def test_runtime_endpoints_are_profile_owned_except_explicit_portal_global(
    monkeypatch,
):
    from hermes_cli import auth

    monkeypatch.setenv(
        "HERMES_CODEX_BASE_URL",
        "https://synthetic-hostile-codex.invalid/v1",
    )
    monkeypatch.setenv(
        "HERMES_XAI_BASE_URL",
        "https://synthetic-hostile.proxy.x.ai/v1",
    )
    monkeypatch.setenv(
        "NOUS_INFERENCE_BASE_URL",
        "https://synthetic-hostile-nous.invalid/v1",
    )
    monkeypatch.setenv(
        "HERMES_PORTAL_BASE_URL",
        "https://synthetic-deployment-portal.invalid",
    )
    monkeypatch.setenv(
        "NOUS_PORTAL_BASE_URL",
        "https://synthetic-hostile-profile-portal.invalid",
    )
    monkeypatch.setattr(
        auth,
        "_read_codex_tokens",
        lambda **_kwargs: {
            "tokens": {"access_token": "synthetic-codex-access"},
            "last_refresh": None,
        },
    )
    monkeypatch.setattr(
        auth,
        "_read_xai_oauth_tokens",
        lambda **_kwargs: {
            "tokens": {"access_token": "synthetic-xai-access"},
            "last_refresh": None,
            "discovery": {},
            "redirect_uri": "",
        },
    )

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(
        {
            "HERMES_CODEX_BASE_URL": "https://synthetic-profile-codex.invalid/v1",
            "HERMES_XAI_BASE_URL": "https://synthetic-profile.proxy.x.ai/v1",
            "NOUS_INFERENCE_BASE_URL": "https://synthetic-profile-nous.invalid/v1",
            "NOUS_PORTAL_BASE_URL": "https://synthetic-profile-portal.invalid",
        }
    )
    try:
        codex = auth.resolve_codex_runtime_credentials(refresh_if_expiring=False)
        xai = auth.resolve_xai_oauth_runtime_credentials(refresh_if_expiring=False)
        nous_inference = auth._nous_inference_env_override()
        nous_portal = auth._nous_portal_env_override()
    finally:
        ss.reset_secret_scope(token)

    assert codex["base_url"] == "https://synthetic-profile-codex.invalid/v1"
    assert xai["base_url"] == "https://synthetic-profile.proxy.x.ai/v1"
    assert nous_inference == "https://synthetic-profile-nous.invalid/v1"
    assert nous_portal == "https://synthetic-deployment-portal.invalid"


def test_nous_login_uses_profile_endpoints_without_network(monkeypatch):
    from hermes_cli import auth

    class SyntheticClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    observed = {}

    monkeypatch.delenv("HERMES_PORTAL_BASE_URL", raising=False)
    monkeypatch.setenv(
        "NOUS_PORTAL_BASE_URL", "https://synthetic-hostile-portal.invalid"
    )
    monkeypatch.setenv(
        "NOUS_INFERENCE_BASE_URL", "https://synthetic-hostile-nous.invalid/v1"
    )
    monkeypatch.setattr(auth.httpx, "Client", lambda **_kwargs: SyntheticClient())
    monkeypatch.setattr(
        auth,
        "_request_device_code",
        lambda **kwargs: observed.update(portal=kwargs["portal_base_url"])
        or {
            "device_code": "synthetic-device-code",
            "user_code": "synthetic-user-code",
            "verification_uri_complete": "https://synthetic-verify.invalid",
            "expires_in": 60,
            "interval": 1,
        },
    )
    monkeypatch.setattr(
        auth,
        "_poll_for_token",
        lambda **_kwargs: {
            "access_token": "synthetic-nous-access",
            "refresh_token": "synthetic-nous-refresh",
            "expires_in": 3600,
            "scope": auth.DEFAULT_NOUS_SCOPE,
        },
    )
    monkeypatch.setattr(
        auth,
        "refresh_nous_oauth_from_state",
        lambda state, **_kwargs: state,
    )

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(
        {
            "NOUS_PORTAL_BASE_URL": "https://synthetic-profile-portal.invalid",
            "NOUS_INFERENCE_BASE_URL": "https://synthetic-profile-nous.invalid/v1",
        }
    )
    try:
        state = auth._nous_device_code_login(open_browser=False)
    finally:
        ss.reset_secret_scope(token)

    assert observed["portal"] == "https://synthetic-profile-portal.invalid"
    assert state["portal_base_url"] == "https://synthetic-profile-portal.invalid"
    assert state["inference_base_url"] == (
        "https://synthetic-profile-nous.invalid/v1"
    )


def test_anthropic_and_copilot_env_credentials_are_profile_owned(monkeypatch):
    from agent import anthropic_adapter
    from hermes_cli import copilot_auth

    monkeypatch.setenv("ANTHROPIC_TOKEN", "synthetic-hostile-anthropic-token")
    monkeypatch.setenv(
        "COPILOT_GITHUB_TOKEN", "synthetic-hostile-copilot-token"
    )
    monkeypatch.setattr(anthropic_adapter, "read_claude_code_credentials", lambda: {})
    monkeypatch.setattr(
        anthropic_adapter,
        "_resolve_claude_code_token_from_credentials",
        lambda _creds: None,
    )
    monkeypatch.setattr(
        anthropic_adapter, "_resolve_anthropic_pool_token", lambda: None
    )

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(
        {
            "ANTHROPIC_TOKEN": "synthetic-profile-anthropic-token",
            "COPILOT_GITHUB_TOKEN": "synthetic-profile-copilot-token",
        }
    )
    try:
        anthropic = anthropic_adapter.resolve_anthropic_token()
        copilot, source = copilot_auth.resolve_copilot_token()
    finally:
        ss.reset_secret_scope(token)

    assert anthropic == "synthetic-profile-anthropic-token"
    assert copilot == "synthetic-profile-copilot-token"
    assert source == "COPILOT_GITHUB_TOKEN"


def test_copilot_cli_host_override_is_profile_owned(monkeypatch):
    from types import SimpleNamespace

    from hermes_cli import copilot_auth

    observed = {}
    monkeypatch.setenv("COPILOT_GH_HOST", "synthetic-hostile-github.invalid")
    for name in copilot_auth.COPILOT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        copilot_auth, "_gh_cli_candidates", lambda: ["synthetic-gh"]
    )

    def _run(command, **_kwargs):
        observed["command"] = command
        return SimpleNamespace(
            returncode=0,
            stdout="synthetic-profile-copilot-cli-token",
        )

    monkeypatch.setattr(copilot_auth.subprocess, "run", _run)
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(
        {"COPILOT_GH_HOST": "synthetic-profile-github.invalid"}
    )
    try:
        resolved, source = copilot_auth.resolve_copilot_token()
    finally:
        ss.reset_secret_scope(token)

    assert observed["command"] == [
        "synthetic-gh",
        "auth",
        "token",
        "--hostname",
        "synthetic-profile-github.invalid",
    ]
    assert resolved == "synthetic-profile-copilot-cli-token"
    assert source == "gh auth token"


def test_anthropic_setup_token_postcheck_uses_profile_scope(monkeypatch):
    import shutil

    from agent import anthropic_adapter

    monkeypatch.setenv(
        "ANTHROPIC_TOKEN", "synthetic-hostile-anthropic-setup-token"
    )
    monkeypatch.setattr(shutil, "which", lambda _name: "claude")
    monkeypatch.setattr(anthropic_adapter.subprocess, "run", lambda *_a, **_k: None)
    monkeypatch.setattr(
        anthropic_adapter, "read_claude_code_credentials", lambda: None
    )

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(
        {"ANTHROPIC_TOKEN": "synthetic-profile-anthropic-setup-token"}
    )
    try:
        resolved = anthropic_adapter.run_oauth_setup_token()
    finally:
        ss.reset_secret_scope(token)

    assert resolved == "synthetic-profile-anthropic-setup-token"


def test_tui_provider_and_config_status_are_profile_owned(monkeypatch):
    from tui_gateway import server

    monkeypatch.setenv("HERMES_TUI_PROVIDER", "synthetic-hostile-provider")
    monkeypatch.setenv("HERMES_MODEL", "synthetic-hostile-model")
    monkeypatch.setenv("HERMES_API_KEY", "synthetic-hostile-tui-api-key")
    monkeypatch.setenv(
        "HERMES_BASE_URL", "https://synthetic-hostile-tui.invalid/v1"
    )
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "api_key": "synthetic-config-fallback-key",
            "base_url": "https://synthetic-config-fallback.invalid/v1",
            "enabled_toolsets": [],
        },
    )

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(
        {
            "HERMES_TUI_PROVIDER": "synthetic-profile-provider",
            "HERMES_MODEL": "synthetic-profile-model",
            "HERMES_API_KEY": "synthetic-profile-tui-api-key",
            "HERMES_BASE_URL": "https://synthetic-profile-tui.invalid/v1",
        }
    )
    try:
        startup = server._resolve_startup_runtime()
        response = server._methods["config.show"]("synthetic-rid", {})
    finally:
        ss.reset_secret_scope(token)

    model_rows = response["result"]["sections"][0]["rows"]
    assert startup == ("synthetic-profile-model", "synthetic-profile-provider")
    assert ["Base URL", "https://synthetic-profile-tui.invalid/v1"] in model_rows
    assert ["API Key", "****-key"] in model_rows

    with pytest.raises(ss.UnscopedSecretError):
        server._resolve_startup_runtime()


def test_xai_tool_discovery_ignores_hostile_ambient_key(monkeypatch):
    from hermes_cli import auth, tools_config

    monkeypatch.setenv("XAI_API_KEY", "synthetic-hostile-xai-tool-key")
    monkeypatch.setattr(
        auth,
        "_read_xai_oauth_tokens",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic-empty")),
    )
    ss.set_multiplex_active(True)

    token = ss.set_secret_scope({})
    try:
        assert tools_config._xai_credentials_present() is False
    finally:
        ss.reset_secret_scope(token)

    token = ss.set_secret_scope(
        {"XAI_API_KEY": "synthetic-profile-xai-tool-key"}
    )
    try:
        assert tools_config._xai_credentials_present() is True
    finally:
        ss.reset_secret_scope(token)

    with pytest.raises(ss.UnscopedSecretError):
        tools_config._xai_credentials_present()


def test_nous_billing_cache_never_crosses_profile_owner(monkeypatch, tmp_path):
    import time

    from gateway.run import _profile_runtime_scope
    from hermes_cli import auth, nous_billing

    profiles = []
    for suffix in ("a", "b"):
        home = tmp_path / f"synthetic-billing-profile-{suffix}"
        home.mkdir()
        token = f"synthetic-profile-{suffix}-billing-token"
        portal = f"https://synthetic-profile-{suffix}-portal.invalid"
        (home / ".env").write_text(
            f"SYNTHETIC_NOUS_ACCESS_TOKEN={token}\n"
            f"NOUS_PORTAL_BASE_URL={portal}\n",
            encoding="utf-8",
        )
        profiles.append((home, token, portal))

    monkeypatch.delenv("HERMES_PORTAL_BASE_URL", raising=False)
    monkeypatch.setenv(
        "NOUS_PORTAL_BASE_URL", "https://synthetic-hostile-portal.invalid"
    )
    monkeypatch.setattr(
        auth, "get_provider_auth_state", lambda _provider: {}
    )
    monkeypatch.setattr(
        auth,
        "resolve_nous_access_token",
        lambda: ss.get_secret("SYNTHETIC_NOUS_ACCESS_TOKEN", ""),
    )
    monkeypatch.setattr(
        nous_billing,
        "_token_cache",
        (
            time.time(),
            "synthetic-hostile-cached-token",
            "https://synthetic-hostile-cached.invalid",
        ),
    )

    ss.set_multiplex_active(True)
    for home, expected_token, expected_portal in profiles:
        with _profile_runtime_scope(home):
            assert nous_billing._resolve_token_and_base() == (
                expected_token,
                expected_portal,
            )

    with pytest.raises(ss.UnscopedSecretError):
        nous_billing._resolve_token_and_base()


def test_nous_tier_cache_never_crosses_profile_owner(monkeypatch, tmp_path):
    import time
    from types import SimpleNamespace

    from gateway.run import _profile_runtime_scope
    from hermes_cli import models, nous_account

    profiles = []
    for suffix, is_free in (("a", True), ("b", False)):
        home = tmp_path / f"synthetic-tier-profile-{suffix}"
        home.mkdir()
        (home / ".env").write_text(
            f"SYNTHETIC_NOUS_FREE_TIER={int(is_free)}\n",
            encoding="utf-8",
        )
        profiles.append((home, is_free))

    monkeypatch.setattr(
        nous_account,
        "get_nous_portal_account_info",
        lambda **_kwargs: SimpleNamespace(
            is_free_tier=ss.get_secret("SYNTHETIC_NOUS_FREE_TIER") == "1"
        ),
    )
    monkeypatch.setattr(
        models, "_free_tier_cache", (True, time.monotonic())
    )

    ss.set_multiplex_active(True)
    for home, expected in profiles:
        with _profile_runtime_scope(home):
            assert models.check_nous_free_tier() is expected

    with pytest.raises(ss.UnscopedSecretError):
        models.check_nous_free_tier()
