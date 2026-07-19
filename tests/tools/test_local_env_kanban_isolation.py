from __future__ import annotations

from tools.environments import local


KANBAN_CONTEXT_KEYS = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACE_KIND",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_BRANCH",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_GOAL_MODE",
    "HERMES_KANBAN_GOAL_MAX_TURNS",
)

KANBAN_ENV = {
    "HERMES_KANBAN_TASK": "t_parent",
    "HERMES_KANBAN_WORKSPACE": "/tmp/work",
    "HERMES_KANBAN_WORKSPACE_KIND": "worktree",
    "HERMES_KANBAN_DB": "/tmp/kanban.db",
    "HERMES_KANBAN_BOARD": "default",
    "HERMES_KANBAN_RUN_ID": "42",
    "HERMES_KANBAN_CLAIM_LOCK": "host:123",
    "HERMES_KANBAN_BRANCH": "wt/t_parent",
    "HERMES_KANBAN_WORKSPACES_ROOT": "/tmp/workspaces",
    "HERMES_KANBAN_GOAL_MODE": "1",
    "HERMES_KANBAN_GOAL_MAX_TURNS": "5",
    "HERMES_PROFILE": "worker",
    "HERMES_HOME": "/tmp/hermes",
}


def test_make_run_env_strips_kanban_worker_context(monkeypatch):
    for key, value in KANBAN_ENV.items():
        monkeypatch.setenv(key, value)

    env = local._make_run_env({})

    for key in KANBAN_CONTEXT_KEYS:
        assert key not in env
    assert env["HERMES_PROFILE"] == "worker"
    assert env["HERMES_HOME"] == "/tmp/hermes"


def test_sanitize_subprocess_env_allows_explicit_kanban_context_opt_in(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_PROPAGATE_CONTEXT", raising=False)

    env = local._sanitize_subprocess_env(
        KANBAN_ENV,
        {"HERMES_KANBAN_PROPAGATE_CONTEXT": "1"},
    )

    for key in KANBAN_CONTEXT_KEYS:
        assert env[key] == KANBAN_ENV[key]


def test_make_run_env_allows_explicit_kanban_context_opt_in(monkeypatch):
    for key, value in KANBAN_ENV.items():
        monkeypatch.setenv(key, value)

    env = local._make_run_env({"HERMES_KANBAN_PROPAGATE_CONTEXT": "1"})

    for key in KANBAN_CONTEXT_KEYS:
        assert env[key] == KANBAN_ENV[key]


def test_hermes_subprocess_env_strips_kanban_worker_context(monkeypatch):
    for key, value in KANBAN_ENV.items():
        monkeypatch.setenv(key, value)

    env = local.hermes_subprocess_env(inherit_credentials=True)

    for key in KANBAN_CONTEXT_KEYS:
        assert key not in env
