"""Focused acceptance tests for the opt-in blocker-SLA continuation pass."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_blocked(
    conn,
    tmp_path: Path,
    *,
    kind: str = "capability",
    reason: str = "self-fixable blocker",
    target: str = "alice",
    source_assignee: str = "owner",
    metadata: dict | None = None,
    workspace: bool = True,
    dependency_task_id: str | None = None,
    old: bool = True,
    metadata_author: str = "controller",
) -> str:
    workspace_path = None
    workspace_kind = "scratch"
    if workspace:
        workspace_path = str(tmp_path / "reusable-workspace")
        Path(workspace_path).mkdir(exist_ok=True)
        workspace_kind = "dir"
    task_id = kb.create_task(
        conn,
        title="ordinary source title",
        assignee=source_assignee,
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
    )
    assert kb.block_task(
        conn,
        task_id,
        reason=reason,
        kind=kind,
        dependency_task_id=dependency_task_id,
    )
    marker = {
        "classification": kind,
        "target_assignee": target,
        **(metadata or {}),
    }
    kb.add_comment(
        conn,
        task_id,
        metadata_author,
        "controller_continuation_metadata: " + json.dumps(marker),
    )
    if old:
        cutoff = int(time.time()) - 901
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id = ? AND kind = 'blocked'",
                (cutoff, task_id),
            )
    return task_id


def _spawnable_profiles(monkeypatch: pytest.MonkeyPatch, names=("alice", "owner")):
    from hermes_cli import profiles

    monkeypatch.setattr(
        profiles,
        "profile_exists",
        lambda name: str(name).casefold() in {n.casefold() for n in names},
    )


def test_younger_blocker_is_not_continued(kanban_home, tmp_path, monkeypatch):
    _spawnable_profiles(monkeypatch)
    with kb.connect_closing() as conn:
        source = _make_blocked(conn, tmp_path, old=False)
        decisions = kb.continue_blocked_tasks(conn)
        assert decisions[0]["decision"] == "skipped"
        assert decisions[0]["reason"] == "blocker_younger_than_sla"
        assert decisions[0]["source_task_id"] == source
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_exactly_900_seconds_old_is_not_older_than_sla(
    kanban_home, tmp_path, monkeypatch,
):
    _spawnable_profiles(monkeypatch)
    with kb.connect_closing() as conn:
        source = _make_blocked(conn, tmp_path, old=False)
        event_created = conn.execute(
            "SELECT created_at FROM task_events WHERE task_id = ? "
            "AND kind = 'blocked'",
            (source,),
        ).fetchone()["created_at"]
        decisions = kb.continue_blocked_tasks(
            conn, now=int(event_created) + kb.DEFAULT_BLOCKER_CONTINUATION_SLA_SECONDS,
        )
        assert decisions[0]["reason"] == "blocker_younger_than_sla"


@pytest.mark.parametrize(
    ("kind", "reason", "metadata"),
    [
        ("needs_input", "human decision required", {}),
        ("capability", "review-required: human signoff", {}),
        ("capability", "requires human input before continuing", {}),
        ("capability", "maintenance-window: wait for window", {}),
        ("capability", "wait for the maintenance window", {}),
        ("capability", "self-fixable blocker", {"scheduled": True}),
    ],
)
def test_human_review_and_scheduled_semantics_are_excluded(
    kanban_home, tmp_path, monkeypatch, kind, reason, metadata,
):
    _spawnable_profiles(monkeypatch)
    with kb.connect_closing() as conn:
        source = _make_blocked(
            conn, tmp_path, kind=kind, reason=reason, metadata=metadata,
        )
        decisions = kb.continue_blocked_tasks(conn)
        assert decisions[0]["source_task_id"] == source
        assert decisions[0]["decision"] == "skipped"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_unchanged_dependency_wait_is_not_continued(kanban_home, tmp_path, monkeypatch):
    _spawnable_profiles(monkeypatch)
    with kb.connect_closing() as conn:
        dependency = kb.create_task(conn, title="dependency", assignee="owner")
        source = kb.create_task(
            conn,
            title="source",
            assignee="owner",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        assert kb.block_task(
            conn, source, reason="waiting for dependency",
            kind="dependency", dependency_task_id=dependency,
        )
        kb.add_comment(
            conn, source, "controller",
            'controller_continuation_metadata: '
            + json.dumps({
                "classification": "dependency",
                "target_assignee": "alice",
            }),
        )
        decisions = kb.continue_blocked_tasks(conn)
        assert decisions == []
        assert kb.get_task(conn, source).status == "todo"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


def test_explicit_metadata_routes_exactly_one_detached_continuation(
    kanban_home, tmp_path, monkeypatch,
):
    _spawnable_profiles(monkeypatch)
    with kb.connect_closing() as conn:
        source = _make_blocked(conn, tmp_path)
        decisions = kb.continue_blocked_tasks(conn)
        assert decisions[0]["decision"] == "routed"
        replacement = decisions[0]["replacement_task_id"]
        assert replacement
        assert replacement != source
        assert conn.execute(
            "SELECT COUNT(*) FROM task_links WHERE child_id = ?", (replacement,)
        ).fetchone()[0] == 0
        created = kb.get_task(conn, replacement)
        assert created.tenant == kb.get_task(conn, source).tenant
        assert created.priority == kb.get_task(conn, source).priority
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'continuation_created'",
            (replacement,),
        ).fetchone()
        payload = json.loads(event["payload"])
        assert payload["source_task_id"] == source
        assert payload["replacement_task_id"] == replacement
        assert payload["block_event_id"] == decisions[0]["block_event_id"]
        assert payload["dedup_key"] == created.idempotency_key


def test_worker_authored_metadata_never_authorizes_continuation(
    kanban_home,
    tmp_path,
    monkeypatch,
):
    _spawnable_profiles(monkeypatch)
    with kb.connect_closing() as conn:
        source = _make_blocked(
            conn,
            tmp_path,
            metadata_author="worker",
        )
        decisions = kb.continue_blocked_tasks(conn)
        assert len(decisions) == 1
        assert decisions[0]["decision"] == "skipped"
        assert decisions[0]["reason"] == "continuation_metadata_required"
        assert decisions[0]["source_task_id"] == source
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_repeated_unchanged_ticks_return_existing_path_without_duplicate(
    kanban_home, tmp_path, monkeypatch,
):
    _spawnable_profiles(monkeypatch)
    with kb.connect_closing() as conn:
        source = _make_blocked(conn, tmp_path)
        first = kb.continue_blocked_tasks(conn)
        replacement = first[0]["replacement_task_id"]
        second = kb.continue_blocked_tasks(conn)
        assert second[0]["decision"] == "existing_live"
        assert second[0]["replacement_task_id"] == replacement
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


def test_non_actionable_replacement_is_audited_but_never_authorizes(
    kanban_home,
    tmp_path,
    monkeypatch,
):
    _spawnable_profiles(monkeypatch)
    with kb.connect_closing() as conn:
        source = _make_blocked(conn, tmp_path)
        replacement = kb.continue_blocked_tasks(conn)[0]["replacement_task_id"]
        canonical = kb.create_task(conn, title="canonical", assignee="owner")
        assert kb.mark_task_non_actionable(
            conn,
            replacement,
            status="superseded",
            actor="controller",
            superseded_by=canonical,
            live_path_task_id=canonical,
            canonical_live_path="refs/heads/main",
        )

        decision = kb.continue_blocked_tasks(conn)[0]
        assert decision["decision"] == "existing_non_authorizing"
        assert decision["replacement_task_id"] == replacement
        assert decision["superseded_by"] == canonical
        assert decision["live_path_task_id"] == canonical
        assert decision["canonical_live_path"] == "refs/heads/main"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 3


def test_explicit_replacement_id_must_match_durable_path(
    kanban_home, tmp_path, monkeypatch,
):
    _spawnable_profiles(monkeypatch)
    with kb.connect_closing() as conn:
        source = _make_blocked(conn, tmp_path)
        first = kb.continue_blocked_tasks(conn)
        replacement = first[0]["replacement_task_id"]
        kb.add_comment(
            conn, source, "controller",
            "controller_continuation_metadata: "
            + json.dumps({
                "classification": "capability",
                "target_assignee": "alice",
                "replacement_task_id": replacement,
            }),
        )
        second = kb.continue_blocked_tasks(conn)
        assert second[0]["decision"] == "existing_live"
        assert second[0]["replacement_task_id"] == replacement


def test_dependency_change_after_terminal_continuation_allows_one_new_path(
    kanban_home, tmp_path, monkeypatch,
):
    _spawnable_profiles(monkeypatch)
    with kb.connect_closing() as conn:
        dependency = kb.create_task(conn, title="dependency", assignee="owner")
        assert kb.complete_task(conn, dependency, result="version-one")
        source = _make_blocked(
            conn, tmp_path,
            metadata={"dependency_task_id": dependency},
        )
        first = kb.continue_blocked_tasks(conn)
        first_replacement = first[0]["replacement_task_id"]
        assert kb.complete_task(conn, first_replacement, result="finished")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET result = ? WHERE id = ?",
                ("version-two", dependency),
            )
        second = kb.continue_blocked_tasks(conn)
        assert second[0]["decision"] == "routed"
        assert second[0]["replacement_task_id"] != first_replacement
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 4
        assert kb.continue_blocked_tasks(conn)[0]["decision"] == "existing_live"


def test_pending_linked_parent_cannot_be_bypassed(
    kanban_home, tmp_path, monkeypatch,
):
    _spawnable_profiles(monkeypatch)
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="unfinished parent", assignee="owner")
        source = _make_blocked(conn, tmp_path)
        kb.link_tasks(conn, parent_id=parent, child_id=source)
        decisions = kb.continue_blocked_tasks(conn)
        assert decisions[0]["reason"] == "parent_dependency_pending"
        assert decisions[0]["dependency_task_id"] == parent
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


@pytest.mark.parametrize("target", ["worker", " CoDeX ", "ACP"])
def test_pseudo_assignees_never_create_or_emit_motion(kanban_home, tmp_path, monkeypatch, target):
    _spawnable_profiles(monkeypatch, names=("alice", "owner"))
    with kb.connect_closing() as conn:
        _make_blocked(conn, tmp_path, target=target)
        before_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        decisions = kb.continue_blocked_tasks(conn)
        assert decisions[0]["reason"] == "target_assignee_pseudo_profile"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == before_events


@pytest.mark.parametrize("source_assignee", ["worker", " CoDeX ", "ACP"])
def test_legacy_pseudo_source_rows_never_become_normal_execution(
    kanban_home, tmp_path, monkeypatch, source_assignee,
):
    _spawnable_profiles(monkeypatch, names=("alice", "owner"))
    with kb.connect_closing() as conn:
        source = _make_blocked(
            conn, tmp_path, source_assignee=source_assignee, target="alice",
        )
        before_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        decisions = kb.continue_blocked_tasks(conn)
        assert decisions[0]["source_task_id"] == source
        assert decisions[0]["reason"] == "source_assignee_pseudo_profile"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == before_events


def test_invalid_profile_and_workspace_are_rejected_without_creation(
    kanban_home, tmp_path, monkeypatch,
):
    _spawnable_profiles(monkeypatch, names=("owner",))
    with kb.connect_closing() as conn:
        _make_blocked(conn, tmp_path, target="missing-profile")
        _make_blocked(conn, tmp_path, target="owner", workspace=False)
        decisions = kb.continue_blocked_tasks(conn)
        assert {d["reason"] for d in decisions} == {
            "target_assignee_profile_missing",
            "workspace_not_reusable",
        }
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


def test_opt_in_dispatch_can_spawn_created_continuation(kanban_home, tmp_path, monkeypatch):
    _spawnable_profiles(monkeypatch)
    spawned = []
    with kb.connect_closing() as conn:
        source = _make_blocked(conn, tmp_path)

        def fake_spawn(task, workspace):
            spawned.append((task.id, workspace))
            return None

        result = kb.dispatch_once(
            conn,
            enable_continuations=True,
            spawn_fn=fake_spawn,
            max_spawn=1,
        )
        replacement = result.continuation_decisions[0]["replacement_task_id"]
        assert result.continuation_decisions[0]["decision"] == "routed"
        assert spawned == [(replacement, str(tmp_path / "reusable-workspace"))]
        assert kb.get_task(conn, replacement).status == "running"
        assert source not in [task_id for task_id, _ in spawned]

        repeated = kb.dispatch_once(
            conn,
            enable_continuations=True,
            spawn_fn=fake_spawn,
            max_spawn=1,
        )
        assert repeated.continuation_decisions[0]["decision"] == "existing_live"
        assert spawned == [(replacement, str(tmp_path / "reusable-workspace"))]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


def test_continuation_dry_run_is_mutation_free(kanban_home, tmp_path, monkeypatch):
    _spawnable_profiles(monkeypatch)
    with kb.connect_closing() as conn:
        _make_blocked(conn, tmp_path)
        before = list(conn.iterdump())
        decisions = kb.continue_blocked_tasks(conn, dry_run=True)
        after = list(conn.iterdump())
        assert decisions[0]["decision"] == "would_route"
        assert before == after


def test_opt_in_dispatch_dry_run_is_mutation_free(
    kanban_home, tmp_path, monkeypatch,
):
    _spawnable_profiles(monkeypatch)
    with kb.connect_closing() as conn:
        _make_blocked(conn, tmp_path)
        before = list(conn.iterdump())
        result = kb.dispatch_once(
            conn, enable_continuations=True, dry_run=True,
        )
        after = list(conn.iterdump())
        assert result.continuation_decisions[0]["decision"] == "would_route"
        assert before == after


def test_cli_only_passes_activation_when_explicit(kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban as kanban_cli

    captured = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch_once)
    args = argparse.Namespace(
        dry_run=True, max=None, failure_limit=2, json=True,
        continue_blockers=True,
    )
    assert kanban_cli._cmd_dispatch(args) == 0
    assert captured["enable_continuations"] is True
    assert json.loads(capsys.readouterr().out)["continuations"] == []

    captured.clear()
    args.continue_blockers = False
    assert kanban_cli._cmd_dispatch(args) == 0
    assert "enable_continuations" not in captured
