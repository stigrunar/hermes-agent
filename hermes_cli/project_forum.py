"""Project conversation provisioning helpers.

The Telegram Bot API can create forum *topics* in an existing forum supergroup,
but a bot cannot create the supergroup itself.  This module therefore treats
``chat_id`` as a pre-existing operator-created forum and binds every provisioned
topic to the root-shared Project/Outcome conversation-lane store.

The pure async provisioner accepts an injected ``create_topic`` callable so the
coordination contract can be tested without network access or credentials.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Optional

from hermes_cli import outcomes_db as odb


@dataclass(frozen=True)
class TopicSpec:
    name: str
    outcome_id: Optional[str] = None
    lane_kind: str = "workstream"


CreateTopic = Callable[[str], Awaitable[str | int | None] | str | int | None]


def _clean_name(value: object) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("topic name is required")
    if len(name) > 128:
        raise ValueError("topic name exceeds 128 characters")
    return name


def _resolve_outcome(conn, *, project_id: str, token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    outcome = odb.get_outcome(conn, token, project_id=project_id)
    if outcome is None:
        raise ValueError(f"outcome does not resolve inside project: {token}")
    return outcome.id


def _existing_topic_lane(
    conn,
    *,
    project_id: str,
    chat_id: str,
    name: str,
    outcome_id: Optional[str],
    lane_kind: str,
):
    for lane in odb.list_conversation_lanes(conn, project_id):
        if lane.platform != "telegram" or lane.chat_id != chat_id:
            continue
        if lane.label != name or lane.lane_kind != lane_kind:
            continue
        if lane.outcome_id != outcome_id:
            continue
        if lane.thread_id:
            return lane
    return None


async def provision_telegram_topics(
    *,
    project_id: str,
    chat_id: str | int,
    topics: Iterable[TopicSpec],
    create_topic: CreateTopic,
) -> list[dict]:
    """Create/bind missing topics in one existing Telegram forum group.

    Existing exact Project/Outcome lane bindings are reused without another API
    call.  A failed topic creation never writes a guessed thread id.
    """
    project_id = str(project_id or "").strip()
    if not project_id:
        raise ValueError("project_id is required")
    chat = str(chat_id or "").strip()
    if not chat:
        raise ValueError("chat_id is required")

    results: list[dict] = []
    with odb.connect_closing() as conn:
        for raw_spec in topics:
            name = _clean_name(raw_spec.name)
            lane_kind = str(raw_spec.lane_kind or "workstream").strip() or "workstream"
            outcome_id = _resolve_outcome(
                conn, project_id=project_id, token=raw_spec.outcome_id
            )
            existing = _existing_topic_lane(
                conn,
                project_id=project_id,
                chat_id=chat,
                name=name,
                outcome_id=outcome_id,
                lane_kind=lane_kind,
            )
            if existing is not None:
                results.append({"created": False, "lane": existing.to_dict()})
                continue

            created = create_topic(name)
            if inspect.isawaitable(created):
                created = await created
            thread_id = str(created or "").strip()
            if not thread_id:
                raise RuntimeError(f"Telegram did not return a thread id for topic {name!r}")
            lane_id = odb.bind_conversation_lane(
                conn,
                project_id=project_id,
                outcome_id=outcome_id,
                platform="telegram",
                chat_id=chat,
                thread_id=thread_id,
                label=name,
                lane_kind=lane_kind,
            )
            lane = next(
                lane
                for lane in odb.list_conversation_lanes(conn, project_id)
                if lane.id == lane_id
            )
            results.append({"created": True, "lane": lane.to_dict()})
    return results


async def provision_telegram_topics_with_configured_bot(
    *,
    project_id: str,
    chat_id: str | int,
    topics: Iterable[TopicSpec],
) -> list[dict]:
    """Provision topics using the configured Telegram bot secret.

    The token is resolved through the normal secret scope and is never returned
    or persisted by this function.
    """
    from agent.secret_scope import get_secret

    token = str(get_secret("TELEGRAM_BOT_TOKEN", "") or "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    try:
        from telegram import Bot
    except Exception as exc:  # pragma: no cover - installation packaging guard
        raise RuntimeError("python-telegram-bot is unavailable") from exc

    try:
        numeric_chat_id = int(str(chat_id))
    except (TypeError, ValueError):
        raise ValueError("Telegram chat_id must be numeric") from None

    async with Bot(token=token) as bot:
        async def _create(name: str):
            topic = await bot.create_forum_topic(chat_id=numeric_chat_id, name=name)
            return getattr(topic, "message_thread_id", None)

        return await provision_telegram_topics(
            project_id=project_id,
            chat_id=numeric_chat_id,
            topics=topics,
            create_topic=_create,
        )
