"""Regression coverage for profile-scoped Codex model, tools and continuity."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.codex_runtime import _codex_runtime_contract, _codex_thread_binding
from agent.transports.codex_app_server import CodexAppServerError
from agent.transports.codex_app_server_session import CodexAppServerSession


def client_and_session(**kwargs):
    client = MagicMock()
    client.is_alive.return_value = True
    client.take_server_request.return_value = None
    client.stderr_tail.return_value = []

    def request(method, params, timeout):
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}

    client.request.side_effect = request
    client.take_notification.side_effect = [
        {"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}
        }},
        None,
    ]
    session = CodexAppServerSession(cwd="/tmp", client_factory=lambda **kw: client, **kwargs)
    return client, session


def test_selected_model_effort_and_contract_reach_thread_start():
    client, session = client_and_session(
        model="gpt-6-astra", reasoning_effort="low",
        developer_instructions="frozen worker contract",
        config_overrides={"mcp_servers.hermes-tools": {"command": "python"}},
    )
    session.ensure_started()
    params = client.request.call_args.args[1]
    assert params["model"] == "gpt-6-astra"
    assert params["config"]["model_reasoning_effort"] == "low"
    assert params["developerInstructions"] == "frozen worker contract"
    assert params["config"]["mcp_servers.hermes-tools"]["command"] == "python"
    assert "baseInstructions" not in params


def test_each_turn_preserves_explicit_model_and_effort():
    client, session = client_and_session(model="gpt-5.6-luna", reasoning_effort="xhigh")
    result = session.run_turn("test", model="gpt-6-astra", reasoning_effort="low", turn_timeout=1)
    params = next(c.args[1] for c in client.request.call_args_list if c.args[0] == "turn/start")
    assert params["model"] == "gpt-6-astra"
    assert params["effort"] == "low"
    assert result.error is None


def test_resume_does_not_start_a_new_thread_and_persists_before_turn():
    ready = []
    client, session = client_and_session(resume_thread_id="thread-1", on_thread_ready=ready.append)
    session.ensure_started()
    assert ready == ["thread-1"]
    assert client.request.call_args.args[0] == "thread/resume"
    assert client.request.call_args.args[1]["threadId"] == "thread-1"
    session.ensure_started()
    assert client.request.call_count == 1


def test_resume_error_never_falls_back_to_another_writer():
    client, session = client_and_session(resume_thread_id="missing-thread")
    client.request.side_effect = CodexAppServerError(code=-32000, message="missing thread")
    result = session.run_turn("do not lose previous work", turn_timeout=1)
    assert result.error is not None
    assert [c.args[0] for c in client.request.call_args_list] == ["thread/resume"]


def test_binding_failure_prevents_model_turn():
    def fail(_):
        raise OSError("state persistence unavailable")
    client, session = client_and_session(on_thread_ready=fail)
    with pytest.raises(OSError):
        session.run_turn("must not execute", turn_timeout=1)
    assert [c.args[0] for c in client.request.call_args_list] == ["thread/start"]


def test_runtime_uses_rendered_instructions_and_thread_local_callback(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"agent": {"gateway_timeout": 1800}})
    monkeypatch.setattr(
        "hermes_cli.codex_runtime_plugin_migration._build_hermes_tools_mcp_entry",
        lambda: {"command": "python", "args": ["-m", "callback"], "env": {"HERMES_HOME": "/profile"}},
    )
    agent = SimpleNamespace(model="gpt-6-astra", reasoning_config={"effort": "low"},
                            _cached_system_prompt="DollyCode owns this bounded implementation.")
    contract, timeout = _codex_runtime_contract(agent, [], "/workspace")
    assert contract["model"] == "gpt-6-astra"
    assert contract["reasoning_effort"] == "low"
    assert contract["developer_instructions"].startswith("DollyCode owns")
    callback = contract["config_overrides"]["mcp_servers.hermes-tools"]
    assert callback["env"]["HERMES_HOME"] == "/profile"
    assert "PYTHONPATH" in callback["env"]
    assert contract["kanban_sandbox_mode"] == "workspace-write"
    assert timeout == 1740


def test_binding_survives_recreation_and_refuses_wrong_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    values = {}
    db = SimpleNamespace(get_meta=values.get, set_meta=values.__setitem__)
    agent = SimpleNamespace(_session_db=db, session_id="session-1")
    thread, save = _codex_thread_binding(agent, str(tmp_path))
    assert thread is None
    save("thread-1")
    thread, _ = _codex_thread_binding(agent, str(tmp_path))
    assert thread == "thread-1"
    assert json.loads(values["codex_app_server.thread:session-1"])["cwd"] == str(tmp_path)
    with pytest.raises(ValueError, match="does not match"):
        _codex_thread_binding(agent, str(tmp_path / "other-workspace"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "different-codex"))
    with pytest.raises(ValueError, match="does not match"):
        _codex_thread_binding(agent, str(tmp_path))


def test_required_dollycode_callback_tools_are_exposed():
    from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS
    assert {"kanban_show", "kanban_complete", "kanban_request_review", "kanban_request_changes",
            "kanban_attach", "kanban_attachments", "browser_exec"}.issubset(EXPOSED_TOOLS)
    assert not {"delegate_task", "memory", "session_search", "todo"}.intersection(EXPOSED_TOOLS)


def test_worker_scope_and_child_defaults_are_thread_local(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "isolated-task")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_KANBAN_DB", "/isolated/kanban.db")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {
        "codex_app_server": {"default_subagent_model": "gpt-5.6-luna",
                             "default_subagent_reasoning_effort": "xhigh",
                             "kanban_sandbox_mode": "danger-full-access"}
    })
    monkeypatch.setattr(
        "hermes_cli.codex_runtime_plugin_migration._build_hermes_tools_mcp_entry",
        lambda: {"command": "python", "env": {}},
    )
    agent = SimpleNamespace(model="gpt-6-astra", reasoning_config={"effort": "low"})
    contract, _ = _codex_runtime_contract(agent, [], "/workspace")
    config = contract["config_overrides"]
    env = config["mcp_servers.hermes-tools"]["env"]
    assert env["HERMES_KANBAN_TASK"] == "isolated-task"
    assert env["HERMES_KANBAN_RUN_ID"] == "7"
    assert env["HERMES_KANBAN_DB"] == "/isolated/kanban.db"
    assert config["agents.default_subagent_model"] == "gpt-5.6-luna"
    assert config["agents.default_subagent_reasoning_effort"] == "xhigh"
    assert config["agents.max_concurrent_threads_per_session"] == 2
    assert contract["kanban_sandbox_mode"] == "danger-full-access"
    assert not any("config_file" in key for key in config)
