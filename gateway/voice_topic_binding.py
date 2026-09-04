"""Telegram Voice Mini App launch receipts and authenticated exchange.

The Telegram button carries only a short-lived opaque nonce.  The nonce is
stored as a digest in a small profile-local SQLite database; all routing
authority stays on the server and is consumed atomically after Telegram's
``initData`` signature has been verified.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

VOICE_MINI_APP_TTL_SECONDS = 5 * 60
VOICE_MINI_APP_AUTH_FRESHNESS_SECONDS = 5 * 60
VOICE_MINI_APP_CLOCK_SKEW_SECONDS = 60
VOICE_MINI_APP_MAX_BODY_BYTES = 32 * 1024
VOICE_MINI_APP_MAX_INIT_DATA_BYTES = 16 * 1024
VOICE_MINI_APP_DB_NAME = "voice_mini_app.db"


class VoiceMiniAppError(ValueError):
    """A safe, expected failure from the Mini App exchange boundary."""

    def __init__(self, message: str, *, status: int = 400, code: str = "invalid_request"):
        super().__init__(message)
        self.status = status
        self.code = code


def normalize_thread_id(thread_id: Any) -> Optional[str]:
    """Normalize Telegram root/General topic ids to one stable representation."""
    if thread_id is None:
        return None
    value = str(thread_id).strip()
    return None if value in {"", "1"} else value


def _bounded_text(value: Any, *, name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise VoiceMiniAppError(f"{name} must be a string")
    value = value.strip()
    if not value or len(value) > max_length:
        raise VoiceMiniAppError(f"{name} is invalid")
    return value


def _nonce_digest(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("ascii")).hexdigest()


def launcher_url(base_url: str, nonce: str) -> str:
    """Append the opaque nonce as Telegram's direct-link ``startapp`` value."""
    base_url = _bounded_text(base_url, name="voice_mini_app_url", max_length=2048)
    try:
        parsed = urlsplit(base_url)
    except ValueError as exc:
        raise VoiceMiniAppError("Voice Mini App URL is invalid", code="invalid_config") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.fragment:
        raise VoiceMiniAppError("Voice Mini App URL must be an absolute HTTP(S) URL", code="invalid_config")
    query = list(parse_qsl(parsed.query, keep_blank_values=True))
    # A normal HTTPS app URL opened from a group does not receive signed
    # Telegram initData.  The configured URL must therefore be the BotFather
    # direct-link form (https://t.me/<bot>/<short-name>); Telegram forwards the
    # opaque startapp value into the Mini App launch context.
    if parsed.hostname not in {"t.me", "www.t.me"}:
        raise VoiceMiniAppError(
            "Voice Mini App URL must be a Telegram direct link",
            code="invalid_config",
        )
    query = [(key, value) for key, value in query if key != "startapp"]
    query.append(("startapp", nonce))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


class VoiceLaunchStore:
    """Profile-local durable store for one-time Telegram launch receipts."""

    def __init__(self, home: Path | str):
        self.home = Path(home)
        self.path = self.home / VOICE_MINI_APP_DB_NAME

    def _connect(self) -> sqlite3.Connection:
        self.home.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS voice_launch_receipts (
                nonce_digest TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                thread_id TEXT,
                user_id TEXT NOT NULL,
                profile TEXT NOT NULL,
                session_key TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                consumed_at REAL
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_voice_launch_receipts_expiry "
            "ON voice_launch_receipts(expires_at)"
        )
        conn.commit()
        return conn

    def create(
        self,
        *,
        platform: str,
        chat_id: str,
        chat_type: str,
        thread_id: Optional[str],
        user_id: str,
        profile: str,
        session_key: str,
        now: Optional[float] = None,
        ttl_seconds: int = VOICE_MINI_APP_TTL_SECONDS,
    ) -> str:
        platform = _bounded_text(platform, name="platform", max_length=32).lower()
        chat_id = _bounded_text(str(chat_id), name="chat_id", max_length=256)
        chat_type = _bounded_text(str(chat_type or "dm"), name="chat_type", max_length=32).lower()
        user_id = _bounded_text(str(user_id), name="user_id", max_length=256)
        profile = _bounded_text(str(profile or "default"), name="profile", max_length=128)
        session_key = _bounded_text(str(session_key), name="session_key", max_length=512)
        thread_id = normalize_thread_id(thread_id)
        if thread_id is not None:
            thread_id = _bounded_text(thread_id, name="thread_id", max_length=256)
        now = time.time() if now is None else float(now)
        ttl_seconds = max(1, min(int(ttl_seconds), 3600))
        nonce = secrets.token_urlsafe(32)
        conn = self._connect()
        try:
            # The receipt is intentionally tiny and one-shot. Remove expired
            # rows while minting so a long-lived gateway cannot accumulate
            # abandoned launches indefinitely.
            conn.execute(
                "DELETE FROM voice_launch_receipts WHERE expires_at <= ?",
                (now,),
            )
            conn.execute(
                """INSERT INTO voice_launch_receipts (
                    nonce_digest, platform, chat_id, chat_type, thread_id,
                    user_id, profile, session_key, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _nonce_digest(nonce),
                    platform,
                    chat_id,
                    chat_type,
                    thread_id,
                    user_id,
                    profile,
                    session_key,
                    now,
                    now + ttl_seconds,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return nonce

    @staticmethod
    def _row_to_receipt(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def lookup(self, nonce: str) -> Optional[dict[str, Any]]:
        """Read a receipt without consuming it (needed before auth)."""
        if not isinstance(nonce, str) or not nonce or len(nonce) > 256:
            return None
        try:
            digest = _nonce_digest(nonce)
        except UnicodeEncodeError:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM voice_launch_receipts WHERE nonce_digest = ?",
                (digest,),
            ).fetchone()
            return self._row_to_receipt(row) if row else None
        finally:
            conn.close()

    def consume(self, nonce: str, *, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Atomically claim one unexpired receipt, returning its metadata."""
        if not isinstance(nonce, str) or not nonce or len(nonce) > 256:
            return None
        try:
            digest = _nonce_digest(nonce)
        except UnicodeEncodeError:
            return None
        now = time.time() if now is None else float(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM voice_launch_receipts
                   WHERE nonce_digest = ? AND consumed_at IS NULL AND expires_at > ?""",
                (digest, now),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            updated = conn.execute(
                """UPDATE voice_launch_receipts SET consumed_at = ?
                   WHERE nonce_digest = ? AND consumed_at IS NULL AND expires_at > ?""",
                (now, digest, now),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            receipt = self._row_to_receipt(row)
            receipt["consumed_at"] = now
            return receipt
        finally:
            conn.close()


def verify_telegram_init_data(
    init_data: str,
    bot_token: str,
    *,
    expected_user_id: Optional[str] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Verify Telegram WebApp ``initData`` and return only safe user fields."""
    init_data = _bounded_text(init_data, name="init_data", max_length=VOICE_MINI_APP_MAX_INIT_DATA_BYTES)
    bot_token = _bounded_text(bot_token, name="bot_token", max_length=512)
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise VoiceMiniAppError("Telegram initData is malformed", code="telegram_auth_failed") from exc
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise VoiceMiniAppError("Duplicate initData field", code="telegram_auth_failed")
        values[key] = value
    supplied_hash = values.pop("hash", "")
    if not supplied_hash or len(supplied_hash) != 64:
        raise VoiceMiniAppError("Telegram initData signature is missing", code="telegram_auth_failed")
    try:
        supplied_hash_bytes = bytes.fromhex(supplied_hash)
    except ValueError as exc:
        raise VoiceMiniAppError("Telegram initData signature is invalid", code="telegram_auth_failed") from exc
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).digest()
    if not hmac.compare_digest(supplied_hash_bytes, expected_hash):
        raise VoiceMiniAppError("Telegram initData signature is invalid", code="telegram_auth_failed")

    try:
        auth_date = int(values.get("auth_date", ""))
    except (TypeError, ValueError) as exc:
        raise VoiceMiniAppError("Telegram auth_date is invalid", code="telegram_auth_failed") from exc
    now = time.time() if now is None else float(now)
    age = now - auth_date
    if age > VOICE_MINI_APP_AUTH_FRESHNESS_SECONDS or age < -VOICE_MINI_APP_CLOCK_SKEW_SECONDS:
        raise VoiceMiniAppError("Telegram initData has expired", code="telegram_auth_stale")

    try:
        user = json.loads(values.get("user", ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VoiceMiniAppError("Telegram user data is invalid", code="telegram_auth_failed") from exc
    if not isinstance(user, dict) or user.get("id") in (None, ""):
        raise VoiceMiniAppError("Telegram user data is missing", code="telegram_auth_failed")
    user_id = str(user["id"])
    if expected_user_id is not None and not hmac.compare_digest(
        user_id.encode("utf-8"), str(expected_user_id).encode("utf-8")
    ):
        raise VoiceMiniAppError("Telegram user does not match the launch", code="telegram_user_mismatch")
    return {"id": user_id}


def _active_home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def _candidate_homes(api_adapter: Any, runner: Any) -> Iterable[Path]:
    """Yield active/default/profile homes; nonce auth, not URL prefix, selects one."""
    homes: list[Path] = []
    with suppress(Exception):
        homes.append(_active_home())
    with suppress(Exception):
        from hermes_constants import get_default_hermes_root

        homes.append(Path(get_default_hermes_root()))
    profile_maps = getattr(runner, "_profile_adapters", None) or {}
    with suppress(Exception):
        from hermes_cli.profiles import get_profile_dir

        for profile in profile_maps:
            homes.append(Path(get_profile_dir(profile)))
    seen: set[str] = set()
    for home in homes:
        key = str(home.resolve())
        if key not in seen:
            seen.add(key)
            yield home


def _telegram_adapter(runner: Any, profile: str) -> Any:
    from gateway.config import Platform

    resolver = getattr(runner, "_authorization_adapter", None)
    if callable(resolver):
        return resolver(Platform.TELEGRAM, profile)
    adapters = getattr(runner, "adapters", None) or {}
    if profile in {"", "default"}:
        return adapters.get(Platform.TELEGRAM)
    profile_maps = getattr(runner, "_profile_adapters", None) or {}
    return (profile_maps.get(profile) or {}).get(Platform.TELEGRAM)


def _adapter_bot_token(adapter: Any) -> str:
    bot = getattr(adapter, "_bot", None)
    token = getattr(bot, "token", None) if bot is not None else None
    if not token:
        token = getattr(getattr(adapter, "config", None), "token", None)
    return str(token or "").strip()


async def handle_telegram_exchange(request: Any, api_adapter: Any) -> Any:
    """Handle the route-specific Telegram-authenticated exchange.

    This endpoint intentionally does not call ``_check_auth``: Telegram's
    signed ``initData`` plus a single-use receipt is its only authentication
    mechanism.  The route is still bounded, fail-closed, and never accepts
    client-supplied profile/chat/thread/session authority.
    """
    from aiohttp import web

    if request.content_length is not None and request.content_length > VOICE_MINI_APP_MAX_BODY_BYTES:
        return web.json_response({"error": {"message": "Request body too large", "code": "body_too_large"}}, status=413)
    try:
        raw_body = await request.content.read(VOICE_MINI_APP_MAX_BODY_BYTES + 1)
    except Exception:
        return web.json_response({"error": {"message": "Unable to read request body", "code": "invalid_request"}}, status=400)
    if len(raw_body) > VOICE_MINI_APP_MAX_BODY_BYTES:
        return web.json_response({"error": {"message": "Request body too large", "code": "body_too_large"}}, status=413)
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return web.json_response({"error": {"message": "Request body must be JSON", "code": "invalid_json"}}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": {"message": "Request body must be an object", "code": "invalid_request"}}, status=400)
    nonce = body.get("nonce")
    init_data = body.get("init_data", body.get("initData"))
    if not isinstance(nonce, str) or not isinstance(init_data, str):
        return web.json_response({"error": {"message": "nonce and init_data are required", "code": "invalid_request"}}, status=400)

    runner = getattr(api_adapter, "gateway_runner", None) or request.app.get("gateway_runner")
    if runner is None:
        return web.json_response({"error": {"message": "Gateway unavailable", "code": "gateway_unavailable"}}, status=503)
    store = None
    receipt = None
    for home in _candidate_homes(api_adapter, runner):
        candidate = VoiceLaunchStore(home)
        found = await asyncio.to_thread(candidate.lookup, nonce)
        if found is not None:
            store, receipt = candidate, found
            break
    if store is None or receipt is None:
        return web.json_response({"error": {"message": "Launch receipt is invalid or expired", "code": "invalid_receipt"}}, status=401)

    profile = str(receipt.get("profile") or "default")
    adapter = _telegram_adapter(runner, profile)
    token = _adapter_bot_token(adapter) if adapter is not None else ""
    if not token:
        return web.json_response({"error": {"message": "Telegram adapter unavailable", "code": "telegram_unavailable"}}, status=503)
    try:
        verify_telegram_init_data(
            init_data,
            token,
            expected_user_id=str(receipt["user_id"]),
        )
    except VoiceMiniAppError as exc:
        return web.json_response({"error": {"message": str(exc), "code": exc.code}}, status=exc.status)

    consumed = await asyncio.to_thread(store.consume, nonce)
    if consumed is None:
        return web.json_response({"error": {"message": "Launch receipt is invalid, expired, or already used", "code": "receipt_replayed"}}, status=401)

    try:
        with api_adapter._profile_scope(profile):
            session_id = await asyncio.to_thread(
                resolve_current_session,
                consumed,
                runner=runner,
                api_adapter=api_adapter,
            )
    except VoiceMiniAppError as exc:
        return web.json_response({"error": {"message": str(exc), "code": exc.code}}, status=exc.status)
    if not session_id:
        return web.json_response({"error": {"message": "Current Hermes session is unavailable", "code": "session_unavailable"}}, status=404)
    return web.json_response(
        {
            "session_id": session_id,
            "lane": {
                "platform": "telegram",
                "kind": "topic" if consumed.get("thread_id") else "dm",
            },
        }
    )


def resolve_current_session(receipt: dict[str, Any], *, runner: Any, api_adapter: Any) -> Optional[str]:
    """Resolve only the currently active session named by a receipt."""
    from gateway.config import Platform
    from hermes_state import SessionDB

    home = _active_home()
    db = api_adapter._ensure_session_db() if hasattr(api_adapter, "_ensure_session_db") else SessionDB(db_path=home / "state.db")
    if db is None:
        raise VoiceMiniAppError("Session database unavailable", status=503, code="session_db_unavailable")
    chat_id = str(receipt["chat_id"])
    user_id = str(receipt["user_id"])
    thread_id = normalize_thread_id(receipt.get("thread_id"))
    session_key = str(receipt["session_key"])
    is_topic = thread_id is not None
    if is_topic and receipt.get("chat_type") == "dm":
        binding = db.get_telegram_topic_binding(chat_id=chat_id, thread_id=thread_id)
        if not binding or str(binding.get("user_id") or "") != user_id:
            raise VoiceMiniAppError("Current Telegram topic session is unavailable", status=404, code="session_unavailable")
        if str(binding.get("session_key") or "") != session_key:
            raise VoiceMiniAppError("Telegram topic binding no longer matches this launch", status=401, code="session_binding_mismatch")
        session_id = str(binding.get("session_id") or "")
        get_tip = getattr(db, "get_compression_tip", None)
        if session_id and callable(get_tip):
            session_id = str(get_tip(session_id) or session_id)
        row = db.get_session(session_id) if session_id else None
        if not row or row.get("ended_at") is not None:
            raise VoiceMiniAppError("Current Telegram topic session is unavailable", status=404, code="session_unavailable")
        return session_id

    store = getattr(runner, "session_store", None)
    peek = getattr(store, "peek_session_id", None)
    session_id = peek(session_key) if callable(peek) else None
    row = db.get_session(session_id) if session_id else None
    if is_topic:
        row = None
    if not row or row.get("ended_at") is not None or str(row.get("session_key") or "") != session_key or str(row.get("user_id") or "") != user_id:
        session_id = db.find_session_by_origin(
            platform=Platform.TELEGRAM.value,
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
        )
        row = db.get_session(session_id) if session_id else None
    if not row or row.get("ended_at") is not None:
        raise VoiceMiniAppError("Current Telegram session is unavailable", status=404, code="session_unavailable")
    if str(row.get("session_key") or "") != session_key or str(row.get("user_id") or "") != user_id:
        raise VoiceMiniAppError("Current Telegram session no longer matches this launch", status=401, code="session_binding_mismatch")
    return str(row["id"])


async def mint_voice_launch(runner: Any, event: Any, adapter: Any) -> str:
    """Persist a launch receipt and return the configured opaque URL."""
    from hermes_constants import get_hermes_home

    source = getattr(event, "source", event)
    config = getattr(adapter, "config", None)
    extra = getattr(config, "extra", None) or {}
    base_url = str(extra.get("voice_mini_app_url") or "").strip()
    if not base_url:
        raise VoiceMiniAppError(
            "Voice Mini App is not configured. Set platforms.telegram.extra.voice_mini_app_url.",
            code="voice_mini_app_unconfigured",
        )
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", "telegram"))
    thread_id = normalize_thread_id(getattr(source, "thread_id", None))
    profile = str(getattr(source, "profile", None) or "default")
    if profile == "default" and getattr(getattr(runner, "config", None), "multiplex_profiles", False):
        active = getattr(runner, "_active_profile_name", None)
        if callable(active):
            profile = str(active() or "default")
    session_key = runner._session_key_for_source(source)
    nonce = VoiceLaunchStore(get_hermes_home()).create(
        platform=str(platform),
        chat_id=str(getattr(source, "chat_id", "")),
        chat_type=str(getattr(source, "chat_type", "dm") or "dm"),
        thread_id=thread_id,
        user_id=str(getattr(source, "user_id", "")),
        profile=profile,
        session_key=session_key,
    )
    return launcher_url(base_url, nonce)
