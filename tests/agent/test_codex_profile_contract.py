"""Regression coverage for profile-scoped Codex model and callback tools."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.codex_runtime import _codex_reasoning_effort
from agent.transports.codex_app_server_session import CodexAppServerSession


def _session(**kwargs):
    client = MagicMock()
    client.is_alive.return_value = True
    client.take_server_request.return_value = None
    client.stderr_tail.return_value = []

    def request(method, params, timeout):
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}

    client.request.side_effect = request
    client.take_notification.side_effect = [{
        "method": "turn/completed",
        "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
    }]
    session = CodexAppServerSession(cwd="/tmp", client_factory=lambda **kw: client, **kwargs)
    return client, session


def test_selected_model_effort_and_child_defaults_reach_thread_start():
    client, session = _session(
        model="gpt-6-astra", reasoning_effort="low",
        developer_instructions="frozen worker contract",
        config_overrides={"agents.default_subagent_model": "gpt-5.6-luna"},
    )
    session.ensure_started()
    params = client.request.call_args.args[1]
    assert params["model"] == "gpt-6-astra"
    assert params["config"]["model_reasoning_effort"] == "low"
    assert params["config"]["agents.default_subagent_model"] == "gpt-5.6-luna"
    assert params["developerInstructions"] == "frozen worker contract"


def test_each_turn_preserves_explicit_model_and_effort():
    client, session = _session(model="gpt-5.6-luna", reasoning_effort="xhigh")
    result = session.run_turn(
        "test", model="gpt-6-astra", reasoning_effort="low", turn_timeout=1,
    )
    params = next(call.args[1] for call in client.request.call_args_list if call.args[0] == "turn/start")
    assert params["model"] == "gpt-6-astra"
    assert params["effort"] == "low"
    assert result.error is None


def test_agent_reasoning_effort_is_normalized():
    assert _codex_reasoning_effort(SimpleNamespace(reasoning_config={"effort": "high"})) == "high"
    assert _codex_reasoning_effort(SimpleNamespace(reasoning_config=None)) is None


def test_required_dollycode_callback_tools_are_exposed():
    from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS

    assert {
        "kanban_show", "kanban_complete", "kanban_request_review",
        "kanban_request_changes", "kanban_attach", "kanban_attachments", "browser_exec",
    }.issubset(EXPOSED_TOOLS)
    assert not {"delegate_task", "memory", "session_search", "todo"}.intersection(EXPOSED_TOOLS)
