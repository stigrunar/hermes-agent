"""Profile-isolation contract for auxiliary.<task>.key_env."""

from __future__ import annotations

import pytest

from agent import auxiliary_client as auxiliary
from agent import secret_scope


@pytest.fixture(autouse=True)
def _reset_multiplex():
    secret_scope.set_multiplex_active(False)
    yield
    secret_scope.set_multiplex_active(False)


def test_auxiliary_key_env_uses_authoritative_profile_scope(monkeypatch):
    monkeypatch.setenv("SECONDARY_KEY", "hostile-ambient-secret")
    monkeypatch.setattr(
        auxiliary,
        "_get_auxiliary_task_config",
        lambda _task: {
            "provider": "custom",
            "model": "test-model",
            "base_url": "https://example.invalid/v1",
            "key_env": "SECONDARY_KEY",
        },
    )
    secret_scope.set_multiplex_active(True)
    token = secret_scope.set_secret_scope(
        {"SECONDARY_KEY": "profile-owned-secret"}
    )
    try:
        resolved = auxiliary._resolve_task_provider_model("compression")
    finally:
        secret_scope.reset_secret_scope(token)

    assert resolved[3] == "profile-owned-secret"
    with pytest.raises(secret_scope.UnscopedSecretError):
        auxiliary._resolve_task_provider_model("compression")
