"""Registry-driven Telegram projection for first-class Hermes Projects.

Telegram groups/topics are conversation lanes. Project/Outcome state remains
canonical in the existing project and outcomes stores; this module only
provisions and refreshes the Telegram projection.
"""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_cli import outcomes_db as odb

_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TOPIC_FIELDS = {
    "display_name",
    "thread_id",
    "pinned_message_id",
    "card_source",
    "telegram_general_topic",
    "outcome_id",
    "lane_kind",
}
_RESERVED_KINDS = {"control", "inbox", "ops"}


class MaterialControlEvent(str, Enum):
    ESTABLISHMENT = "establishment"
    GOAL_SCOPE_ACCEPTANCE_CHANGE = "goal_scope_acceptance_change"
    CROSS_TOPIC_DECISION = "cross_topic_decision"
    STIG_REQUIRED_BLOCKER = "stig_required_blocker"
    CROSS_TOPIC_REPLAN = "cross_topic_replan"
    LIFECYCLE_TRANSITION = "lifecycle_transition"
    MAJOR_WORK_AREA_CLOSEOUT = "major_work_area_closeout"


def is_material_control_event(value: object) -> bool:
    """Return true only for frozen project-level control-surface events."""
    token = value.value if isinstance(value, MaterialControlEvent) else str(value or "")
    return token in {item.value for item in MaterialControlEvent}


@dataclass(frozen=True)
class RegistryTopic:
    key: str
    display_name: str
    thread_id: Optional[str]
    lane_kind: str
    outcome_id: Optional[str] = None
    pinned_message_id: Optional[int] = None
    card_source: Optional[Mapping[str, Any]] = None
    telegram_general_topic: bool = False


@dataclass(frozen=True)
class TelegramProjectSpec:
    registry_path: Path
    registry_project_id: str
    project_name: str
    chat_id: int
    group_name: str
    is_forum: bool
    topics: tuple[RegistryTopic, ...]

    @property
    def control(self) -> RegistryTopic:
        return next(topic for topic in self.topics if topic.key == "control")


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate registry key: {key}")
        result[key] = value
    return result


def _required_text(value: object, field: str, *, max_chars: int = 512) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    if len(text) > max_chars:
        raise ValueError(f"{field} exceeds {max_chars} characters")
    return text


def _optional_positive_int(value: object, field: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a positive integer") from None
    if number <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return number


def load_project_registry(
    path: str | Path,
    *,
    project_ident: str,
    canonical_project_id: Optional[str] = None,
) -> TelegramProjectSpec:
    """Load and strictly validate one explicit project from the registry."""
    registry_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(
            registry_path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicates,
        )
    except OSError as exc:
        raise ValueError(f"cannot read Telegram project registry: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid Telegram project registry JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Telegram project registry schema_version must be 1")
    projects = payload.get("projects")
    if not isinstance(projects, list):
        raise ValueError("Telegram project registry projects must be a list")
    ident = _required_text(project_ident, "project identifier", max_chars=256)
    matches = [
        row
        for row in projects
        if isinstance(row, dict)
        and ident
        in {
            str(row.get("project_id") or ""),
            str(
                (row.get("topics") or {})
                .get("control", {})
                .get("card_source", {})
                .get("project_registry_id")
                or ""
            ),
        }
    ]
    if len(matches) != 1:
        raise ValueError(
            f"registry must contain exactly one project matching {ident!r}"
        )
    row = matches[0]
    registry_project_id = _required_text(
        row.get("project_id"), "project_id", max_chars=256
    )
    control_source = ((row.get("topics") or {}).get("control") or {}).get(
        "card_source"
    ) or {}
    declared_canonical = str(control_source.get("project_registry_id") or "").strip()
    if (
        canonical_project_id
        and declared_canonical
        and declared_canonical != canonical_project_id
    ):
        raise ValueError(
            f"registry project mismatch: expected canonical id {canonical_project_id}, got {declared_canonical}"
        )

    group = row.get("telegram_group")
    if not isinstance(group, dict):
        raise ValueError("telegram_group must be an object")
    try:
        chat_id = int(str(group.get("chat_id")))
    except (TypeError, ValueError):
        raise ValueError("telegram_group.chat_id must be numeric") from None
    if chat_id >= 0:
        raise ValueError("telegram_group.chat_id must be a negative supergroup id")
    is_forum = group.get("is_forum") is True
    if not is_forum:
        raise ValueError("telegram_group.is_forum must be true")

    raw_topics = row.get("topics")
    if not isinstance(raw_topics, dict):
        raise ValueError("topics must be an object")
    if "control" not in raw_topics or "inbox" not in raw_topics:
        raise ValueError("topics must explicitly contain control and inbox")
    topics: list[RegistryTopic] = []
    for key, raw in raw_topics.items():
        if not _KEY_RE.fullmatch(str(key)):
            raise ValueError(f"invalid topic key: {key!r}")
        if not isinstance(raw, dict):
            raise ValueError(f"topic {key!r} must be an object")
        unknown = sorted(set(raw) - _TOPIC_FIELDS)
        if unknown:
            raise ValueError(
                f"topic {key!r} has unknown field(s): {', '.join(unknown)}"
            )
        name = _required_text(
            raw.get("display_name")
            or (
                "00 · Prosjekt"
                if key == "control"
                else "01 · Innkommende"
                if key == "inbox"
                else None
            ),
            f"topics.{key}.display_name",
            max_chars=128,
        )
        general = raw.get("telegram_general_topic") is True
        if general != (key == "inbox"):
            raise ValueError(
                "only inbox may be the Telegram General topic, and inbox must declare it"
            )
        lane_kind = (
            str(
                raw.get("lane_kind")
                or (key if key in _RESERVED_KINDS else "workstream")
            )
            .strip()
            .lower()
        )
        expected_kind = key if key in _RESERVED_KINDS else "workstream"
        if lane_kind != expected_kind:
            raise ValueError(f"topic {key!r} must use lane_kind {expected_kind!r}")
        thread_num = _optional_positive_int(
            raw.get("thread_id"), f"topics.{key}.thread_id"
        )
        if general and thread_num not in {None, 1}:
            raise ValueError("Telegram General/inbox thread_id must be 1")
        card_source = raw.get("card_source")
        if key == "control" and not isinstance(card_source, dict):
            raise ValueError("control.card_source must be an object")
        if key != "control" and ("pinned_message_id" in raw or "card_source" in raw):
            raise ValueError(
                "pinned_message_id/card_source are allowed only on control"
            )
        outcome_id = str(raw.get("outcome_id") or "").strip() or None
        if key in _RESERVED_KINDS and outcome_id:
            raise ValueError(f"reserved topic {key!r} cannot bind an outcome")
        topics.append(
            RegistryTopic(
                key=str(key),
                display_name=name,
                thread_id=str(thread_num) if thread_num is not None else None,
                lane_kind=lane_kind,
                outcome_id=outcome_id,
                pinned_message_id=_optional_positive_int(
                    raw.get("pinned_message_id"), f"topics.{key}.pinned_message_id"
                ),
                card_source=card_source,
                telegram_general_topic=general,
            )
        )
    if len({topic.display_name for topic in topics}) != len(topics):
        raise ValueError("topic display names must be unique inside one project")
    return TelegramProjectSpec(
        registry_path=registry_path,
        registry_project_id=registry_project_id,
        project_name=_required_text(row.get("project_name"), "project_name"),
        chat_id=chat_id,
        group_name=_required_text(
            group.get("display_name"), "telegram_group.display_name"
        ),
        is_forum=is_forum,
        topics=tuple(topics),
    )


def render_project_card(spec: TelegramProjectSpec) -> str:
    """Render the single deterministic card from registry-declared pointers/state."""
    source = dict(spec.control.card_source or {})
    labels = {
        "project_registry_id": "Prosjekt-ID",
        "status": "Status",
        "attention": "Oppmerksomhet",
        "lifecycle": "Livsløp",
        "outcome_owner": "Outcome-eier",
        "active_executor": "Aktiv utfører",
        "next_action": "Neste handling",
        "stig_blocker": "Stig-blokkering",
        "repository": "Repository",
        "roadmap_spec": "Roadmap/spec",
        "kanban_board": "Kanban",
        "frozen_acceptance": "Frosset acceptance",
        "active_work_areas": "Aktive arbeidsområder",
        "last_accepted_delivery": "Siste godkjente leveranse",
    }
    lines = [f"📌 {spec.project_name}", "", "Canonical prosjektprojeksjon"]
    for key, label in labels.items():
        value = source.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        lines.append(f"{label}: {value}")
    lines.extend([
        "",
        f"Kontrollflate: {spec.control.display_name}",
        "Arbeidslogg, tester og QA hører hjemme i relevant workstream-topic.",
    ])
    text = "\n".join(lines)
    if len(text) > 4096:
        raise ValueError(
            "rendered project card exceeds Telegram's 4096 character limit"
        )
    return text


def _lane_for_key(conn, *, project_id: str, chat_id: str, topic: RegistryTopic):
    for lane in odb.list_conversation_lanes(conn, project_id):
        if lane.platform != "telegram" or lane.chat_id != chat_id:
            continue
        if lane.lane_kind != topic.lane_kind:
            continue
        if lane.label == topic.display_name:
            return lane
    return None


def _dry_run_state(
    *, project_id: str, chat_id: str, topics: tuple[RegistryTopic, ...]
) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve state read-only without creating or migrating an outcomes DB."""
    path = odb.outcomes_db_path()
    if not path.exists():
        if any(topic.outcome_id for topic in topics):
            raise ValueError(
                "outcomes.db is required to validate registry outcome bindings"
            )
        return {}, {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        lanes: dict[str, str] = {}
        outcomes: dict[str, str] = {}
        for topic in topics:
            if topic.outcome_id:
                outcome = odb.get_outcome(conn, topic.outcome_id, project_id=project_id)
                if outcome is None:
                    raise ValueError(
                        f"topic {topic.key!r} outcome does not resolve inside project: {topic.outcome_id}"
                    )
                outcomes[topic.key] = outcome.id
            lane = _lane_for_key(
                conn, project_id=project_id, chat_id=chat_id, topic=topic
            )
            if lane is not None:
                lanes[topic.key] = lane.thread_id
        return lanes, outcomes
    finally:
        conn.close()


async def _call(value):
    return await value if inspect.isawaitable(value) else value


async def sync_telegram_project(
    *,
    spec: TelegramProjectSpec,
    canonical_project_id: str,
    api: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Sync one project projection; dry-run performs no API or DB mutations."""
    project_id = _required_text(
        canonical_project_id, "canonical_project_id", max_chars=256
    )
    chat = str(spec.chat_id)
    planned: list[dict[str, Any]] = []
    if dry_run:
        existing_threads, resolved_outcomes = _dry_run_state(
            project_id=project_id, chat_id=chat, topics=spec.topics
        )
        for topic in spec.topics:
            thread_id = (
                topic.thread_id
                or existing_threads.get(topic.key)
                or ("1" if topic.telegram_general_topic else None)
            )
            if topic.telegram_general_topic and topic.key not in existing_threads:
                planned.append({"action": "rename_general", "name": topic.display_name})
            elif not thread_id:
                planned.append({
                    "action": "create_topic",
                    "key": topic.key,
                    "name": topic.display_name,
                })
            planned.append({
                "action": "bind_lane",
                "key": topic.key,
                "thread_id": thread_id,
                "lane_kind": topic.lane_kind,
                "outcome_id": resolved_outcomes.get(topic.key),
            })
        render_project_card(spec)
        planned.append({
            "action": "upsert_project_card",
            "thread_id": spec.control.thread_id,
            "message_id": spec.control.pinned_message_id,
        })
        return {
            "project_id": project_id,
            "registry_project_id": spec.registry_project_id,
            "chat_id": spec.chat_id,
            "dry_run": True,
            "actions": planned,
            "registry_updates": [],
        }
    resolved: list[tuple[RegistryTopic, Optional[str], Optional[str]]] = []
    with odb.connect_closing() as conn:
        for topic in spec.topics:
            outcome_id = None
            if topic.outcome_id:
                outcome = odb.get_outcome(conn, topic.outcome_id, project_id=project_id)
                if outcome is None:
                    raise ValueError(
                        f"topic {topic.key!r} outcome does not resolve inside project: {topic.outcome_id}"
                    )
                outcome_id = outcome.id
            lane = _lane_for_key(conn, project_id=project_id, chat_id=chat, topic=topic)
            thread_id = topic.thread_id or (lane.thread_id if lane else None)
            if topic.telegram_general_topic:
                thread_id = thread_id or "1"
                if lane is None:
                    planned.append({
                        "action": "rename_general",
                        "name": topic.display_name,
                    })
            elif not thread_id:
                planned.append({
                    "action": "create_topic",
                    "key": topic.key,
                    "name": topic.display_name,
                })
            planned.append({
                "action": "bind_lane",
                "key": topic.key,
                "thread_id": thread_id,
                "lane_kind": topic.lane_kind,
                "outcome_id": outcome_id,
            })
            resolved.append((topic, outcome_id, thread_id))

    card_text = render_project_card(spec)
    planned.append({
        "action": "upsert_project_card",
        "thread_id": spec.control.thread_id,
        "message_id": spec.control.pinned_message_id,
    })
    if api is None:
        raise ValueError("Telegram API adapter is required unless --dry-run is used")

    chat_info = await _call(api.get_chat(spec.chat_id))
    if not bool(getattr(chat_info, "is_forum", False)):
        raise ValueError("target Telegram chat is not a forum")
    actual_title = str(getattr(chat_info, "title", "") or "")
    if actual_title and actual_title != spec.group_name:
        raise ValueError(
            f"Telegram group title mismatch: expected {spec.group_name!r}, got {actual_title!r}"
        )
    me = await _call(api.get_me())
    member = await _call(api.get_chat_member(spec.chat_id, getattr(me, "id")))
    if str(getattr(member, "status", "")) not in {"administrator", "creator"}:
        raise ValueError("configured Telegram bot is not an administrator")
    if not bool(getattr(member, "can_manage_topics", False)):
        raise ValueError("configured Telegram bot lacks can_manage_topics")
    if not bool(getattr(member, "can_pin_messages", False)):
        raise ValueError("configured Telegram bot lacks can_pin_messages")

    pinned = getattr(chat_info, "pinned_message", None)
    pinned_id = getattr(pinned, "message_id", None)
    pinned_text = str(getattr(pinned, "text", "") or "")
    expected_id = spec.control.pinned_message_id
    if expected_id and pinned_id != expected_id:
        raise ValueError(
            f"registered project card {expected_id} is not the chat's pinned message ({pinned_id})"
        )

    actions: list[dict[str, Any]] = []
    registry_updates: list[dict[str, Any]] = []
    resolved_threads: dict[str, str] = {}
    with odb.connect_closing() as conn:
        for topic, outcome_id, thread_id in resolved:
            lane = _lane_for_key(conn, project_id=project_id, chat_id=chat, topic=topic)
            if topic.telegram_general_topic and lane is None:
                await _call(
                    api.edit_general_forum_topic(spec.chat_id, name=topic.display_name)
                )
                actions.append({
                    "action": "renamed_general",
                    "name": topic.display_name,
                })
            if not thread_id:
                created = await _call(
                    api.create_forum_topic(spec.chat_id, name=topic.display_name)
                )
                thread_id = str(getattr(created, "message_thread_id", "") or "").strip()
                if not thread_id:
                    raise RuntimeError(
                        f"Telegram did not return a thread id for topic {topic.display_name!r}"
                    )
                actions.append({
                    "action": "created_topic",
                    "key": topic.key,
                    "thread_id": thread_id,
                })
                registry_updates.append({
                    "project_id": spec.registry_project_id,
                    "topic": topic.key,
                    "thread_id": int(thread_id),
                })
            if (
                lane is not None
                and lane.thread_id == str(thread_id)
                and lane.outcome_id == outcome_id
            ):
                resolved_threads[topic.key] = str(thread_id)
                actions.append({
                    "action": "reused_lane",
                    "key": topic.key,
                    "lane_id": lane.id,
                    "thread_id": str(thread_id),
                })
                continue
            lane_id = odb.bind_conversation_lane(
                conn,
                project_id=project_id,
                outcome_id=outcome_id,
                platform="telegram",
                chat_id=chat,
                thread_id=thread_id,
                label=topic.display_name,
                lane_kind=topic.lane_kind,
            )
            resolved_threads[topic.key] = str(thread_id)
            actions.append({
                "action": "bound_lane",
                "key": topic.key,
                "lane_id": lane_id,
                "thread_id": thread_id,
            })

    if expected_id:
        if pinned_text != card_text:
            await _call(
                api.edit_message_text(
                    card_text, chat_id=spec.chat_id, message_id=expected_id
                )
            )
            actions.append({"action": "edited_project_card", "message_id": expected_id})
        else:
            actions.append({"action": "reused_project_card", "message_id": expected_id})
    else:
        sent = await _call(
            api.send_message(
                spec.chat_id,
                card_text,
                message_thread_id=int(resolved_threads["control"]),
                disable_web_page_preview=True,
            )
        )
        message_id = int(getattr(sent, "message_id", 0) or 0)
        if not message_id:
            raise RuntimeError(
                "Telegram did not return a message id for the project card"
            )
        await _call(
            api.pin_chat_message(spec.chat_id, message_id, disable_notification=True)
        )
        actions.append({"action": "created_project_card", "message_id": message_id})
        registry_updates.append({
            "project_id": spec.registry_project_id,
            "topic": "control",
            "pinned_message_id": message_id,
        })
    return {
        "project_id": project_id,
        "registry_project_id": spec.registry_project_id,
        "chat_id": spec.chat_id,
        "dry_run": False,
        "actions": actions,
        "registry_updates": registry_updates,
    }


async def sync_telegram_project_with_configured_bot(**kwargs) -> dict[str, Any]:
    """Run sync with the profile-scoped Telegram secret without exposing it."""
    from agent.secret_scope import get_secret

    token = str(get_secret("TELEGRAM_BOT_TOKEN", "") or "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    try:
        from telegram import Bot
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("python-telegram-bot is unavailable") from exc
    async with Bot(token=token) as bot:
        return await sync_telegram_project(api=bot, **kwargs)
