"""Bounded controller receipts for the three terminal progression edges."""

from __future__ import annotations

import pytest

from gateway.kanban_outcome_owner_wake import owner_wake_prompt


@pytest.mark.parametrize(
    ("event_kind", "title"),
    [
        ("completed", "implementation complete; existing QA is authorized"),
        ("completed", "QA approved; existing release is authorized"),
        ("completed", "deploy-complete; perform live readback"),
    ],
)
def test_progression_receipt_is_graph_safe(event_kind, title):
    spec = {
        "project_id": "p_demo",
        "outcome_id": "o_demo",
        "outcome_revision": "or_demo",
        "board": "hermes",
        "task_id": "t_demo",
        "event_id": "42",
        "event_kind": event_kind,
        "visible_owner": "owner",
        "route": {
            "lane_id": "cl_demo", "platform": "telegram",
            "chat_id": "owner-chat", "thread_id": "owner-topic",
            "target": "telegram:owner-chat:owner-topic", "profile": "owner",
        },
        "outcome": {
            "outcome_key": "DEMO",
            "current_candidate_ref": "candidate-1",
            "current_base_ref": "base-1",
            "current_live_ref": "live-1",
        },
        "task": {
            "title": title,
            "parent_execution_id": "ex_demo",
            "mutation_repository": "example/repo",
            "mutation_scope": ["src/**"],
            "mutation_base_ref": "base-1",
            "topic_target": "telegram:owner-chat:owner-topic",
        },
        "human_gate": False,
    }
    prompt = owner_wake_prompt(spec)
    assert "consume at most one already-authorized next gate" in prompt
    assert "Do not create a successor graph" in prompt
    assert "do not deploy again" in prompt
    assert "candidate-1 / base-1 / live-1" in prompt
    assert "example/repo / src/** / base-1" in prompt
