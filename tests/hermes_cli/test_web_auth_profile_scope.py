"""Dashboard auth workers keep the selected profile's secret owner."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent import secret_scope as ss


@pytest.fixture(autouse=True)
def _reset_multiplex():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


def test_dashboard_auth_scope_reaches_executor_and_thread(
    monkeypatch, tmp_path
):
    default_home = tmp_path / "synthetic-default"
    profile_home = tmp_path / "synthetic-profile-a"
    default_home.mkdir()
    profile_home.mkdir()
    (profile_home / ".env").write_text(
        "SYNTHETIC_DASHBOARD_KEY=synthetic-profile-dashboard-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setenv(
        "SYNTHETIC_DASHBOARD_KEY", "synthetic-hostile-dashboard-key"
    )

    from hermes_cli import web_server

    monkeypatch.setattr(
        web_server,
        "_resolve_profile_dir",
        lambda profile: profile_home if profile == "synthetic-a" else default_home,
    )
    seen = []

    ss.set_multiplex_active(True)
    with web_server._auth_profile_scope("synthetic-a"):
        executor_value = asyncio.run(
            web_server._run_auth_in_executor(
                ss.get_secret, "SYNTHETIC_DASHBOARD_KEY"
            )
        )
        thread = web_server._start_auth_thread(
            lambda: seen.append(ss.get_secret("SYNTHETIC_DASHBOARD_KEY")),
            name="synthetic-auth-context-test",
        )
        thread.join(timeout=2)

    assert executor_value == "synthetic-profile-dashboard-key"
    assert seen == ["synthetic-profile-dashboard-key"]


def test_dashboard_auth_scope_isolated_between_profiles(monkeypatch, tmp_path):
    homes = {}
    for suffix in ("a", "b"):
        home = tmp_path / f"synthetic-profile-{suffix}"
        home.mkdir()
        (home / ".env").write_text(
            f"SYNTHETIC_DASHBOARD_KEY=synthetic-profile-{suffix}-dashboard-key\n",
            encoding="utf-8",
        )
        homes[f"synthetic-{suffix}"] = home
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "synthetic-default"))
    monkeypatch.setenv(
        "SYNTHETIC_DASHBOARD_KEY", "synthetic-hostile-dashboard-key"
    )

    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "_resolve_profile_dir", homes.__getitem__)
    ss.set_multiplex_active(True)

    observed = []
    for profile in ("synthetic-a", "synthetic-b"):
        with web_server._auth_profile_scope(profile):
            observed.append(ss.get_secret("SYNTHETIC_DASHBOARD_KEY"))

    assert observed == [
        "synthetic-profile-a-dashboard-key",
        "synthetic-profile-b-dashboard-key",
    ]


def test_dashboard_credential_pool_listing_uses_selected_profile(
    monkeypatch, tmp_path
):
    from agent import credential_pool
    from hermes_cli import auth, web_server

    homes = {}
    for suffix in ("a", "b"):
        home = tmp_path / f"synthetic-pool-profile-{suffix}"
        home.mkdir()
        (home / ".env").write_text(
            f"SYNTHETIC_POOL_KEY=synthetic-profile-{suffix}-pool-key\n",
            encoding="utf-8",
        )
        homes[f"synthetic-{suffix}"] = home

    seen = []

    class Pool:
        def entries(self):
            value = ss.get_secret("SYNTHETIC_POOL_KEY")
            seen.append(value)
            return [
                type(
                    "Entry",
                    (),
                    {
                        "access_token": value,
                        "refresh_token": None,
                    },
                )()
            ]

    monkeypatch.setenv(
        "SYNTHETIC_POOL_KEY", "synthetic-hostile-dashboard-pool-key"
    )
    monkeypatch.setattr(web_server, "_resolve_profile_dir", homes.__getitem__)
    monkeypatch.setattr(auth, "read_credential_pool", lambda: {"synthetic": []})
    monkeypatch.setattr(credential_pool, "load_pool", lambda _provider: Pool())
    ss.set_multiplex_active(True)

    for profile in ("synthetic-a", "synthetic-b"):
        result = asyncio.run(web_server.list_credential_pool(profile=profile))
        assert result["providers"]

    assert seen == [
        "synthetic-profile-a-pool-key",
        "synthetic-profile-b-pool-key",
    ]


def test_dashboard_anthropic_status_ignores_hostile_ambient(monkeypatch, tmp_path):
    profile_home = tmp_path / "synthetic-profile-status"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "synthetic-default"))
    monkeypatch.setenv(
        "ANTHROPIC_API_KEY", "synthetic-hostile-anthropic-status-key"
    )

    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "_resolve_profile_dir", lambda _p: profile_home)
    ss.set_multiplex_active(True)
    with web_server._auth_profile_scope("synthetic-status"):
        status = web_server._anthropic_oauth_status()

    assert status["logged_in"] is False


def test_default_dashboard_keeps_single_profile_ambient_compatibility(
    monkeypatch, tmp_path
):
    default_home = tmp_path / "synthetic-default"
    default_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setenv(
        "SYNTHETIC_DASHBOARD_KEY", "synthetic-single-profile-dashboard-key"
    )

    from hermes_cli import web_server

    with web_server._auth_profile_scope(None):
        assert ss.current_secret_scope() is None
        assert ss.get_secret("SYNTHETIC_DASHBOARD_KEY") == (
            "synthetic-single-profile-dashboard-key"
        )


def test_xai_dashboard_login_shadows_inherited_global_owner(monkeypatch, tmp_path):
    import httpx

    from hermes_cli import auth, web_server

    root = tmp_path / "synthetic-hermes-root"
    profile_home = root / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(
        web_server,
        "_resolve_profile_dir",
        lambda profile: profile_home if profile == "coder" else root,
    )
    global_auth = root / "auth.json"
    global_auth.write_text(
        json.dumps(
            {
                "providers": {
                    "xai-oauth": {
                        "tokens": {
                            "access_token": "synthetic-global-access",
                            "refresh_token": "synthetic-global-refresh",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(
        auth,
        "_xai_oauth_discovery",
        lambda _timeout: {"token_endpoint": "https://synthetic-token.invalid"},
    )
    monkeypatch.setattr(
        auth,
        "_xai_oauth_poll_device_token",
        lambda *_args, **_kwargs: {
            "access_token": "synthetic-profile-access",
            "refresh_token": "synthetic-profile-refresh",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(auth, "unsuppress_credential_source", lambda *_args: None)

    sid, _ = web_server._new_oauth_session(
        "xai-oauth", "device_code", profile="coder"
    )
    web_server._oauth_sessions[sid].update(
        device_code="synthetic-device-code",
        interval=1,
        expires_at=web_server.time.time() + 60,
    )
    ss.set_multiplex_active(True)
    try:
        web_server._xai_device_poller(sid)
    finally:
        web_server._oauth_sessions.pop(sid, None)

    global_state = json.loads(global_auth.read_text(encoding="utf-8"))
    profile_state = json.loads((profile_home / "auth.json").read_text(encoding="utf-8"))
    assert global_state["providers"]["xai-oauth"]["tokens"]["access_token"] == (
        "synthetic-global-access"
    )
    assert profile_state["providers"]["xai-oauth"]["tokens"]["access_token"] == (
        "synthetic-profile-access"
    )


def test_default_anthropic_oauth_owner_rejects_named_submit_profile():
    from fastapi import HTTPException
    from hermes_cli import web_server

    sid, _ = web_server._new_oauth_session("anthropic", "pkce", profile=None)
    try:
        assert web_server._oauth_session_profile(sid) is None
        with pytest.raises(HTTPException) as exc:
            web_server._oauth_session_profile(sid, "coder")
        assert exc.value.status_code == 409
    finally:
        web_server._oauth_sessions.pop(sid, None)


def test_named_anthropic_oauth_owner_rejects_conflicting_profile():
    from fastapi import HTTPException
    from hermes_cli import web_server

    sid, _ = web_server._new_oauth_session(
        "anthropic", "pkce", profile="profile-a"
    )
    try:
        assert web_server._oauth_session_profile(sid) == "profile-a"
        with pytest.raises(HTTPException) as exc:
            web_server._oauth_session_profile(sid, "profile-b")
        assert exc.value.status_code == 409
    finally:
        web_server._oauth_sessions.pop(sid, None)


def test_anthropic_dashboard_save_propagates_unscoped_error(monkeypatch):
    from hermes_cli import web_server

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "access_token": "synthetic-anthropic-access",
                    "refresh_token": "synthetic-anthropic-refresh",
                    "expires_in": 3600,
                }
            ).encode()

    def fail_save(*_args, **_kwargs):
        raise ss.UnscopedSecretError("synthetic boundary")

    monkeypatch.setattr(web_server.urllib.request, "urlopen", lambda *_a, **_k: Response())
    monkeypatch.setattr(web_server, "_save_anthropic_oauth_creds", fail_save)
    sid, session = web_server._new_oauth_session(
        "anthropic", "pkce", profile=None
    )
    session.update(state="synthetic-state", verifier="synthetic-verifier")
    try:
        with pytest.raises(ss.UnscopedSecretError):
            web_server._submit_anthropic_pkce(
                sid,
                "synthetic-code#synthetic-state",
                None,
            )
    finally:
        web_server._oauth_sessions.pop(sid, None)
