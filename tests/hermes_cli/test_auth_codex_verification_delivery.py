"""OpenAI Codex device-login verification delivery for non-terminal auth surfaces."""

from __future__ import annotations

from types import SimpleNamespace

import hermes_cli.auth_codex as auth_codex
import hermes_cli.auth_commands as auth_commands


def test_openai_add_spec_forwards_verification_callback(monkeypatch):
    observed = {}
    callback = lambda url, code: None

    def _fake_login(*, on_verification=None):
        observed["callback"] = on_verification
        return {"tokens": {"access_token": "unused", "refresh_token": "unused"}}

    monkeypatch.setattr(auth_commands.auth_mod, "_codex_device_code_login", _fake_login)

    auth_commands._OAUTH_ADD_SPECS["openai-codex"].login(
        SimpleNamespace(on_verification=callback)
    )

    assert observed["callback"] is callback


def test_codex_device_login_delivers_verification_before_poll(monkeypatch):
    events = []

    monkeypatch.setattr(
        auth_codex,
        "_codex_request_device_code",
        lambda issuer, client_id: {
            "user_code": "ABCD-EFGH",
            "device_auth_id": "device-auth-id",
            "interval": 3,
        },
    )

    def _verify(url: str, code: str) -> None:
        events.append(("verify", url, code))

    def _poll(issuer: str, *, device_auth_id: str, user_code: str, poll_interval: int):
        events.append(("poll", issuer, user_code))
        assert events[0] == (
            "verify", "https://auth.openai.com/codex/device", "ABCD-EFGH"
        )
        assert device_auth_id == "device-auth-id"
        assert poll_interval == 3
        return {"authorization_code": "auth-code", "code_verifier": "verifier"}

    monkeypatch.setattr(auth_codex, "_codex_poll_authorization_code", _poll)
    monkeypatch.setattr(
        auth_codex,
        "_codex_exchange_authorization_code",
        lambda issuer, client_id, code_resp: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
        },
    )

    result = auth_codex._codex_device_code_login(on_verification=_verify)

    assert events == [
        ("verify", "https://auth.openai.com/codex/device", "ABCD-EFGH"),
        ("poll", "https://auth.openai.com", "ABCD-EFGH"),
    ]
    assert result["tokens"] == {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
    }
