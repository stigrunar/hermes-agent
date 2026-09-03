"""Tests for the `hermes project` CLI dispatch (hermes_cli/projects_cmd)."""

from __future__ import annotations

import argparse

import pytest

from hermes_cli import projects_cmd
from hermes_cli import outcomes_db as odb
from hermes_cli import projects_db as pdb


def _run(argv):
    """Build the project subparser, parse argv, and dispatch. Returns rc."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    p = projects_cmd.build_parser(sub)
    p.set_defaults(func=projects_cmd.projects_command)
    args = parser.parse_args(["project", *argv])
    return projects_cmd.projects_command(args)


def test_create_list_show(capsys, tmp_path):
    assert _run(["create", "My App", str(tmp_path), "--use"]) == 0
    out = capsys.readouterr().out
    assert "Created project" in out

    with pdb.connect_closing() as conn:
        projects = pdb.list_projects(conn)
        assert len(projects) == 1
        assert projects[0].name == "My App"
        # --use set it active.
        assert pdb.get_active_id(conn) == projects[0].id

    assert _run(["list"]) == 0
    assert "my-app" in capsys.readouterr().out

    assert _run(["show", "my-app"]) == 0
    assert "My App" in capsys.readouterr().out




def test_rename_and_archive(tmp_path):
    _run(["create", "Old Name", str(tmp_path)])
    assert _run(["rename", "old-name", "New Name"]) == 0
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "old-name").name == "New Name"

    assert _run(["archive", "old-name"]) == 0
    with pdb.connect_closing() as conn:
        assert pdb.list_projects(conn) == []
        assert len(pdb.list_projects(conn, include_archived=True)) == 1

    assert _run(["restore", "old-name"]) == 0
    with pdb.connect_closing() as conn:
        assert len(pdb.list_projects(conn)) == 1






def test_outcome_lane_and_snapshot_cli(capsys, tmp_path):
    assert _run(["create", "Prosjektstyring", str(tmp_path)]) == 0
    capsys.readouterr()
    assert _run([
        "outcome-create",
        "prosjektstyring",
        "STAFFING-TEST-ENABLER-R1",
        "--state",
        "implementing",
        "--next",
        "Prove real read-only seam",
    ]) == 0
    created = capsys.readouterr().out
    assert "STAFFING-TEST-ENABLER-R1" in created

    assert _run([
        "bind-lane",
        "prosjektstyring",
        "--platform",
        "telegram",
        "--chat-id",
        "-1001",
        "--thread-id",
        "42",
        "--outcome",
        "STAFFING-TEST-ENABLER-R1",
        "--label",
        "Bemanning",
    ]) == 0
    assert "telegram:-1001:42" in capsys.readouterr().out

    assert _run(["snapshot", "prosjektstyring"]) == 0
    snapshot = capsys.readouterr().out
    assert "STAFFING-TEST-ENABLER-R1" in snapshot
    assert "implementing" in snapshot
    assert "Conversation lanes: 1" in snapshot


def test_outcome_update_cli(capsys, tmp_path):
    _run(["create", "P", str(tmp_path)])
    capsys.readouterr()
    _run(["outcome-create", "p", "O1"])
    capsys.readouterr()
    assert _run([
        "outcome-update",
        "p",
        "O1",
        "--state",
        "candidate",
        "--candidate",
        "abc123",
    ]) == 0
    assert "state=candidate" in capsys.readouterr().out


def test_outcome_create_and_update_frozen_acceptance_cli(capsys, tmp_path):
    _run(["create", "P", str(tmp_path)])
    capsys.readouterr()
    assert _run([
        "outcome-create", "p", "O1",
        "--acceptance", "first criterion",
        "--acceptance", "second criterion",
    ]) == 0
    capsys.readouterr()
    with odb.connect_closing() as conn:
        outcome = odb.get_outcome(conn, "O1")
        assert outcome is not None
        assert outcome.frozen_acceptance == ["first criterion", "second criterion"]

    assert _run([
        "outcome-update", "p", "O1",
        "--acceptance", "replacement criterion",
    ]) == 0
    capsys.readouterr()
    with odb.connect_closing() as conn:
        updated = odb.get_outcome(conn, "O1")
        assert updated is not None
        assert updated.frozen_acceptance == ["replacement criterion"]


def test_materialize_outcome_status_cli(capsys, tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True, capture_output=True)
    _run(["create", "P", str(tmp_path)])
    capsys.readouterr()
    _run(["outcome-create", "p", "O1", "--state", "implementing"])
    capsys.readouterr()
    assert _run(["materialize-status", "p", "O1"]) == 0
    output = capsys.readouterr().out.strip()
    assert output.endswith("docs/outcomes/O1/00-status.md")
    assert (tmp_path / "docs/outcomes/O1/00-status.md").exists()


def test_cross_project_outcome_dependency_cli(capsys, tmp_path):
    ps = tmp_path / "ps"; hw = tmp_path / "hw"
    ps.mkdir(); hw.mkdir()
    _run(["create", "PS", str(ps)]); capsys.readouterr()
    _run(["create", "HW", str(hw)]); capsys.readouterr()
    _run(["outcome-create", "ps", "STAFFING-R1"]); capsys.readouterr()
    _run(["outcome-create", "hw", "HWSTAFF-R2"]); capsys.readouterr()
    assert _run(["outcome-depend", "hw", "HWSTAFF-R2", "ps", "STAFFING-R1"]) == 0
    out = capsys.readouterr().out
    assert "hw/HWSTAFF-R2 -> ps/STAFFING-R1" in out


def test_execution_and_resource_cli(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(odb, "cross_project_orchestration_enabled", lambda: True)
    assert _run(["create", "Plugin A", str(tmp_path)]) == 0
    capsys.readouterr()
    assert _run(["outcome-create", "plugin-a", "PLUGIN-A-R1"]) == 0
    capsys.readouterr()
    assert _run([
        "bind-lane", "plugin-a", "--platform", "telegram", "--chat-id", "-1001",
        "--thread-id", "42", "--outcome", "PLUGIN-A-R1",
    ]) == 0
    capsys.readouterr()
    with pdb.connect_closing() as project_conn:
        project = pdb.get_project(project_conn, "plugin-a")
        assert project is not None
        project_id = project.id
    with odb.connect_closing() as conn:
        lanes = odb.list_conversation_lanes(conn, project_id)
        lane_id = lanes[0].id

    assert _run([
        "execution-create", "plugin-a", "PLUGIN-A-R1", "--mode", "direct_codex",
        "--owner", "default", "--read-only", "--lane", lane_id,
        "--resource", "vectorworks-local",
    ]) == 0
    created = __import__("json").loads(capsys.readouterr().out)
    execution_id = created["execution_id"]
    assert created["delivery_target"] == "telegram:-1001:42"

    assert _run(["execution-admit", "plugin-a", execution_id]) == 0
    admitted = __import__("json").loads(capsys.readouterr().out)
    assert admitted["admitted"] is True
    assert admitted["execution"]["state"] == "running"

    assert _run(["execution-heartbeat", "plugin-a", execution_id]) == 0
    assert __import__("json").loads(capsys.readouterr().out)["ok"] is True

    assert _run([
        "execution-terminal", "plugin-a", execution_id, "--state", "completed",
        "--receipt", "receipt://plugin-a",
    ]) == 0
    terminal = __import__("json").loads(capsys.readouterr().out)
    assert terminal["execution"]["state"] == "completed"
    assert terminal["execution"]["receipt_uri"] == "receipt://plugin-a"
