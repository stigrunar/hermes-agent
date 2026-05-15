"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    try:
        from agent import skill_commands

        def _fake_load_skill_payload(skill_identifier: str, task_id: str | None = None):
            identifier = str(skill_identifier or "").strip()
            if not identifier or identifier.startswith("missing") or identifier.startswith("unknown"):
                return None
            return ({"name": identifier, "content": ""}, None, identifier)

        monkeypatch.setattr(skill_commands, "_load_skill_payload", _fake_load_skill_payload)
    except Exception:
        pass
