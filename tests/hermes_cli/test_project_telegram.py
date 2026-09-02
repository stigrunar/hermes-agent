from __future__ import annotations

import json
import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import outcomes_db as odb
from hermes_cli import projects_db as pdb
from hermes_cli.projects_cmd import projects_command
from hermes_cli.project_telegram import (
    MaterialControlEvent,
    is_material_control_event,
    load_project_registry,
    render_project_card,
    sync_telegram_project,
)


def _registry(*, topic_extra=None):
    control = {
        "display_name": "00 · Prosjekt",
        "thread_id": 4,
        "pinned_message_id": 7,
        "card_source": {
            "project_registry_id": "p_123",
            "repository": "owner/repo",
            "frozen_acceptance": "ACCEPT@1",
        },
    }
    if topic_extra:
        control.update(topic_extra)
    return {
        "schema_version": 1,
        "projects": [
            {
                "project_id": "demo",
                "project_name": "Demo Project",
                "telegram_group": {
                    "display_name": "Demo Group",
                    "chat_id": -100123,
                    "is_forum": True,
                },
                "topics": {
                    "control": control,
                    "inbox": {
                        "display_name": "01 · Innkommende",
                        "thread_id": 1,
                        "telegram_general_topic": True,
                    },
                    "staffing": {
                        "display_name": "Staffing",
                        "thread_id": 9,
                    },
                },
            }
        ],
    }


def _write_registry(tmp_path: Path, payload=None) -> Path:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload or _registry()), encoding="utf-8")
    return path


def test_registry_loads_explicit_topics_and_rejects_mismatch(tmp_path):
    path = _write_registry(tmp_path)
    spec = load_project_registry(
        path, project_ident="demo", canonical_project_id="p_123"
    )
    assert spec.chat_id == -100123
    assert [(t.key, t.lane_kind, t.thread_id) for t in spec.topics] == [
        ("control", "control", "4"),
        ("inbox", "inbox", "1"),
        ("staffing", "workstream", "9"),
    ]
    assert "owner/repo" in render_project_card(spec)
    with pytest.raises(ValueError, match="project mismatch"):
        load_project_registry(
            path, project_ident="demo", canonical_project_id="p_wrong"
        )


def test_registry_rejects_duplicate_keys_unknown_fields_and_inferred_reserved_topics(
    tmp_path,
):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":1,"schema_version":1,"projects":[]}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicate registry key"):
        load_project_registry(duplicate, project_ident="demo")

    bad = _registry(topic_extra={"surprise": True})
    path = _write_registry(tmp_path, bad)
    with pytest.raises(ValueError, match="unknown field"):
        load_project_registry(path, project_ident="demo")

    bad = _registry()
    bad["projects"][0]["topics"]["planning"] = {
        "display_name": "Planning",
        "telegram_general_topic": True,
    }
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="only inbox"):
        load_project_registry(path, project_ident="demo")


@pytest.mark.asyncio
async def test_dry_run_is_deterministic_and_does_not_create_outcomes_db(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    spec = load_project_registry(
        _write_registry(tmp_path), project_ident="demo", canonical_project_id="p_123"
    )
    first = await sync_telegram_project(
        spec=spec, canonical_project_id="p_123", dry_run=True
    )
    second = await sync_telegram_project(
        spec=spec, canonical_project_id="p_123", dry_run=True
    )
    assert first == second
    assert first["dry_run"] is True
    assert not odb.outcomes_db_path().exists()


class FakeTelegram:
    def __init__(self, card_text: str):
        self.calls = []
        self.chat = SimpleNamespace(
            is_forum=True,
            title="Demo Group",
            pinned_message=SimpleNamespace(message_id=7, text=card_text),
        )

    async def get_chat(self, chat_id):
        self.calls.append(("get_chat", chat_id))
        return self.chat

    async def get_me(self):
        return SimpleNamespace(id=88)

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(
            status="administrator", can_manage_topics=True, can_pin_messages=True
        )

    async def edit_general_forum_topic(self, chat_id, *, name):
        self.calls.append(("edit_general", chat_id, name))

    async def create_forum_topic(self, chat_id, *, name):
        self.calls.append(("create_topic", chat_id, name))
        return SimpleNamespace(message_thread_id=21)

    async def edit_message_text(self, text, *, chat_id, message_id):
        self.calls.append(("edit_card", chat_id, message_id))
        self.chat.pinned_message.text = text


class AlreadyNamedTelegram(FakeTelegram):
    async def edit_general_forum_topic(self, chat_id, *, name):
        self.calls.append(("edit_general", chat_id, name))
        raise Exception("Topic_not_modified")


class MissingPinnedTelegram(FakeTelegram):
    def __init__(self, card_text: str):
        super().__init__(card_text)
        self.chat.pinned_message = None
        self.edit_count = 0

    async def edit_message_text(self, text, *, chat_id, message_id):
        self.calls.append(("edit_card", chat_id, message_id))
        self.edit_count += 1
        if self.edit_count > 1:
            raise Exception("Message_not_modified")

    async def pin_chat_message(self, chat_id, message_id, *, disable_notification):
        self.calls.append(("pin_card", chat_id, message_id))

    async def send_message(
        self, chat_id, text, *, message_thread_id, disable_web_page_preview
    ):
        self.calls.append(("send_card", chat_id, message_thread_id))
        return SimpleNamespace(message_id=99)


@pytest.mark.asyncio
async def test_apply_rejects_nonmatching_registered_pinned_message(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    spec = load_project_registry(
        _write_registry(tmp_path), project_ident="demo", canonical_project_id="p_123"
    )
    api = FakeTelegram(render_project_card(spec))
    api.chat.pinned_message.message_id = 8

    with pytest.raises(ValueError, match="registered project card 7"):
        await sync_telegram_project(spec=spec, canonical_project_id="p_123", api=api)


@pytest.mark.asyncio
async def test_apply_repairs_missing_pinned_message_without_creating_card(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    spec = load_project_registry(
        _write_registry(tmp_path), project_ident="demo", canonical_project_id="p_123"
    )
    api = MissingPinnedTelegram(render_project_card(spec))

    first = await sync_telegram_project(
        spec=spec, canonical_project_id="p_123", api=api
    )
    second = await sync_telegram_project(
        spec=spec, canonical_project_id="p_123", api=api
    )

    assert [call[0] for call in api.calls if call[0] in {"edit_card", "pin_card"}] == [
        "edit_card",
        "pin_card",
        "edit_card",
        "pin_card",
    ]
    assert [call for call in api.calls if call[0] == "edit_card"] == [
        ("edit_card", -100123, 7),
        ("edit_card", -100123, 7),
    ]
    assert [call for call in api.calls if call[0] == "pin_card"] == [
        ("pin_card", -100123, 7),
        ("pin_card", -100123, 7),
    ]
    assert not [call for call in api.calls if call[0] in {"send_card", "create_topic"}]
    assert any(action["action"] == "edited_project_card" for action in first["actions"])
    assert any(
        action["action"] == "reused_project_card" for action in second["actions"]
    )
    assert first["registry_updates"] == second["registry_updates"] == []


@pytest.mark.asyncio
async def test_apply_binds_verified_ids_and_second_sync_has_no_telegram_writes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    spec = load_project_registry(
        _write_registry(tmp_path), project_ident="demo", canonical_project_id="p_123"
    )
    api = FakeTelegram(render_project_card(spec))

    first = await sync_telegram_project(
        spec=spec, canonical_project_id="p_123", api=api
    )
    assert [call for call in api.calls if call[0] == "edit_general"] == [
        ("edit_general", -100123, "01 · Innkommende")
    ]
    assert not [call for call in api.calls if call[0] in {"create_topic", "edit_card"}]
    with odb.connect_closing() as conn:
        lanes = odb.list_conversation_lanes(conn, "p_123")
    assert {(lane.thread_id, lane.lane_kind) for lane in lanes} == {
        ("4", "control"),
        ("1", "inbox"),
        ("9", "workstream"),
    }
    before = list(api.calls)
    second = await sync_telegram_project(
        spec=spec, canonical_project_id="p_123", api=api
    )
    assert api.calls[len(before) :] == [
        ("get_chat", -100123),
    ]
    assert any(
        action["action"] == "reused_project_card" for action in second["actions"]
    )
    assert not first["registry_updates"]


@pytest.mark.asyncio
async def test_apply_creates_only_explicit_missing_topic_and_returns_registry_update(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    payload = _registry()
    payload["projects"][0]["topics"]["staffing"].pop("thread_id")
    spec = load_project_registry(
        _write_registry(tmp_path, payload),
        project_ident="demo",
        canonical_project_id="p_123",
    )
    api = FakeTelegram(render_project_card(spec))
    result = await sync_telegram_project(
        spec=spec, canonical_project_id="p_123", api=api
    )
    assert [call for call in api.calls if call[0] == "create_topic"] == [
        ("create_topic", -100123, "Staffing")
    ]
    assert result["registry_updates"] == [
        {"project_id": "demo", "topic": "staffing", "thread_id": 21}
    ]


@pytest.mark.asyncio
async def test_apply_treats_telegram_topic_not_modified_as_idempotent(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    spec = load_project_registry(
        _write_registry(tmp_path), project_ident="demo", canonical_project_id="p_123"
    )
    api = AlreadyNamedTelegram(render_project_card(spec))
    result = await sync_telegram_project(
        spec=spec, canonical_project_id="p_123", api=api
    )
    assert any(action["action"] == "reused_general" for action in result["actions"])
    with odb.connect_closing() as conn:
        assert len(odb.list_conversation_lanes(conn, "p_123")) == 3


def test_material_control_event_filter_rejects_operational_noise():
    assert all(is_material_control_event(item) for item in MaterialControlEvent)
    for noise in (
        "worker_started",
        "kanban_update",
        "tool_call",
        "test_passed",
        "qa_note",
        "migration",
        "bot_admin",
    ):
        assert not is_material_control_event(noise)


def test_telegram_sync_cli_dry_run_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with pdb.connect_closing() as conn:
        project_id = pdb.create_project(conn, name="Demo Project", slug="demo")
    payload = _registry()
    payload["projects"][0]["topics"]["control"]["card_source"][
        "project_registry_id"
    ] = project_id
    registry = _write_registry(tmp_path, payload)
    rc = projects_command(
        argparse.Namespace(
            project_action="telegram-sync",
            project="demo",
            registry=str(registry),
            dry_run=True,
            as_json=True,
        )
    )
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["project_id"] == project_id
    assert result["chat_id"] == -100123
