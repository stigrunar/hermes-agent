from __future__ import annotations

import importlib
import json

import yaml


MODULE = "plugins.execution_envelope_telemetry"


def _fresh_plugin():
    module = importlib.import_module(MODULE)
    module._STATES.clear()
    return module


def test_deterministic_fixture_emits_schema_valid_jsonl():
    plugin = _fresh_plugin()
    lines = []

    plugin.on_post_api_request(
        session_id="session-1",
        task_id="task-1",
        api_request_id="api-1",
        started_at=100.0,
        model="gpt-test",
        provider="test-provider",
        api_mode="responses",
        usage={
            "prompt_tokens": 80,
            "completion_tokens": 20,
            "cache_read_tokens": 30,
            "cache_write_tokens": 5,
            "reasoning_tokens": 7,
            "total_tokens": 135,
        },
    )
    plugin.on_post_tool_call(
        session_id="session-1",
        task_id="task-1",
        tool_name="terminal",
        args={"command": "pytest private/test_name.py"},
        result="private output",
    )
    plugin.on_subagent_start(
        parent_session_id="session-1",
        child_goal="private delegated prompt",
    )
    record = plugin.emit_completed_run(
        session_id="session-1",
        task_id="task-1",
        user_message="private prompt",
        assistant_response="private answer",
        _clock=lambda: 112.5,
        _writer=lines.append,
    )

    assert record is not None
    assert len(lines) == 1
    assert "\n" not in lines[0]
    assert json.loads(lines[0]) == record
    assert lines[0] == plugin.serialize_record(record)
    assert record["schema_version"] == "execution-envelope-telemetry/v1"
    assert record["runtime"]["model"] == {"status": "available", "value": "gpt-test"}
    assert record["runtime"]["api_mode"] == {"status": "available", "value": "responses"}
    assert record["timing"]["wall_time_seconds"] == {"status": "available", "value": 12.5}
    assert record["usage"] == {
        "iterations": {"status": "available", "value": 1},
        "prompt_tokens": {"status": "available", "value": 80},
        "completion_tokens": {"status": "available", "value": 20},
        "cached_tokens": {"status": "available", "value": 35},
        "reasoning_tokens": {"status": "available", "value": 7},
        "total_tokens": {"status": "available", "value": 135},
    }
    assert record["delivery"]["tool_calls"]["value"] == 1
    assert record["delivery"]["commands_run"]["value"] == 1
    assert record["delivery"]["worker_count"]["value"] == 1


def test_absent_fields_are_explicit_and_zero_is_not_missing():
    plugin = _fresh_plugin()
    lines = []

    record = plugin.emit_completed_run(
        session_id="session-empty",
        task_id="task-empty",
        _clock=lambda: 42.0,
        _writer=lines.append,
    )

    assert record is not None
    assert record["usage"]["iterations"] == {"status": "available", "value": 0}
    assert record["delivery"]["commands_run"] == {"status": "available", "value": 0}
    assert record["delivery"]["worker_count"] == {"status": "available", "value": 0}
    assert record["runtime"]["fallbacks"]["status"] == "not_available"
    assert record["runtime"]["enabled_toolsets"]["status"] == "not_available"
    assert record["runtime"]["skills_loaded"]["status"] == "not_available"
    assert record["delivery"]["files_changed"]["status"] == "not_available"
    assert record["delivery"]["tests_run"]["status"] == "not_available"
    assert record["delivery"]["acceptance"]["status"] == "not_available"
    for field in record["usage"].values():
        if field["status"] == "not_available":
            assert field["reason"]


def test_raw_prompts_secrets_tool_payloads_and_errors_never_leak():
    plugin = _fresh_plugin()
    lines = []
    sentinels = {
        "PROMPT_SENTINEL",
        "ASSISTANT_SENTINEL",
        "sk-secret-sentinel",
        "TOOL_RESULT_SENTINEL",
        "ERROR_SENTINEL",
        "DELEGATE_SENTINEL",
    }

    plugin.on_pre_llm_call(
        session_id="safe-session",
        task_id="safe-task",
        user_message="PROMPT_SENTINEL",
        conversation_history=[{"content": "sk-secret-sentinel"}],
    )
    plugin.on_api_request_error(
        session_id="safe-session",
        task_id="safe-task",
        api_request_id="failed-api",
        started_at=10.0,
        request={"body": {"messages": ["PROMPT_SENTINEL"], "api_key": "sk-secret-sentinel"}},
        error={"message": "ERROR_SENTINEL"},
    )
    plugin.on_post_tool_call(
        session_id="safe-session",
        task_id="safe-task",
        tool_name="terminal",
        args={"command": "echo sk-secret-sentinel"},
        result="TOOL_RESULT_SENTINEL",
    )
    plugin.on_subagent_start(
        parent_session_id="safe-session",
        child_goal="DELEGATE_SENTINEL",
        child_summary="PROMPT_SENTINEL",
    )
    plugin.emit_completed_run(
        session_id="safe-session",
        task_id="safe-task",
        user_message="PROMPT_SENTINEL",
        assistant_response="ASSISTANT_SENTINEL",
        conversation_history=[{"content": "sk-secret-sentinel"}],
        _clock=lambda: 11.0,
        _writer=lines.append,
    )

    serialized = lines[0]
    for sentinel in sentinels:
        assert sentinel not in serialized
    assert "echo" not in serialized


def test_duplicate_request_id_counts_once_and_missing_usage_stays_missing():
    plugin = _fresh_plugin()
    lines = []
    payload = {
        "session_id": "session-duplicate",
        "task_id": "task-duplicate",
        "api_request_id": "same-api",
        "started_at": 5.0,
    }

    plugin.on_api_request_error(**payload, error={"message": "ignored"})
    plugin.on_post_api_request(**payload, usage=None)
    record = plugin.emit_completed_run(
        session_id=payload["session_id"],
        task_id=payload["task_id"],
        _clock=lambda: 6.0,
        _writer=lines.append,
    )

    assert record is not None
    assert record["usage"]["iterations"]["value"] == 1
    assert record["usage"]["total_tokens"]["status"] == "not_available"


def test_default_writer_appends_one_local_jsonl_record(tmp_path, monkeypatch):
    plugin = _fresh_plugin()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    plugin.on_post_api_request(
        session_id="local-session",
        task_id="local-task",
        api_request_id="api-local",
        started_at=20.0,
        usage={"input_tokens": 2, "output_tokens": 1},
    )
    plugin.emit_completed_run(
        session_id="local-session",
        task_id="local-task",
        _clock=lambda: 21.0,
    )

    output = tmp_path / "telemetry" / "execution-envelope.jsonl"
    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["run"]["task_id"]["value"] == "local-task"


def test_manifest_and_registration_match_supported_hooks():
    plugin = _fresh_plugin()
    manifest_path = plugin.Path(plugin.__file__).with_name("plugin.yaml")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    registered = []

    class Context:
        def register_hook(self, name, callback):
            registered.append((name, callback))

    plugin.register(Context())

    names = [name for name, _ in registered]
    assert names == manifest["hooks"]
    assert manifest["kind"] == "standalone"
