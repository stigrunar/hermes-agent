from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

from gateway.voice_topic_binding import (
    VoiceLaunchStore,
    VoiceMiniAppError,
    launcher_url,
    normalize_thread_id,
    verify_telegram_init_data,
)


def _init_data(*, token: str, user_id: int, auth_date: int, tamper: bool = False) -> str:
    values = {
        "auth_date": str(auth_date),
        "query_id": "AA-test",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    values["hash"] = ("0" * 64) if tamper else digest
    return urlencode(values)


def test_launcher_url_uses_only_opaque_startapp_value() -> None:
    url = launcher_url("https://t.me/DollyClawBot/voice", "opaque-value")
    assert url == "https://t.me/DollyClawBot/voice?startapp=opaque-value"
    assert "chat" not in url and "thread" not in url and "session" not in url


def test_launcher_url_rejects_non_telegram_app_url() -> None:
    with pytest.raises(VoiceMiniAppError, match="Telegram direct link"):
        launcher_url("https://private.example/ui/voice", "opaque")


def test_store_survives_restart_and_consumes_once(tmp_path: Path) -> None:
    nonce = VoiceLaunchStore(tmp_path).create(
        platform="telegram",
        chat_id="-1001",
        chat_type="group",
        thread_id="22",
        user_id="42",
        profile="default",
        session_key="telegram:-1001:22:42",
        now=1000,
        ttl_seconds=60,
    )
    restarted = VoiceLaunchStore(tmp_path)
    assert restarted.lookup(nonce)["thread_id"] == "22"
    assert restarted.consume(nonce, now=1010)["session_key"] == "telegram:-1001:22:42"
    assert restarted.consume(nonce, now=1011) is None


def test_expired_receipt_cannot_be_consumed(tmp_path: Path) -> None:
    nonce = VoiceLaunchStore(tmp_path).create(
        platform="telegram",
        chat_id="1",
        chat_type="dm",
        thread_id=None,
        user_id="42",
        profile="default",
        session_key="lane",
        now=1000,
        ttl_seconds=5,
    )
    assert VoiceLaunchStore(tmp_path).consume(nonce, now=1005) is None


def test_valid_telegram_init_data_and_exact_user() -> None:
    token = "12345:secret"
    data = _init_data(token=token, user_id=42, auth_date=1000)
    assert verify_telegram_init_data(data, token, expected_user_id="42", now=1010) == {"id": "42"}


@pytest.mark.parametrize(
    ("data_factory", "expected"),
    [
        (lambda token: _init_data(token=token, user_id=42, auth_date=1000, tamper=True), "signature"),
        (lambda token: _init_data(token=token, user_id=99, auth_date=1000), "does not match"),
        (lambda token: _init_data(token=token, user_id=42, auth_date=1), "expired"),
    ],
)
def test_telegram_init_data_fails_closed(data_factory, expected: str) -> None:
    token = "12345:secret"
    with pytest.raises(VoiceMiniAppError, match=expected):
        verify_telegram_init_data(data_factory(token), token, expected_user_id="42", now=1010)


def test_thread_normalization() -> None:
    assert normalize_thread_id(None) is None
    assert normalize_thread_id(1) is None
    assert normalize_thread_id("22") == "22"
