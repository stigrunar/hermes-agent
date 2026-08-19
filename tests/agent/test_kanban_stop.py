"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.conversation_loop import _append_kanban_budget_reserve_nudge
from agent.kanban_stop import (
    build_kanban_budget_reserve_nudge,
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch






def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_request_review_counts_as_terminal(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_request_review", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "kanban_request_review",
            "tool_call_id": "1",
            "content": "review",
        },
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None
    assert build_kanban_budget_reserve_nudge(
        messages=messages, api_call_count=54, max_iterations=60
    ) is None


def test_budget_reserve_nudges_before_hard_limit(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_budget")
    assert build_kanban_budget_reserve_nudge(
        messages=[], api_call_count=53, max_iterations=60
    ) is None
    nudge = build_kanban_budget_reserve_nudge(
        messages=[], api_call_count=54, max_iterations=60
    )
    assert nudge is not None
    assert "6 model-call slot" in nudge
    assert "kanban_complete" in nudge
    assert "kanban_request_review" in nudge
    assert "kanban_block" in nudge
    assert "new broad suite" in nudge


def test_conversation_loop_appends_reserve_nudge_at_54_of_60(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_budget")
    statuses = []
    agent = SimpleNamespace(
        max_iterations=60,
        _kanban_budget_reserve_nudged=False,
        _session_messages=None,
        _emit_status=statuses.append,
    )
    messages = []

    assert _append_kanban_budget_reserve_nudge(
        agent=agent, messages=messages, api_call_count=53
    ) is False
    assert messages == []

    assert _append_kanban_budget_reserve_nudge(
        agent=agent, messages=messages, api_call_count=54
    ) is True
    assert agent._kanban_budget_reserve_nudged is True
    assert agent._session_messages is messages
    assert messages[-1]["role"] == "user"
    assert messages[-1]["_kanban_budget_reserve_synthetic"] is True
    assert "6 model-call slot" in messages[-1]["content"]
    assert statuses == ["⚠️ Kanban closeout reserve active (54/60)"]


def test_budget_reserve_is_one_shot_and_not_for_tiny_budgets(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_budget")
    assert build_kanban_budget_reserve_nudge(
        messages=[], api_call_count=54, max_iterations=60, already_nudged=True
    ) is None
    assert build_kanban_budget_reserve_nudge(
        messages=[], api_call_count=1, max_iterations=1
    ) is None


# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.




