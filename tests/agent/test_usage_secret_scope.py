"""Nested usage timeout workers preserve profile-owner context."""

from __future__ import annotations

import pytest

from agent import secret_scope as ss


@pytest.fixture(autouse=True)
def _reset_multiplex(monkeypatch):
    monkeypatch.delenv("HERMES_DEV_CREDITS_FIXTURE", raising=False)
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


def test_billing_timeout_worker_inherits_profile_secret_scope(monkeypatch):
    from agent import billing_usage
    from hermes_cli import auth, nous_account

    monkeypatch.setattr(
        auth,
        "get_provider_auth_state",
        lambda _provider: {"access_token": "synthetic-account-access"},
    )
    monkeypatch.setattr(
        nous_account,
        "get_nous_portal_account_info",
        lambda **_kwargs: ss.get_secret("NOUS_INFERENCE_BASE_URL"),
    )
    monkeypatch.setattr(
        billing_usage, "usage_model_from_account", lambda account: account
    )
    monkeypatch.setenv(
        "NOUS_INFERENCE_BASE_URL", "https://synthetic-hostile-nous.invalid/v1"
    )

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(
        {
            "NOUS_INFERENCE_BASE_URL": (
                "https://synthetic-profile-nous.invalid/v1"
            )
        }
    )
    try:
        result = billing_usage.build_usage_model(timeout=1.0)
    finally:
        ss.reset_secret_scope(token)

    assert result == "https://synthetic-profile-nous.invalid/v1"


def test_billing_timeout_worker_does_not_suppress_unscoped_error(monkeypatch):
    from agent import billing_usage
    from hermes_cli import auth, nous_account

    monkeypatch.setattr(
        auth,
        "get_provider_auth_state",
        lambda _provider: {"access_token": "synthetic-account-access"},
    )
    monkeypatch.setattr(
        nous_account,
        "get_nous_portal_account_info",
        lambda **_kwargs: ss.get_secret("NOUS_INFERENCE_BASE_URL"),
    )
    monkeypatch.setenv(
        "NOUS_INFERENCE_BASE_URL", "https://synthetic-hostile-nous.invalid/v1"
    )
    ss.set_multiplex_active(True)

    with pytest.raises(ss.UnscopedSecretError):
        billing_usage.build_usage_model(timeout=1.0)


def test_credits_seed_thread_inherits_profile_secret_scope(monkeypatch):
    import threading
    from types import SimpleNamespace

    from agent import credits_tracker
    from hermes_cli import nous_account

    completed = threading.Event()
    observed = {}
    monkeypatch.setattr(credits_tracker, "dev_fixture_credits_state", lambda: None)
    monkeypatch.setattr(
        nous_account,
        "get_nous_portal_account_info",
        lambda **_kwargs: ss.get_secret("SYNTHETIC_CREDITS_OWNER"),
    )
    monkeypatch.setattr(
        credits_tracker,
        "_credits_state_from_account",
        lambda owner: {"owner": owner},
    )

    def _hydrate(_agent, state):
        observed.update(state)
        completed.set()

    monkeypatch.setattr(credits_tracker, "_hydrate_seed_state", _hydrate)
    monkeypatch.setenv(
        "SYNTHETIC_CREDITS_OWNER", "synthetic-hostile-ambient-owner"
    )
    agent = SimpleNamespace(provider="nous", _credits_state=None)

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(
        {"SYNTHETIC_CREDITS_OWNER": "synthetic-profile-credits-owner"}
    )
    try:
        assert credits_tracker.seed_credits_at_session_start(agent) is True
        assert completed.wait(2.0)
    finally:
        ss.reset_secret_scope(token)

    assert observed == {"owner": "synthetic-profile-credits-owner"}


def test_credits_seed_rejects_unscoped_multiplex(monkeypatch):
    from types import SimpleNamespace

    from agent import credits_tracker

    monkeypatch.setattr(credits_tracker, "dev_fixture_credits_state", lambda: None)
    ss.set_multiplex_active(True)

    with pytest.raises(ss.UnscopedSecretError):
        credits_tracker.seed_credits_at_session_start(
            SimpleNamespace(provider="nous", _credits_state=None)
        )
