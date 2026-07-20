"""Network-free regressions for confirmed profile credential crossings."""

from agent.secret_scope import reset_secret_scope, set_multiplex_active, set_secret_scope


def test_homeassistant_empty_scope_ignores_ambient_endpoint_and_token(monkeypatch):
    from tools import homeassistant_tool as ha

    monkeypatch.setenv("HASS_URL", "https://ambient.invalid")
    monkeypatch.setenv("HASS_TOKEN", "ambient-token")
    monkeypatch.setattr(ha, "_HASS_URL", "https://legacy.invalid")
    monkeypatch.setattr(ha, "_HASS_TOKEN", "legacy-token")
    set_multiplex_active(True)
    scope_token = set_secret_scope({})
    try:
        assert ha._check_ha_available() is False
        url, token = ha._get_config()
        assert url == "http://homeassistant.local:8123"
        assert token == ""
    finally:
        reset_secret_scope(scope_token)
        set_multiplex_active(False)


def test_homeassistant_dual_scopes_do_not_cross(monkeypatch):
    from tools import homeassistant_tool as ha

    monkeypatch.setattr(ha, "_HASS_URL", "https://legacy.invalid")
    monkeypatch.setattr(ha, "_HASS_TOKEN", "legacy-token")
    set_multiplex_active(True)
    a = set_secret_scope({"HASS_URL": "https://a.invalid", "HASS_TOKEN": "a-token"})
    try:
        assert ha._get_config() == ("https://a.invalid", "a-token")
    finally:
        reset_secret_scope(a)
    b = set_secret_scope({"HASS_URL": "https://b.invalid", "HASS_TOKEN": "b-token"})
    try:
        assert ha._get_config() == ("https://b.invalid", "b-token")
    finally:
        reset_secret_scope(b)
        set_multiplex_active(False)


def test_auxiliary_cache_key_is_profile_bound(monkeypatch):
    from agent import auxiliary_client as aux

    set_multiplex_active(True)
    a = set_secret_scope({"ANTHROPIC_API_KEY": "profile-a"})
    try:
        key_a = aux._client_cache_key("anthropic", async_mode=False)
    finally:
        reset_secret_scope(a)
    b = set_secret_scope({"ANTHROPIC_API_KEY": "profile-b"})
    try:
        key_b = aux._client_cache_key("anthropic", async_mode=False)
    finally:
        reset_secret_scope(b)
        set_multiplex_active(False)
    assert key_a != key_b


def test_anthropic_empty_scope_does_not_use_host_claude_code(monkeypatch):
    from agent import anthropic_adapter as adapter

    monkeypatch.setenv("ANTHROPIC_TOKEN", "")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(
        adapter,
        "read_claude_code_credentials",
        lambda: {"accessToken": "host-token", "refreshToken": "host-refresh"},
    )
    monkeypatch.setattr(adapter, "_resolve_anthropic_pool_token", lambda: None)
    set_multiplex_active(True)
    scope_token = set_secret_scope({})
    try:
        assert adapter.resolve_anthropic_token() is None
    finally:
        reset_secret_scope(scope_token)
        set_multiplex_active(False)


def test_anthropic_dual_scopes_do_not_cross(monkeypatch):
    from agent import anthropic_adapter as adapter

    monkeypatch.setattr(adapter, "read_claude_code_credentials", lambda: {"accessToken": "host"})
    monkeypatch.setattr(adapter, "_resolve_anthropic_pool_token", lambda: None)
    set_multiplex_active(True)
    a = set_secret_scope({"ANTHROPIC_API_KEY": "profile-a"})
    try:
        assert adapter.resolve_anthropic_token() == "profile-a"
    finally:
        reset_secret_scope(a)
    b = set_secret_scope({"ANTHROPIC_API_KEY": "profile-b"})
    try:
        assert adapter.resolve_anthropic_token() == "profile-b"
    finally:
        reset_secret_scope(b)
        set_multiplex_active(False)


def test_copilot_empty_scope_does_not_use_host_gh(monkeypatch):
    from hermes_cli import copilot_auth

    monkeypatch.setattr(copilot_auth, "_try_gh_cli_token", lambda: "host-gh-token")
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    set_multiplex_active(True)
    scope_token = set_secret_scope({})
    try:
        assert copilot_auth.resolve_copilot_token() == ("", "")
    finally:
        reset_secret_scope(scope_token)
        set_multiplex_active(False)


def test_copilot_dual_scopes_do_not_cross(monkeypatch):
    from hermes_cli import copilot_auth

    monkeypatch.setattr(copilot_auth, "_try_gh_cli_token", lambda: "host-gh-token")
    set_multiplex_active(True)
    a = set_secret_scope({"COPILOT_GITHUB_TOKEN": "gho_profile_a"})
    try:
        assert copilot_auth.resolve_copilot_token() == (
            "gho_profile_a", "COPILOT_GITHUB_TOKEN",
        )
    finally:
        reset_secret_scope(a)
    b = set_secret_scope({"COPILOT_GITHUB_TOKEN": "gho_profile_b"})
    try:
        assert copilot_auth.resolve_copilot_token() == (
            "gho_profile_b", "COPILOT_GITHUB_TOKEN",
        )
    finally:
        reset_secret_scope(b)
        set_multiplex_active(False)
