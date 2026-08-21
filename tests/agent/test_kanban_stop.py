"""Focused tests for the role-safe Kanban closeout reserve."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.conversation_loop import (
    KANBAN_CLOSEOUT_RESERVE_NOTICE,
    _maybe_inject_kanban_closeout_reserve,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    return monkeypatch


def _agent(max_iterations=60, nudged=False):
    return SimpleNamespace(
        max_iterations=max_iterations,
        _kanban_closeout_reserve_injected=nudged,
        _session_messages=None,
    )


def test_reserve_injects_once_into_latest_tool_without_new_row(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "task-1")
    older = {"role": "tool", "name": "terminal", "content": "older"}
    latest = {"role": "tool", "name": "pytest", "content": "latest"}
    messages = [older, latest]
    agent = _agent()

    assert _maybe_inject_kanban_closeout_reserve(
        agent=agent, messages=messages, api_call_count=54
    ) is True
    assert len(messages) == 2
    assert older["content"] == "older"
    assert latest["content"] == (
        "latest\n\n" + KANBAN_CLOSEOUT_RESERVE_NOTICE
    )
    assert agent._kanban_closeout_reserve_injected is True

    assert _maybe_inject_kanban_closeout_reserve(
        agent=agent, messages=messages, api_call_count=55
    ) is False
    assert latest["content"].count(KANBAN_CLOSEOUT_RESERVE_NOTICE) == 1


def test_reserve_preserves_multimodal_tool_content(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "task-2")
    blocks = [
        {"type": "text", "text": "tool output"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
    ]
    message = {"role": "tool", "content": blocks}

    assert _maybe_inject_kanban_closeout_reserve(
        agent=_agent(), messages=[message], api_call_count=54
    ) is True
    assert message["content"][:2] == blocks
    assert message["content"][-1] == {
        "type": "text",
        "text": KANBAN_CLOSEOUT_RESERVE_NOTICE,
    }


def test_no_tool_row_does_not_latch_and_retries_later(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "task-3")
    agent = _agent()
    messages = [{"role": "user", "content": "work"}]

    assert _maybe_inject_kanban_closeout_reserve(
        agent=agent, messages=messages, api_call_count=54
    ) is False
    assert agent._kanban_closeout_reserve_injected is False
    messages.append({"role": "tool", "content": "result"})
    assert _maybe_inject_kanban_closeout_reserve(
        agent=agent, messages=messages, api_call_count=55
    ) is True


def test_already_latched_reserve_does_not_mutate_latest_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "task-latched")
    message = {"role": "tool", "content": "existing result"}
    agent = _agent(nudged=True)

    assert _maybe_inject_kanban_closeout_reserve(
        agent=agent, messages=[message], api_call_count=54
    ) is False
    assert message["content"] == "existing result"


@pytest.mark.parametrize(
    ("env", "count", "maximum", "messages"),
    [
        (None, 54, 60, [{"role": "tool", "content": "result"}]),
        ("task-4", 53, 60, [{"role": "tool", "content": "result"}]),
        ("task-4", 54, 9, [{"role": "tool", "content": "result"}]),
        (
            "task-4",
            54,
            60,
            [
                {"role": "tool", "content": "cached result"},
                {"role": "assistant", "content": "current response"},
            ],
        ),
        (
            "task-4",
            54,
            60,
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "kanban_request_review",
                                "arguments": "{}",
                            }
                        }
                    ],
                },
                {"role": "tool", "name": "kanban_request_review", "content": "ok"},
            ],
        ),
    ],
)
def test_reserve_negative_cases(clear_kanban_env, env, count, maximum, messages):
    if env is not None:
        clear_kanban_env.setenv("HERMES_KANBAN_TASK", env)
    agent = _agent(maximum)
    before = [dict(message) for message in messages]

    assert _maybe_inject_kanban_closeout_reserve(
        agent=agent, messages=messages, api_call_count=count
    ) is False
    assert messages == before
    assert agent._kanban_closeout_reserve_injected is False


def test_notice_requires_one_decisive_check_and_exactly_one_lifecycle_transition(
    clear_kanban_env,
):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "task-5")
    message = {"role": "tool", "content": "result"}
    assert _maybe_inject_kanban_closeout_reserve(
        agent=_agent(), messages=[message], api_call_count=54
    ) is True
    notice = message["content"]
    assert "Stop expanding scope" in notice
    assert "evidence already gathered" in notice
    assert "at most one decisive directly affected check" in notice
    assert notice.count("kanban_complete") == 1
    assert notice.count("kanban_request_review") == 1
    assert notice.count("kanban_block") == 1
    assert not any(key.startswith("_kanban") for key in message)
