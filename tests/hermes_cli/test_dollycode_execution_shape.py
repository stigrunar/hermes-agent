"""Execution-shape routing guards for DollyCode and durable Sol controllers."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


LEAF_BODY = """execution_contract: leaf-fixture
execution_shape: leaf
mutation_scope: src/feature.py and its focused test
acceptance_commands:
- python -m pytest tests/test_feature.py
qa_boundary: Code runs focused checks; detached QA owns any full browser matrix.
"""


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def test_agent_created_dollycode_card_requires_leaf_contract(isolated_home: Path) -> None:
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="leaf execution contract"):
            kb.create_task(
                conn,
                title="oversized implementation",
                body="outcome: build the whole feature",
                assignee="dollycode",
                created_by="default",
            )

        tid = kb.create_task(
            conn,
            title="bounded implementation",
            body=LEAF_BODY,
            assignee="dollycode",
            created_by="default",
        )
        assert kb.get_task(conn, tid).assignee == "dollycode"


def test_controller_and_goal_mode_cannot_route_to_dollycode(isolated_home: Path) -> None:
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="controller-sized work"):
            kb.create_task(
                conn,
                title="controller feature",
                body="execution_shape: controller\noutcome: integrate several scopes",
                assignee="dollycode",
                created_by="default",
            )

        with pytest.raises(ValueError, match="leaf-only"):
            kb.create_task(
                conn,
                title="durable mission",
                body=LEAF_BODY,
                assignee="dollycode",
                created_by="default",
                goal_mode=True,
            )


def test_legacy_unattributed_fixture_remains_compatible(isolated_home: Path) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="legacy fixture",
            body="minimal historical body",
            assignee="dollycode",
        )
        assert kb.get_task(conn, tid) is not None


def _capture_spawn(
    monkeypatch: pytest.MonkeyPatch,
    task: kb.Task,
    workspace: Path,
) -> list[str]:
    captured: dict[str, list[str]] = {}

    class FakeProc:
        pid = 99999

    def fake_popen(cmd, **kwargs):  # noqa: ANN001
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace.mkdir(parents=True, exist_ok=True)
    assert kb._default_spawn(task, str(workspace)) == 99999
    return captured["cmd"]


def test_default_controller_auto_loads_codex_first(
    isolated_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="durable controller",
            body="execution_shape: controller\noutcome: integrate bounded packages",
            assignee="default",
            created_by="default",
            workspace_kind="dir",
            workspace_path=str(tmp_path / "controller-ws"),
        )
        task = kb.get_task(conn, tid)

    cmd = _capture_spawn(monkeypatch, task, tmp_path / "controller-ws")
    skill_pairs = list(zip(cmd, cmd[1:]))
    assert ("--skills", "codex-first") in skill_pairs


def test_dollycode_leaf_does_not_add_sol_wrapper(
    isolated_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="leaf implementation",
            body=LEAF_BODY,
            assignee="dollycode",
            created_by="default",
            workspace_kind="dir",
            workspace_path=str(tmp_path / "leaf-ws"),
        )
        task = kb.get_task(conn, tid)

    cmd = _capture_spawn(monkeypatch, task, tmp_path / "leaf-ws")
    assert "codex-first" not in cmd
