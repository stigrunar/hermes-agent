"""Server-authoritative delivery for Desktop continuations of messaging sessions."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_TELEGRAM_FORUM_CHAT_TYPES = frozenset({"group", "supergroup"})


def _routing_id(value: Any) -> Optional[str]:
    """Normalize a persisted Telegram id without accepting container values."""
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip()
    return normalized or None


def _telegram_origin(db: Any, session_key: str) -> Optional[dict[str, str | None]]:
    """Return a validated Telegram DM/topic origin from state.db, or ``None``."""
    if db is None or not session_key:
        return None
    rows = db.get_gateway_session_metadata([session_key])
    row = rows.get(session_key) if isinstance(rows, dict) else None
    if not isinstance(row, dict):
        return None
    row_platform = str(row.get("source") or "").strip().casefold()
    row_chat_type = str(row.get("chat_type") or "").strip().casefold()
    if row_platform != "telegram":
        return None
    if row_chat_type != "dm" and row_chat_type not in _TELEGRAM_FORUM_CHAT_TYPES:
        return None
    chat_id = _routing_id(row.get("chat_id"))
    if not chat_id:
        return None
    try:
        origin = json.loads(row.get("origin_json") or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(origin, dict):
        return None
    origin_platform = str(origin.get("platform") or "").strip().casefold()
    origin_chat_type = str(origin.get("chat_type") or "").strip().casefold()
    if origin_platform != "telegram" or origin_chat_type != row_chat_type:
        return None
    if origin_chat_type != "dm" and origin_chat_type not in _TELEGRAM_FORUM_CHAT_TYPES:
        return None
    # Both values are server-persisted, but requiring agreement detects stale
    # or partially migrated routing metadata instead of guessing a recipient.
    if _routing_id(origin.get("chat_id")) != chat_id:
        return None

    raw_thread_id = row.get("thread_id")
    thread_id = _routing_id(raw_thread_id)
    if raw_thread_id not in (None, "") and thread_id is None:
        return None
    origin_thread_id = _routing_id(origin.get("thread_id"))
    if origin.get("thread_id") not in (None, "") and origin_thread_id is None:
        return None
    if origin_thread_id is not None and origin_thread_id != thread_id:
        return None
    # A Telegram group is not a safe delivery target by itself. Only a
    # persisted forum-topic lane may authorize a resumed response.
    if row_chat_type in _TELEGRAM_FORUM_CHAT_TYPES and thread_id is None:
        return None

    return {
        "chat_id": chat_id,
        "thread_id": thread_id,
    }


@contextmanager
def _profile_scope(profile_home: Optional[str]):
    """Bind one profile's home and secrets for config load and send."""
    home_token = None
    secret_token = None
    try:
        if profile_home:
            from agent.secret_scope import build_profile_secret_scope, set_secret_scope
            from hermes_constants import set_hermes_home_override

            home_token = set_hermes_home_override(profile_home)
            secret_token = set_secret_scope(build_profile_secret_scope(Path(profile_home)))
        yield
    finally:
        try:
            if secret_token is not None:
                from agent.secret_scope import reset_secret_scope

                reset_secret_scope(secret_token)
        finally:
            if home_token is not None:
                from hermes_constants import reset_hermes_home_override

                reset_hermes_home_override(home_token)


def _load_telegram_sender(
    _profile_home: Optional[str],
) -> tuple[Any, Callable[..., Any]]:
    """Resolve the current profile's config and registered standalone sender."""
    from gateway.config import Platform, load_gateway_config
    from gateway.platform_registry import platform_registry

    config = load_gateway_config()
    platform_config = config.platforms.get(Platform.TELEGRAM)
    entry = platform_registry.get("telegram")
    sender = getattr(entry, "standalone_sender_fn", None) if entry is not None else None
    if platform_config is None or not callable(sender):
        raise RuntimeError("telegram standalone sender unavailable")
    return platform_config, sender


def deliver_resumed_telegram_response(
    *,
    db: Any,
    session_key: str,
    response: str,
    explicitly_resumed_from_authoritative_ui: bool,
    profile_home: Optional[str] = None,
    ledger: Any = None,
    sender_loader: Optional[
        Callable[[Optional[str]], tuple[Any, Callable[..., Any]]]
    ] = None,
) -> Optional[dict[str, str]]:
    """Deliver one persisted Desktop continuation response to its Telegram origin.

    Ineligible and legacy sessions return ``None``.  Eligible sessions return
    a renderer-safe receipt containing no routing identifiers or credentials.
    """
    if (
        not explicitly_resumed_from_authoritative_ui
        or not isinstance(response, str)
        or not response.strip()
    ):
        return None

    origin = _telegram_origin(db, session_key)
    if origin is None:
        return None
    assistant_row_id = db.latest_message_row_id(session_key, role="assistant")
    if assistant_row_id is None:
        return {
            "platform": "telegram",
            "status": "failed",
            "error": "delivery_not_tracked",
        }
    message_ref = f"assistant-row:{int(assistant_row_id)}"

    if ledger is None:
        from gateway import delivery_ledger as ledger
    try:
        if not ledger.ledger_enabled():
            return {
                "platform": "telegram",
                "status": "failed",
                "error": "delivery_not_tracked",
            }
        obligation_id = ledger.compute_obligation_id(session_key, message_ref, response)
        created, state = ledger.reserve_obligation(
            obligation_id=obligation_id,
            session_key=session_key,
            platform="telegram",
            chat_id=origin["chat_id"],
            thread_id=origin["thread_id"],
            content=response,
        )
    except Exception:
        logger.warning("Authoritative Telegram delivery could not reserve obligation")
        return {
            "platform": "telegram",
            "status": "failed",
            "error": "delivery_not_tracked",
        }

    if not created:
        if state == "delivered":
            return {"platform": "telegram", "status": "delivered"}
        return {"platform": "telegram", "status": "failed", "error": "delivery_pending"}

    try:
        ledger.mark_attempting(obligation_id)
        with _profile_scope(profile_home):
            platform_config, sender = (sender_loader or _load_telegram_sender)(
                profile_home
            )
            result = asyncio.run(
                sender(
                    platform_config,
                    origin["chat_id"],
                    response,
                    thread_id=origin["thread_id"],
                )
            )
        success = bool(
            result.get("success")
            if isinstance(result, Mapping)
            else getattr(result, "success", False)
        )
    except Exception:
        success = False

    try:
        if success:
            ledger.mark_delivered(obligation_id)
        else:
            ledger.mark_failed(obligation_id, "standalone telegram delivery failed")
    except Exception:
        logger.warning(
            "Authoritative Telegram delivery ledger update failed for obligation %s",
            obligation_id,
            exc_info=False,
        )
        if success:
            return {
                "platform": "telegram",
                "status": "failed",
                "error": "delivery_unconfirmed",
            }

    if success:
        return {"platform": "telegram", "status": "delivered"}
    return {"platform": "telegram", "status": "failed", "error": "delivery_failed"}
