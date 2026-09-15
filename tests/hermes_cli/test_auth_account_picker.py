"""Behavior contracts for the native authentication account picker."""

from __future__ import annotations

import argparse
import base64
import json


def _jwt(claims: dict) -> str:
    def _part(payload: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()

    return f"{_part({'alg': 'none'})}.{_part(claims)}.signature"


def _write_store(tmp_path, monkeypatch, entries: list[dict], *, active: str | None = None):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {},
        "active_provider": active,
        "credential_pool": {"openai-codex": entries},
    }), encoding="utf-8")
    monkeypatch.setattr(
        "agent.credential_pool._seed_from_singletons",
        lambda provider, current: (False, set()),
    )
    monkeypatch.setattr(
        "hermes_cli.auth_commands._picker_provider_ids",
        lambda: ["openai-codex"],
    )
    return hermes_home


def _entry(entry_id: str, label: str, token: str, priority: int, **status) -> dict:
    return {
        "id": entry_id,
        "label": label,
        "auth_type": "oauth",
        "priority": priority,
        "source": "manual:device_code",
        "access_token": token,
        "refresh_token": f"refresh-{entry_id}",
        **status,
    }


def test_picker_groups_account_aliases_and_redacts_identity(tmp_path, monkeypatch):
    token = _jwt({
        "sub": "account-123",
        "email": "person@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "account-123",
            "chatgpt_plan_type": "plus",
        },
    })
    _write_store(tmp_path, monkeypatch, [
        _entry("first", "person@example.com", token, 0),
        _entry("second", "Laptop", token, 1),
        _entry(
            "third", "Team", _jwt({"sub": "account-456"}), 2,
            last_status="dead", last_status_at=1,
        ),
    ], active="openai-codex")

    from hermes_cli.auth_commands import build_auth_account_picker, format_auth_account_status

    payload = build_auth_account_picker(active_credential_id="first")
    assert len(payload["accounts"]) == 2
    duplicate = payload["accounts"][0]
    assert duplicate["active"] is True
    assert duplicate["duplicate_count"] == 2
    assert duplicate["plan"] == "plus"
    assert duplicate["plan_verified"] is True
    assert duplicate["identity_verified"] is True
    assert duplicate["availability"] == "available"
    assert [alias["label"] for alias in duplicate["aliases"]][1] == "Laptop"
    assert payload["accounts"][1]["availability"] == "unavailable"

    rendered = json.dumps(payload) + format_auth_account_status(payload)
    assert "person@example.com" not in rendered
    assert token not in rendered
    assert "refresh-first" not in rendered
    assert "acct:" in rendered
    assert "2 grouped aliases" in rendered


def test_use_promotes_native_pool_priority_and_active_provider(tmp_path, monkeypatch):
    _write_store(tmp_path, monkeypatch, [
        _entry("first", "Primary", _jwt({"sub": "account-1"}), 0),
        _entry("second", "Travel", _jwt({"sub": "account-2"}), 1),
    ])

    from hermes_cli.auth_commands import use_auth_account

    result = use_auth_account("openai-codex", "second")
    stored = json.loads((tmp_path / "hermes" / "auth.json").read_text(encoding="utf-8"))
    assert result == {
        "provider": "openai-codex",
        "label": "Travel",
        "fingerprint": result["fingerprint"],
        "strategy": "fill_first",
        "credential_id": "second",
    }
    assert [row["id"] for row in stored["credential_pool"]["openai-codex"]] == [
        "second", "first",
    ]
    assert [row["priority"] for row in stored["credential_pool"]["openai-codex"]] == [0, 1]
    assert stored["active_provider"] == "openai-codex"


def test_add_uses_native_oauth_seam_and_promotes_new_entry(tmp_path, monkeypatch):
    _write_store(tmp_path, monkeypatch, [])
    observed = {}

    def _fake_add(args, provider, pool, requested_type):
        from agent.credential_pool import PooledCredential

        observed.update(provider=provider, requested_type=requested_type, callback=args.on_verification)
        return pool.add_entry(PooledCredential.from_dict(
            provider, _entry("new-account", "New account", _jwt({"sub": "account-new"}), 0)))

    monkeypatch.setattr("hermes_cli.auth_commands._add_credential", _fake_add)
    monkeypatch.setattr("hermes_cli.auth_commands._unsuppress_provider_sources", lambda provider: None)
    callback = lambda url, code: None

    from hermes_cli.auth_commands import add_auth_account

    result = add_auth_account("openai-codex", on_verification=callback)
    assert observed == {
        "provider": "openai-codex",
        "requested_type": "oauth",
        "callback": callback,
    }
    assert result["credential_id"] == "new-account"
    assert result["label"] == "New account"


def test_cli_exposes_status_use_and_reauthenticate_operations():
    from hermes_cli.subcommands.auth import build_auth_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_auth_parser(subparsers, cmd_auth=lambda args: None)

    status = parser.parse_args(["auth", "status"])
    use = parser.parse_args(["auth", "use", "openai-codex", "account-id"])
    reauth = parser.parse_args(["auth", "reauth", "openai-codex", "account-id", "--no-browser"])
    assert status.provider is None
    assert (use.auth_action, use.provider, use.target) == ("use", "openai-codex", "account-id")
    assert (reauth.auth_action, reauth.target, reauth.no_browser) == ("reauth", "account-id", True)
