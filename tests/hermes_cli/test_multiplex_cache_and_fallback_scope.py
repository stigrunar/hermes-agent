"""Cache identity and broad fallbacks preserve the profile boundary signal."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent import secret_scope as ss


@pytest.fixture(autouse=True)
def _reset_multiplex():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


def test_model_cache_fingerprint_tracks_scoped_key_and_endpoint(monkeypatch):
    from hermes_cli.models import _credential_fingerprint

    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-hostile-cache-key")
    monkeypatch.setenv(
        "DEEPSEEK_BASE_URL", "https://synthetic-hostile-cache.invalid/v1"
    )
    ss.set_multiplex_active(True)

    fingerprints = []
    for suffix in ("a", "b"):
        token = ss.set_secret_scope(
            {
                "DEEPSEEK_API_KEY": f"synthetic-profile-{suffix}-cache-key",
                "DEEPSEEK_BASE_URL": (
                    f"https://synthetic-profile-{suffix}-cache.invalid/v1"
                ),
            }
        )
        try:
            fingerprints.append(_credential_fingerprint("deepseek"))
            monkeypatch.setenv(
                "DEEPSEEK_API_KEY", f"synthetic-other-hostile-{suffix}"
            )
            assert _credential_fingerprint("deepseek") == fingerprints[-1]
        finally:
            ss.reset_secret_scope(token)

    assert fingerprints[0] != fingerprints[1]


def test_unscoped_model_cache_fingerprint_fails_closed(monkeypatch):
    from hermes_cli.models import _credential_fingerprint

    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-hostile-cache-key")
    ss.set_multiplex_active(True)
    with pytest.raises(ss.UnscopedSecretError):
        _credential_fingerprint("deepseek")


def test_models_fallbacks_propagate_unscoped_error(monkeypatch):
    from hermes_cli import auth, models

    def fail(*_args, **_kwargs):
        raise ss.UnscopedSecretError("synthetic boundary")

    monkeypatch.setattr(auth, "resolve_nous_runtime_credentials", fail)
    with pytest.raises(ss.UnscopedSecretError):
        models._resolve_nous_pricing_credentials()
    with pytest.raises(ss.UnscopedSecretError):
        models.provider_model_ids("nous")

    monkeypatch.setattr(auth, "get_auth_status", fail)
    with pytest.raises(ss.UnscopedSecretError):
        models.list_available_providers()


def test_auxiliary_refresh_and_resolution_propagate_unscoped_error(monkeypatch):
    from agent import auxiliary_client
    from hermes_cli import auth

    def fail(*_args, **_kwargs):
        raise ss.UnscopedSecretError("synthetic boundary")

    monkeypatch.setattr(auxiliary_client, "_resolve_nous_pool_runtime_api", lambda **_kwargs: None)
    monkeypatch.setattr(auth, "resolve_nous_runtime_credentials", fail)
    with pytest.raises(ss.UnscopedSecretError):
        auxiliary_client._resolve_nous_runtime_api()

    monkeypatch.setattr(auxiliary_client, "load_pool", fail)
    with pytest.raises(ss.UnscopedSecretError):
        auxiliary_client._refresh_provider_credentials("xai-oauth")


def test_model_switch_does_not_convert_unscoped_error_to_normal_failure():
    from hermes_cli.model_switch import switch_model

    def fail(*_args, **_kwargs):
        raise ss.UnscopedSecretError("synthetic boundary")

    accepted = {
        "accepted": True,
        "persist": True,
        "recognized": True,
        "message": None,
    }
    with patch("hermes_cli.model_switch.resolve_alias", return_value=None), patch(
        "hermes_cli.model_switch.list_provider_models", return_value=[]
    ), patch(
        "hermes_cli.models.validate_requested_model", return_value=accepted
    ), patch(
        "hermes_cli.models.detect_provider_for_model",
        return_value=("deepseek", "deepseek-chat"),
    ), patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=fail
    ):
        with pytest.raises(ss.UnscopedSecretError):
            switch_model(
                raw_input="deepseek:deepseek-chat",
                current_provider="openrouter",
                current_model="old-model",
            )
