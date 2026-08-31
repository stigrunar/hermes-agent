"""Focused proof for repository execution policy and Kanban integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.project_execution_policy import (
    canonical_execution_preflight,
    resolve_project_execution_policy,
)


def _profile(repo: Path, **values: str) -> None:
    policy_dir = repo / ".hermes"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "project.yaml").write_text(
        "\n".join(f"{key}: {value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )


_BASE = {
    "lifecycle": "active",
    "kind": "private_test",
    "usage": "unused",
    "exposure": "private",
    "continuity": "restartable",
    "effect": "none",
}


@pytest.mark.parametrize(
    ("overrides", "execution", "expected"),
    [
        (
            {},
            {"action": "inspect", "quality_mode": "SPIKE", "risk_tier": "R0"},
            ("SPIKE", "R1"),
        ),
        (
            {},
            {"action": "deploy", "quality_mode": "SPIKE", "risk_tier": "R0"},
            ("FEATURE", "R1"),
        ),
        (
            {"kind": "private_preview"},
            {"action": "inspect", "quality_mode": "SPIKE", "risk_tier": "R0"},
            ("FEATURE", "R2"),
        ),
        (
            {"kind": "staging"},
            {"action": "inspect", "quality_mode": "SPIKE", "risk_tier": "R0"},
            ("FEATURE", "R2"),
        ),
        (
            {"usage": "business_active"},
            {"action": "inspect", "quality_mode": "SPIKE", "risk_tier": "R0"},
            ("FEATURE", "R2"),
        ),
        (
            {"effect": "reversible_write"},
            {"action": "write", "quality_mode": "SPIKE", "risk_tier": "R0"},
            ("FEATURE", "R2"),
        ),
        (
            {"kind": "production"},
            {"action": "inspect", "quality_mode": "FEATURE", "risk_tier": "R1"},
            ("RELEASE", "R3"),
        ),
        (
            {"exposure": "public"},
            {"action": "inspect", "quality_mode": "FEATURE", "risk_tier": "R1"},
            ("RELEASE", "R3"),
        ),
        (
            {"usage": "customer_active"},
            {"action": "inspect", "quality_mode": "FEATURE", "risk_tier": "R1"},
            ("RELEASE", "R3"),
        ),
        (
            {"effect": "external_write"},
            {"action": "deploy", "quality_mode": "FEATURE", "risk_tier": "R1"},
            ("RELEASE", "R3"),
        ),
        (
            {"effect": "destructive"},
            {"action": "deploy", "quality_mode": "FEATURE", "risk_tier": "R1"},
            ("RELEASE", "R3"),
        ),
        (
            {},
            {"action": "migrate", "quality_mode": "FEATURE", "risk_tier": "R1"},
            ("RELEASE", "R3"),
        ),
    ],
)
def test_policy_examples(
    tmp_path: Path, overrides: dict, execution: dict, expected: tuple[str, str]
) -> None:
    values = {**_BASE, **overrides}
    _profile(tmp_path, **values)
    result = resolve_project_execution_policy(tmp_path, execution)
    assert result is not None
    assert (
        result["resolved"]["quality_mode"],
        result["resolved"]["risk_tier"],
    ) == expected
    if execution["action"] == "deploy" and not overrides:
        assert result["resolved"]["effect"] == "none"
        assert result["resolved"]["continuity"] == "restartable"
        assert result["proof_policy"] == {
            "scope": "private_test_feature",
            "build_artifact_check": True,
            "restart": True,
            "sustained_health": True,
            "actual_target_smoke": True,
            "rollback_required": False,
            "rollback_ready": False,
            "rollback_plan": "optional",
            "independent_qa": False,
            "full_suite": False,
        }


def test_profile_only_defaults_to_feature_and_no_profile_is_legacy(
    tmp_path: Path,
) -> None:
    _profile(tmp_path, **_BASE)
    profile_only = resolve_project_execution_policy(tmp_path)
    assert profile_only is not None
    assert profile_only["resolved"]["quality_mode"] == "FEATURE"
    assert resolve_project_execution_policy(tmp_path / "other") is None


def test_canonical_json_is_mapping_order_independent() -> None:
    left = {"resolved": {"risk_tier": "R1", "quality_mode": "FEATURE"}, "version": 1}
    right = {"version": 1, "resolved": {"quality_mode": "FEATURE", "risk_tier": "R1"}}
    assert canonical_execution_preflight(left) == canonical_execution_preflight(right)


def test_profile_mapping_order_does_not_change_preflight(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _profile(first, **_BASE)
    _profile(second, **dict(reversed(tuple(_BASE.items()))))
    execution = {
        "environment": "default",
        "action": "deploy",
        "quality_mode": "SPIKE",
        "risk_tier": "R0",
    }
    left = resolve_project_execution_policy(first, execution)
    right = resolve_project_execution_policy(second, execution)
    assert left is not None and right is not None
    # Repo-specific provenance differs; the resolved contract does not.
    for result in (left, right):
        result["inputs"]["project_repo"] = None
        result["profile_path"] = None
    assert canonical_execution_preflight(left) == canonical_execution_preflight(right)


@pytest.mark.parametrize(
    "contents",
    [
        "x: &x local\nkind: *x\n",
        "kind: !custom local\n",
        "kind: local\nkind: production\n",
    ],
)
def test_unsafe_or_ambiguous_yaml_is_conservative_and_keeps_digest(
    tmp_path: Path, contents: str
) -> None:
    policy_dir = tmp_path / ".hermes"
    policy_dir.mkdir()
    path = policy_dir / "project.yaml"
    path.write_text(contents, encoding="utf-8")
    result = resolve_project_execution_policy(tmp_path, {"action": "inspect"})
    assert result is not None
    assert result["resolved"]["risk_tier"] == "R3"
    assert result["profile_digest"]
    assert result["diagnostics"]


def test_malformed_environment_for_mutation_fails_closed(tmp_path: Path) -> None:
    _profile(tmp_path, **_BASE)
    path = tmp_path / ".hermes" / "project.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "environments: broken\n",
        encoding="utf-8",
    )
    result = resolve_project_execution_policy(
        tmp_path, {"environment": "test", "action": "deploy"}
    )
    assert result is not None
    assert result["resolved"]["quality_mode"] == "RELEASE"
    assert result["resolved"]["risk_tier"] == "R3"
    assert result["resolved"]["continuity"] == "rollback_required"
    assert "conservative_mutation_floor" in result["reasons"]


def test_project_link_resolves_primary_repo_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _profile(repo, **_BASE)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import projects_db as pdb

    pdb._INITIALIZED_PATHS.clear()
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn, name="Policy Repo", folders=[str(repo)]
        )
        project = pdb.get_project(project_conn, project_id)
    assert project is not None

    kb._INITIALIZED_PATHS.clear()
    conn = kb.connect(db_path=home / "kanban.db")
    try:
        task_id = kb.create_task(
            conn,
            title="private test deploy",
            project_id=project.slug,
            execution={
                "environment": "default",
                "action": "deploy",
                "quality_mode": "SPIKE",
                "risk_tier": "R0",
            },
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        preflight = task.execution_preflight
        assert preflight is not None
        assert preflight["inputs"]["project_repo"] == str(repo)
        assert preflight["profile_path"] == str(repo / ".hermes" / "project.yaml")
        assert preflight["profile_digest"]
        assert preflight["resolved"]["quality_mode"] == "FEATURE"
        assert preflight["resolved"]["risk_tier"] == "R1"
        raw = conn.execute(
            "SELECT execution_preflight FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["execution_preflight"]
        assert raw == canonical_execution_preflight(preflight)
    finally:
        conn.close()


def test_persistence_context_and_tool_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "tester")
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    kb._INITIALIZED_PATHS.clear()
    conn = kb.connect(db_path=home / "kanban.db")
    try:
        task_id = kb.create_task(
            conn,
            title="explicit preflight",
            body="BODY",
            assignee="tester",
            execution={"action": "write", "quality_mode": "SPIKE", "risk_tier": "R0"},
        )
        task = kb.get_task(conn, task_id)
        assert task is not None and task.execution_preflight is not None
        assert task.execution_preflight["resolved"]["risk_tier"] == "R3"
        context = kb.build_worker_context(conn, task_id)
        assert context.index("## Execution preflight") < context.index("## Body")
        assert "Invariant floor:" in context
        assert "Workers may escalate on concrete evidence" in context
    finally:
        conn.close()

    invalid = json.loads(
        kt._handle_create({
            "title": "invalid execution",
            "assignee": "tester",
            "execution": {"action": "inspect"},
        })
    )
    assert "execution requires" in invalid["error"]

    created = json.loads(
        kt._handle_create({
            "title": "tool task",
            "assignee": "tester",
            "execution": {"environment": "local", "action": "inspect"},
        })
    )
    assert created["ok"] is True
    assert created["execution_preflight"]["resolved"]["risk_tier"] == "R3"
    shown = json.loads(kt._handle_show({"task_id": created["task_id"]}))
    assert shown["task"]["execution_preflight"] is not None
    listed = json.loads(kt._handle_list({"limit": 10}))
    row = next(item for item in listed["tasks"] if item["id"] == created["task_id"])
    assert row["execution_preflight"] is not None
