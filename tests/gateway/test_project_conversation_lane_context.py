from __future__ import annotations

from types import SimpleNamespace

from gateway.platforms.base import Platform
from gateway.run import _project_conversation_lane_prompt
from hermes_cli import outcomes_db as odb


def _source(*, chat_id="-1001", thread_id="42"):
    return SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        thread_id=thread_id,
    )


def test_bound_telegram_topic_injects_project_outcome_context(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with odb.connect_closing() as conn:
        oid = odb.create_outcome(
            conn,
            project_id="p_ps",
            outcome_key="STAFFING-TEST-ENABLER-R1",
        )
        lane = odb.bind_conversation_lane(
            conn,
            project_id="p_ps",
            outcome_id=oid,
            platform="telegram",
            chat_id="-1001",
            thread_id="42",
            label="Bemanning",
        )
    prompt = _project_conversation_lane_prompt(_source())
    assert "Project ID: `p_ps`" in prompt
    assert f"Conversation lane ID: `{lane}`" in prompt
    assert f"Outcome ID: `{oid}`" in prompt
    assert "STAFFING-TEST-ENABLER-R1" in prompt
    assert "does not grant repository mutation or deploy authority" in prompt


def test_unbound_topic_has_no_project_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    assert _project_conversation_lane_prompt(_source()) == ""


def test_thread_can_inherit_project_control_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with odb.connect_closing() as conn:
        odb.bind_conversation_lane(
            conn,
            project_id="p_ps",
            platform="telegram",
            chat_id="-1001",
            label="Project forum",
            lane_kind="control",
        )
    prompt = _project_conversation_lane_prompt(_source(thread_id="999"))
    assert "Project ID: `p_ps`" in prompt
    assert "Outcome: project-level/control lane" in prompt
