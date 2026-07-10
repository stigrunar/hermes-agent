from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def profiles_exist(monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True, text=True)
    return path


def _run_cli(argv: list[str]) -> int:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command")
    kanban_cli.build_parser(sub)
    ns = root.parse_args(["kanban", *argv])
    return kanban_cli.kanban_command(ns)


def test_create_rejects_unanchored_worktree_before_insert(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="workspace_kind=worktree"):
            kb.create_task(conn, title="bad", workspace_kind="worktree")
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_create_accepts_worktree_target_inside_git_repo(kanban_home, tmp_path):
    repo = _git_repo(tmp_path / "repo")
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="ok",
            workspace_kind="worktree",
            workspace_path=str(repo / ".worktrees" / "future"),
        )
        assert kb.get_task(conn, tid).status == "ready"
        assert not (repo / ".worktrees" / "future").exists()


def test_workspace_preflight_blocks_first_failure_without_spawn_storm(kanban_home, tmp_path):
    bad = tmp_path / "not-a-repo" / "child"
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        conn.execute(
            "UPDATE tasks SET workspace_kind='dir', workspace_path=NULL WHERE id=?",
            (tid,),
        )
        res = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_kw: 123)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert res.auto_blocked == [tid]
    assert task.status == "blocked"
    assert task.failure_classification == "workspace_config"
    assert task.failure_fingerprint
    assert [e.kind for e in events].count("deterministic_preflight_blocked") == 1


@pytest.mark.parametrize(
    ("status", "task_id"),
    [("ready", "t_9a5d835f"), ("review", "t_9a5d835e")],
)
def test_malformed_legacy_worktree_blocks_before_nonspawnable_profile(
    kanban_home,
    monkeypatch,
    status,
    task_id,
):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: False)
    with kb.connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, assignee, status, priority, created_at,
                workspace_kind, workspace_path
            ) VALUES (?, ?, ?, ?, 0, ?, 'worktree', ?)
            """,
            (
                task_id,
                "malformed legacy worktree",
                "missing-profile",
                status,
                int(time.time()),
                ".worktrees/t_9a5d835f",
            ),
        )
        res = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_kw: 123)
        task = kb.get_task(conn, task_id)
    assert res.skipped_nonspawnable == []
    assert res.auto_blocked == [task_id]
    assert task.status == "blocked"
    assert task.failure_classification == "workspace_config"
    assert task.failure_fingerprint


@pytest.mark.parametrize("status", ["ready", "review"])
def test_explicit_capability_requirement_blocks_before_nonspawnable_profile(
    kanban_home,
    monkeypatch,
    status,
):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: False)
    monkeypatch.setattr(kb, "_resolve_assignee_toolsets", lambda _a: (None, "profile missing"))
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="needs browser",
            assignee="missing-profile",
            required_capabilities=["browser"],
        )
        if status == "review":
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        res = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_kw: 123)
        task = kb.get_task(conn, tid)
    assert res.skipped_nonspawnable == []
    assert res.capability_blocked == [(tid, ["browser"], ["browser"])]
    assert task.status == "blocked"
    assert task.failure_classification == "capability_mismatch"
    assert task.failure_fingerprint


@pytest.mark.parametrize("status", ["ready", "review"])
def test_no_requirement_nonexistent_profile_remains_nonspawnable_lane(
    kanban_home,
    monkeypatch,
    status,
):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: False)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="human lane", assignee="missing-profile")
        if status == "review":
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        res = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_kw: 123)
        task = kb.get_task(conn, tid)
    assert res.skipped_nonspawnable == [tid]
    assert res.auto_blocked == []
    assert res.capability_blocked == []
    assert task.status == status


def test_superseded_never_dispatches_and_idempotency_ignores_history(kanban_home):
    with kb.connect() as conn:
        old = kb.create_task(conn, title="old", assignee="worker", idempotency_key="same")
        replacement = kb.create_task(conn, title="new", assignee="worker")
        kb.mark_task_non_actionable(
            conn,
            old,
            status="superseded",
            actor="controller",
            superseded_by=replacement,
        )
        again = kb.create_task(conn, title="again", assignee="worker", idempotency_key="same")
        res = kb.dispatch_once(conn, dry_run=True)
        visible = {t.id for t in kb.list_tasks(conn)}
    assert again != old
    assert old not in visible
    assert old not in [tid for tid, _who, _ws in res.spawned]


def test_stale_continuity_only_is_stable_non_success_history(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="stale", assignee="worker")
        kb.mark_task_non_actionable(
            conn,
            tid,
            status="stale_continuity_only",
            actor="controller",
            reason="duplicate",
        )
        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task.status == "stale_continuity_only"
    assert "completed" not in [e.kind for e in events]


def test_detached_live_path_parks_parent_gated_children_atomically(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="blocked source", assignee="worker")
        kb.block_task(conn, source, reason="review required", kind="capability")
        child = kb.create_task(conn, title="parent gated", assignee="worker", parents=[source])
        detached = kb.create_task(conn, title="detached review", assignee="reviewer")
        res = kb.park_blocked_source_detached_path(
            conn,
            source_task_id=source,
            detached_task_id=detached,
            actor="controller",
        )
        source_task = kb.get_task(conn, source)
        child_task = kb.get_task(conn, child)
    assert res["parked_children"] == [child]
    assert source_task.live_path_task_id == detached
    assert child_task.status == "stale_continuity_only"
    assert child_task.superseded_by == detached


def test_detached_live_path_rejects_blocked_detached_task(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="blocked source", assignee="worker")
        kb.block_task(conn, source, reason="review required", kind="capability")
        detached = kb.create_task(conn, title="blocked detached", assignee="reviewer")
        kb.block_task(conn, detached, reason="not actionable", kind="needs_input")
        with pytest.raises(ValueError, match="not an actionable live path"):
            kb.park_blocked_source_detached_path(
                conn,
                source_task_id=source,
                detached_task_id=detached,
                actor="controller",
            )


def test_detached_live_path_strictly_rejects_done_detached_task(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="blocked source", assignee="worker")
        kb.block_task(conn, source, reason="review required", kind="capability")
        detached = kb.create_task(conn, title="done detached", assignee="reviewer")
        assert kb.complete_task(conn, detached, summary="accepted proof")
        with pytest.raises(ValueError, match="not an actionable live path"):
            kb.park_blocked_source_detached_path(
                conn,
                source_task_id=source,
                detached_task_id=detached,
                actor="controller",
            )
        with pytest.raises(ValueError, match="not an actionable live path"):
            kb.reconcile_detached_live_paths(
                conn,
                [{"source_task_id": source, "detached_task_id": detached}],
                actor="controller",
            )


def test_controller_closeout_receipt_validation_and_dependency_promotion(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="helper", assignee="worker")
        child = kb.create_task(conn, title="dependent", assignee="worker", parents=[parent])
        with pytest.raises(ValueError, match="missing required"):
            kb.controller_closeout_task(conn, parent, receipt={}, actor="controller")
        assert kb.controller_closeout_task(
            conn,
            parent,
            actor="controller",
            receipt={
                "accepted_by": "lead",
                "accepted_at": "2026-07-10T12:00:00Z",
                "verdict": "accepted",
                "evidence": ["tests passed"],
            },
        )
        assert kb.get_task(conn, parent).status == "done"
        assert kb.get_task(conn, child).status == "ready"
        assert "controller_closeout" in [e.kind for e in kb.list_events(conn, parent)]


@pytest.mark.parametrize("verdict", ["changes_requested", "rejected", "blocked", "unknown"])
def test_controller_closeout_rejects_non_acceptance_verdicts(kanban_home, verdict):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="helper", assignee="worker")
        with pytest.raises(ValueError, match="explicit acceptance"):
            kb.controller_closeout_task(
                conn,
                tid,
                actor="controller",
                receipt={
                    "accepted_by": "lead",
                    "accepted_at": "2026-07-10T12:00:00Z",
                    "verdict": verdict,
                    "evidence": ["reviewed"],
                },
            )
        assert kb.get_task(conn, tid).status == "ready"


@pytest.mark.parametrize("terminal_status", ["superseded", "stale_continuity_only"])
def test_historical_terminal_parent_promotes_child(kanban_home, terminal_status):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="child", assignee="worker", parents=[parent])
        kb.mark_task_non_actionable(
            conn,
            parent,
            status=terminal_status,
            actor="controller",
        )
        assert kb.get_task(conn, child).status == "ready"


@pytest.mark.parametrize("terminal_status", ["superseded", "stale_continuity_only"])
def test_linking_to_historical_terminal_parent_does_not_demote_ready_child(kanban_home, terminal_status):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="child", assignee="worker")
        kb.mark_task_non_actionable(
            conn,
            parent,
            status=terminal_status,
            actor="controller",
        )
        kb.link_tasks(conn, parent, child)
        assert kb.get_task(conn, child).status == "ready"


def test_capability_preflight_dry_run_and_apply(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_resolve_assignee_toolsets", lambda _a: (["terminal"], None))
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="needs file",
            assignee="worker",
            required_capabilities=["file", "file", "process"],
        )
        dry = kb.dispatch_once(conn, dry_run=True)
        assert dry.capability_blocked == [(tid, ["file", "process"], ["file"])]
        assert kb.get_task(conn, tid).status == "ready"
        applied = kb.dispatch_once(conn)
        task = kb.get_task(conn, tid)
    assert applied.capability_blocked == [(tid, ["file", "process"], ["file"])]
    assert task.status == "blocked"
    assert task.block_kind == "capability"
    assert task.failure_classification == "capability_mismatch"


def test_network_capability_not_satisfied_by_terminal_only(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_resolve_assignee_toolsets", lambda _a: (["terminal"], None))
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="needs network",
            assignee="worker",
            required_capabilities=["network"],
        )
        res = kb.dispatch_once(conn, dry_run=True)
    assert res.capability_blocked == [(tid, ["network"], ["network"])]


@pytest.mark.parametrize("toolsets", [["web"], ["browser"], ["search"]])
def test_network_capability_satisfied_by_network_toolsets(kanban_home, monkeypatch, toolsets):
    monkeypatch.setattr(kb, "_resolve_assignee_toolsets", lambda _a: (toolsets, None))
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="network ok",
            assignee="worker",
            required_capabilities=["network"],
        )
        res = kb.dispatch_once(conn, dry_run=True)
    assert res.capability_blocked == []
    assert [spawn[0] for spawn in res.spawned] == [tid]


def test_capability_preflight_match_spawns(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_resolve_assignee_toolsets", lambda _a: (["terminal", "file"], None))
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="ok",
            assignee="worker",
            required_capabilities=["process", "file_patch"],
        )
        res = kb.dispatch_once(conn, dry_run=True)
    assert res.capability_blocked == []
    assert [spawn[0] for spawn in res.spawned] == [tid]


def test_reconcile_live_path_dry_run_apply_and_idempotent(kanban_home, capsys):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="blocked source", assignee="worker")
        kb.block_task(conn, source, reason="review required", kind="capability")
        child = kb.create_task(conn, title="mirror", assignee="worker", parents=[source])
        detached = kb.create_task(conn, title="detached", assignee="reviewer")

    rc = _run_cli(["reconcile-live-path", "--source", source, "--detached", detached, "--json"])
    preview = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert preview["dry_run"] is True
    with kb.connect() as conn:
        assert kb.get_task(conn, child).status == "todo"

    rc = _run_cli(["reconcile-live-path", "--source", source, "--detached", detached, "--apply", "--json"])
    applied = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert applied["dry_run"] is False
    with kb.connect() as conn:
        assert kb.get_task(conn, child).status == "stale_continuity_only"

    rc = _run_cli(["reconcile-live-path", "--source", source, "--detached", detached, "--apply", "--json"])
    again = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert again["applied"][0]["parked_children"] == []


def test_reconcile_live_path_invalid_later_mapping_mutates_nothing(kanban_home, capsys):
    with kb.connect() as conn:
        source_a = kb.create_task(conn, title="blocked a", assignee="worker")
        kb.block_task(conn, source_a, reason="review required", kind="capability")
        child_a = kb.create_task(conn, title="mirror a", assignee="worker", parents=[source_a])
        detached_a = kb.create_task(conn, title="detached a", assignee="reviewer")

        source_b = kb.create_task(conn, title="not blocked", assignee="worker")
        child_b = kb.create_task(conn, title="mirror b", assignee="worker", parents=[source_b])
        detached_b = kb.create_task(conn, title="detached b", assignee="reviewer")

    mapping = [
        {"source_task_id": source_a, "detached_task_id": detached_a},
        {"source_task_id": source_b, "detached_task_id": detached_b},
    ]
    mapping_path = kanban_home / "mapping.json"
    mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

    rc = _run_cli(["reconcile-live-path", "--mapping", str(mapping_path), "--apply", "--json"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "must be blocked" in err
    with kb.connect() as conn:
        assert kb.get_task(conn, child_a).status == "todo"
        assert kb.get_task(conn, child_b).status == "todo"


def test_reconcile_live_path_terminal_evidence_preview_apply(kanban_home, capsys):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="blocked source", assignee="worker")
        kb.block_task(conn, source, reason="review required", kind="capability")
        child = kb.create_task(conn, title="mirror", assignee="worker", parents=[source])
        detached = kb.create_task(conn, title="accepted terminal evidence", assignee="reviewer")
        assert kb.complete_task(
            conn,
            detached,
            summary="accepted terminal proof",
            metadata={"verification": ["focused test passed"]},
        )

    rc = _run_cli([
        "reconcile-live-path",
        "--source", source,
        "--detached", detached,
        "--allow-terminal-evidence",
        "--json",
    ])
    preview = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert preview["preview"][0]["mapping_kind"] == "terminal_evidence"
    assert preview["preview"][0]["terminal_evidence"]["completed_event_id"]
    with kb.connect() as conn:
        assert kb.get_task(conn, child).status == "todo"

    rc = _run_cli([
        "reconcile-live-path",
        "--source", source,
        "--detached", detached,
        "--allow-terminal-evidence",
        "--apply",
        "--json",
    ])
    applied = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert applied["applied"][0]["mapping_kind"] == "terminal_evidence"
    assert applied["applied"][0]["terminal_evidence"]["completed_event_id"]
    with kb.connect() as conn:
        assert kb.get_task(conn, child).status == "stale_continuity_only"
        assert kb.get_task(conn, source).live_path_task_id == detached


def test_reconcile_live_path_rejects_done_without_positive_evidence(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="blocked source", assignee="worker")
        kb.block_task(conn, source, reason="review required", kind="capability")
        detached = kb.create_task(conn, title="bare done", assignee="reviewer")
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            (int(time.time()), detached),
        )
        with pytest.raises(ValueError, match="lacks positive completion/acceptance evidence"):
            kb.reconcile_detached_live_paths(
                conn,
                [
                    {
                        "source_task_id": source,
                        "detached_task_id": detached,
                        "allow_terminal_evidence": True,
                    }
                ],
                actor="controller",
            )


def test_reconcile_live_path_terminal_evidence_batch_is_atomic(kanban_home, capsys):
    with kb.connect() as conn:
        source_a = kb.create_task(conn, title="blocked a", assignee="worker")
        kb.block_task(conn, source_a, reason="review required", kind="capability")
        child_a = kb.create_task(conn, title="mirror a", assignee="worker", parents=[source_a])
        detached_a = kb.create_task(conn, title="accepted terminal evidence", assignee="reviewer")
        assert kb.complete_task(conn, detached_a, summary="accepted terminal proof")

        source_b = kb.create_task(conn, title="blocked b", assignee="worker")
        kb.block_task(conn, source_b, reason="review required", kind="capability")
        child_b = kb.create_task(conn, title="mirror b", assignee="worker", parents=[source_b])
        detached_b = kb.create_task(conn, title="bare done", assignee="reviewer")
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            (int(time.time()), detached_b),
        )

    mapping = [
        {
            "source_task_id": source_a,
            "detached_task_id": detached_a,
            "allow_terminal_evidence": True,
        },
        {
            "source_task_id": source_b,
            "detached_task_id": detached_b,
            "allow_terminal_evidence": True,
        },
    ]
    mapping_path = kanban_home / "terminal-mapping.json"
    mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

    rc = _run_cli(["reconcile-live-path", "--mapping", str(mapping_path), "--apply", "--json"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "lacks positive completion/acceptance evidence" in err
    with kb.connect() as conn:
        assert kb.get_task(conn, child_a).status == "todo"
        assert kb.get_task(conn, child_b).status == "todo"
        assert kb.get_task(conn, source_a).live_path_task_id is None
