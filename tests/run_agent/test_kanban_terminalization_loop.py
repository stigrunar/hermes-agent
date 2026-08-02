"""Conversation-loop coverage for one-shot Kanban terminalization."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _tool_call(name: str):
    return SimpleNamespace(
        id=f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments="{}"),
    )


def _response(*, tool: str | None = None, content: str = ""):
    calls = [_tool_call(tool)] if tool else None
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=calls),
                finish_reason="tool_calls" if tool else "stop",
            )
        ],
        model="test/model",
        usage=None,
    )


def _agent(max_iterations: int) -> AIAgent:
    names = ("read_file", "kanban_complete", "kanban_block")
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs(*names)),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("hermes_cli.config.load_config_readonly", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=max_iterations,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._disable_streaming = True
    return agent


def _run(agent: AIAgent, terminal_probe):
    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"ok": True})),
        patch("agent.kanban_stop.accepted_kanban_terminal_intent", side_effect=terminal_probe),
        patch("hermes_cli.kanban_db._record_task_failure") as timeout_record,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("finish the task")
    return result, timeout_record


@pytest.fixture
def exact_worker(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_exact")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)


def test_successful_terminal_request_stops_before_later_provider_or_timeout(exact_worker):
    agent = _agent(90)
    agent.client.chat.completions.create.side_effect = [
        _response(tool="kanban_complete"),
        AssertionError("provider called after accepted terminal intent"),
    ]

    result, timeout_record = _run(agent, lambda: {"action": "complete"})

    assert agent.client.chat.completions.create.call_count == 1
    assert result["turn_exit_reason"] == "kanban_terminal_intent_accepted"
    timeout_record.assert_not_called()


def test_rejected_terminal_request_remains_correctable(exact_worker):
    agent = _agent(90)
    agent.client.chat.completions.create.side_effect = [
        _response(tool="kanban_complete"),
        _response(tool="kanban_block"),
        AssertionError("provider called after corrected terminal intent"),
    ]
    probes = iter([None, {"action": "block"}, {"action": "block"}])

    result, timeout_record = _run(agent, lambda: next(probes))

    assert agent.client.chat.completions.create.call_count == 2
    assert result["turn_exit_reason"] == "kanban_terminal_intent_accepted"
    timeout_record.assert_not_called()


@pytest.mark.parametrize(("max_iterations", "checkpoint_call"), [(90, 40), (15, 5), (5, 1)])
def test_checkpoint_fires_once_and_reserves_closeout_calls(
    exact_worker, max_iterations, checkpoint_call
):
    agent = _agent(max_iterations)
    agent.client.chat.completions.create.side_effect = [
        *[_response(tool="read_file") for _ in range(checkpoint_call)],
        _response(tool="kanban_complete"),
    ]

    result, timeout_record = _run(agent, lambda: {"action": "complete"})

    assert agent.client.chat.completions.create.call_count == checkpoint_call + 1
    tool_text = "\n".join(
        str(message.get("content", ""))
        for message in result["messages"]
        if message.get("role") == "tool"
    )
    assert tool_text.count("Kanban terminalization checkpoint v1") == 1
    assert "exactly one concrete expected-vs-actual mismatch" in tool_text
    assert max_iterations - checkpoint_call >= min(10, max_iterations - 1)
    timeout_record.assert_not_called()


@pytest.mark.parametrize("mode", ["goal", "non_kanban"])
def test_goal_and_non_kanban_conversation_paths_do_not_stop_on_probe(monkeypatch, mode):
    if mode == "goal":
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_goal")
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "9")
        monkeypatch.setenv("HERMES_KANBAN_GOAL_MODE", "1")
    else:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    agent = _agent(10)
    agent.client.chat.completions.create.side_effect = [
        _response(tool="kanban_complete"),
        _response(content="ordinary final response"),
    ]

    result, timeout_record = _run(agent, lambda: None)

    assert agent.client.chat.completions.create.call_count == 2
    assert result["final_response"] == "ordinary final response"
    assert "Kanban terminalization checkpoint v1" not in str(result["messages"])
    timeout_record.assert_not_called()
