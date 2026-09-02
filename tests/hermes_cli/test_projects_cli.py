"""Tests for the `hermes project` CLI dispatch (hermes_cli/projects_cmd)."""

from __future__ import annotations

import argparse

import pytest

from hermes_cli import projects_cmd
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
