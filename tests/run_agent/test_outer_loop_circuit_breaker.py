"""Regression tests for deterministic outer-loop failure handling."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _response(content="provider response"):
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_agent() -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://example.com/v1",
            provider="openai",
            api_mode="chat_completions",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _response()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.max_iterations = 80
    return agent


def test_identical_outer_loop_error_stops_after_three_attempts():
    """A deterministic local bug must not burn the full provider-call budget."""
    agent = _make_agent()

    with (
        patch(
            "agent.conversation_loop.has_incomplete_scratchpad",
            side_effect=TypeError("expected string or bytes-like object, got 'list'"),
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("inspect this image")

    assert agent.client.chat.completions.create.call_count == 3
    assert result["api_calls"] == 3
    assert result["failed"] is True
    assert result["turn_exit_reason"].startswith("repeated_outer_loop_error(")
    assert "same internal error repeated 3 times" in result["final_response"]