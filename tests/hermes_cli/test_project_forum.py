from __future__ import annotations

import pytest

from hermes_cli import outcomes_db as odb
from hermes_cli.project_forum import TopicSpec, provision_telegram_topics


@pytest.mark.asyncio
async def test_provision_topics_creates_and_binds_then_reuses(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with odb.connect_closing() as conn:
        staffing = odb.create_outcome(
            conn, project_id="p_ps", outcome_key="STAFFING-R1"
        )

    calls: list[str] = []

    async def create_topic(name: str):
        calls.append(name)
        return {"Control / status": 100, "Bemanning": 101}[name]

    specs = [
        TopicSpec("Control / status", lane_kind="control"),
        TopicSpec("Bemanning", outcome_id=staffing),
    ]
    first = await provision_telegram_topics(
        project_id="p_ps", chat_id=-100123, topics=specs, create_topic=create_topic
    )
    assert calls == ["Control / status", "Bemanning"]
    assert [item["created"] for item in first] == [True, True]
    assert first[0]["lane"]["thread_id"] == "100"
    assert first[1]["lane"]["outcome_id"] == staffing

    second = await provision_telegram_topics(
        project_id="p_ps", chat_id=-100123, topics=specs, create_topic=create_topic
    )
    assert calls == ["Control / status", "Bemanning"]
    assert [item["created"] for item in second] == [False, False]


@pytest.mark.asyncio
async def test_provision_rejects_outcome_from_other_project(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with odb.connect_closing() as conn:
        other = odb.create_outcome(conn, project_id="p_other", outcome_key="O")

    async def create_topic(_name: str):
        raise AssertionError("must not call Telegram before validation")

    with pytest.raises(ValueError, match="does not resolve"):
        await provision_telegram_topics(
            project_id="p_ps",
            chat_id=-100123,
            topics=[TopicSpec("Bemanning", outcome_id=other)],
            create_topic=create_topic,
        )


@pytest.mark.asyncio
async def test_failed_topic_creation_does_not_write_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    async def create_topic(_name: str):
        return None

    with pytest.raises(RuntimeError, match="did not return"):
        await provision_telegram_topics(
            project_id="p_ps",
            chat_id=-100123,
            topics=[TopicSpec("Control", lane_kind="control")],
            create_topic=create_topic,
        )
    with odb.connect_closing() as conn:
        assert odb.list_conversation_lanes(conn, "p_ps") == []
