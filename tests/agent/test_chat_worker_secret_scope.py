"""Provider timeout workers retain the active profile owner context."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import chat_completion_helpers as helpers
from agent import secret_scope as ss


@pytest.fixture(autouse=True)
def _reset_multiplex():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


def _fake_agent() -> SimpleNamespace:
    return SimpleNamespace(
        api_mode="chat_completions",
        provider="custom",
        platform="gateway",
        model="synthetic-model",
        base_url="https://synthetic-profile-endpoint.invalid/v1",
        _base_url_lower="https://synthetic-profile-endpoint.invalid/v1",
        _base_url_hostname="synthetic-profile-endpoint.invalid",
        _interrupt_requested=False,
        _consecutive_stale_streams=0,
        _compute_non_stream_stale_timeout=lambda _kwargs: 5.0,
        _touch_activity=lambda _message: None,
        _emit_wait_notice=lambda _message: None,
        _buffer_status=lambda _message: None,
        _close_request_openai_client=lambda *_args, **_kwargs: None,
        _abort_request_openai_client=lambda *_args, **_kwargs: None,
    )


def test_nonstream_timeout_worker_uses_each_profile_scope(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_WORKER_API_KEY", "synthetic-hostile-ambient-key")
    monkeypatch.setattr(
        helpers,
        "_dispatch_nonstreaming_api_request",
        lambda *_args, **_kwargs: ss.get_secret("SYNTHETIC_WORKER_API_KEY"),
    )
    ss.set_multiplex_active(True)

    observed = []
    for suffix in ("a", "b"):
        token = ss.set_secret_scope(
            {"SYNTHETIC_WORKER_API_KEY": f"synthetic-profile-{suffix}-key"}
        )
        try:
            observed.append(
                helpers.interruptible_api_call(_fake_agent(), {"model": "synthetic"})
            )
        finally:
            ss.reset_secret_scope(token)

    assert observed == ["synthetic-profile-a-key", "synthetic-profile-b-key"]


def test_nonstream_timeout_worker_propagates_unscoped_boundary(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_WORKER_API_KEY", "synthetic-hostile-ambient-key")
    monkeypatch.setattr(
        helpers,
        "_dispatch_nonstreaming_api_request",
        lambda *_args, **_kwargs: ss.get_secret("SYNTHETIC_WORKER_API_KEY"),
    )
    ss.set_multiplex_active(True)

    with pytest.raises(ss.UnscopedSecretError):
        helpers.interruptible_api_call(_fake_agent(), {"model": "synthetic"})
