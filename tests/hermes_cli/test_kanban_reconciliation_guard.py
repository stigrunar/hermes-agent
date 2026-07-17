"""Behavior contracts for the repo/head/worktree reconciliation guard."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _receipt_run(conn, task_id: str, receipt: dict, *, started_at: int = 100) -> int:
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, status, outcome, started_at, ended_at, metadata) "
        "VALUES (?, 'done', 'completed', ?, ?, ?)",
        (
            task_id,
            started_at,
            started_at + 1,
            json.dumps({"reconciliation": receipt}),
        ),
    )
    return int(cur.lastrowid)


def _current_probe(head: str):
    def probe(_repo, _ref, _candidate=None):
        return {"state": "head_current", "current_head": head}

    return probe


def _candidate(head: str = "a" * 40, **extra) -> dict:
    return {
        "repo": "/repo",
        "ref": "refs/heads/main",
        "candidate_head": head,
        **extra,
    }


def _init_repo(path: Path) -> str:
    path.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "guard@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Guard Test"],
        check=True,
    )
    (path / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_receipt_survives_claim_failure_and_retry(kanban_home):
    head = "a" * 40
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="durable receipt", assignee="alice")
        first = kb.claim_task(conn, task_id)
        assert first is not None
        receipt_run_id = first.current_run_id
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (
                json.dumps({"reconciliation": _candidate(head)}),
                receipt_run_id,
            ),
        )
        assert kb._record_spawn_failure(
            conn, task_id, "synthetic launch failure", failure_limit=9,
        ) is False
        second = kb.claim_task(
            conn,
            task_id,
            reconciliation_probe=_current_probe(head),
            profile_roster={"alice"},
        )
        assert second is not None

        receipt, resolved_run_id = kd.reconciliation_receipt(
            conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        )
        assert receipt["candidate_head"] == head
        assert resolved_run_id == receipt_run_id


def test_stale_review_in_review_state_routes_exact_head_reviewer(kanban_home):
    head = "a" * 40
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="refresh review", assignee="alice")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        _receipt_run(conn, task_id, _candidate(
            head,
            review={"head": "b" * 40, "verdict": "changes_requested"},
        ))
        spawned: list[str] = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id),
            reconciliation_probe=_current_probe(head),
            profile_roster={"alice"},
        )

        assert spawned == [task_id]
        assert result.skipped_reconciliation == []
        assert any(
            finding["kind"] == "review_stale"
            for finding in result.reconciliation[task_id]["summary"]
        )
        assert result.reconciliation[task_id]["review"]["exact_head"] is False


def test_replacement_requires_target_owned_terminal_proof(kanban_home):
    with kb.connect() as conn:
        missing_source = kb.create_task(
            conn, title="missing replacement source", assignee="alice",
        )
        _receipt_run(conn, missing_source, {
            "replacement_task_id": "t_missing_replacement",
            "canonical_live_task": "t_missing_replacement",
            "terminal_receipt": {
                "state": "merged",
                "task_id": "t_missing_replacement",
                "head": "c" * 40,
            },
        })
        missing_report = kb._reconciliation_reports(
            conn, [missing_source], profile_roster={"alice"},
        )[missing_source]
        assert missing_report.suppressed is False
        assert missing_report.actionable is True

        source = kb.create_task(conn, title="source", assignee="alice")
        target = kb.create_task(conn, title="target", assignee="alice")
        source_receipt = {
            "replacement_task_id": target,
            "canonical_live_task": target,
            "terminal_receipt": {
                "state": "merged",
                "task_id": target,
                "head": "c" * 40,
            },
        }
        _receipt_run(conn, source, source_receipt)

        reports = kb._reconciliation_reports(
            conn, [source], profile_roster={"alice"},
        )
        assert reports[source].suppressed is False
        assert reports[source].actionable is True
        # A source-authored terminal assertion and an existing but
        # nonterminal target do not suppress ordinary claim/dispatch work.
        claimed = kb.claim_task(
            conn, source, reconciliation_report=reports[source],
        )
        assert claimed is not None
        assert kb.reclaim_task(conn, source, reason="continue proof fixture")

        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (target,))
        target_receipt = {
            "supersedes_task_id": source,
            "canonical_live_task": target,
            "candidate_head": "c" * 40,
            "terminal_receipt": {
                "state": "merged",
                "task_id": target,
                "head": "c" * 40,
            },
        }
        _receipt_run(conn, target, target_receipt, started_at=200)
        proven = kb._reconciliation_reports(
            conn, [source], profile_roster={"alice"},
        )[source]
        assert proven.suppressed is True
        assert proven.actionable is False
        assert kb.claim_task(
            conn, source, profile_roster={"alice"},
        ) is None
        assert kb.get_task(conn, source).status == "ready"


def test_exact_ref_missing_is_not_unknown_or_same_named_tag(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "-C", str(repo), "tag", "absent"], check=True)

    session = kd.GitProbeSession()
    missing = session(str(repo), "refs/heads/absent", "d" * 40)
    assert missing.state == "branch_missing"
    assert missing.current_head is None

    unknown_session = kd.GitProbeSession(
        runner=lambda _args, _timeout: (125, "", "sensor unavailable"),
    )
    unknown = unknown_session("https://example.invalid/repo.git", "main", "d" * 40)
    assert unknown.state == "branch_unknown"
    assert unknown.evidence["reason"] == "probe_error"


def test_git_probe_cache_and_total_command_budget_are_bounded():
    calls: list[list[str]] = []

    def runner(args, _timeout):
        calls.append(args)
        ref = args[-1]
        return 0, f"{'a' * 40}\t{ref}\n", ""

    session = kd.GitProbeSession(
        runner=runner,
        max_commands=2,
        deadline_seconds=1,
        command_timeout=0.1,
    )
    assert session("https://example.test/repo.git", "main", "a" * 40).state == "head_current"
    assert session("https://example.test/repo.git", "main", "b" * 40).state == "head_superseded"
    assert len(calls) == 1
    assert session("https://example.test/repo.git", "other", "a" * 40).state == "head_current"
    exhausted = session("https://example.test/repo.git", "third", "a" * 40)
    assert exhausted.state == "branch_unknown"
    assert exhausted.evidence["reason"] == "probe_budget_exhausted"
    assert len(calls) == 2


def test_real_worktree_identity_and_exact_head_are_enforced(tmp_path):
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    worktree = tmp_path / "candidate-wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "candidate", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    task = {
        "id": "t_worktree",
        "status": "ready",
        "assignee": "alice",
        "workspace_kind": "worktree",
        "workspace_path": str(worktree),
        "created_at": 1,
    }
    runs = [{
        "id": 1,
        "started_at": 1,
        "metadata": {"reconciliation": {
            "repo": str(repo),
            "ref": "refs/heads/candidate",
            "candidate_head": head,
        }},
    }]

    current = kd.reconcile_task(
        task, runs, git_probe=kd.GitProbeSession(), profile_roster={"alice"}, now=2,
    )
    assert current.actionable is True

    missing = kd.reconcile_task(
        {**task, "workspace_path": str(tmp_path / "missing")},
        runs,
        git_probe=kd.GitProbeSession(),
        profile_roster={"alice"},
        now=2,
    )
    assert "workspace_missing" in {finding.kind for finding in missing.findings}
    assert missing.actionable is False

    (worktree / "tracked.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "move"],
        check=True,
        capture_output=True,
        text=True,
    )
    moved = kd.reconcile_task(
        task, runs, git_probe=kd.GitProbeSession(), profile_roster={"alice"}, now=3,
    )
    assert {"head_superseded", "workspace_wrong_head"} <= {
        finding.kind for finding in moved.findings
    }
    assert moved.actionable is False


def test_non_runnable_assignee_blocks_direct_claim(kanban_home):
    head = "a" * 40
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="bad route", assignee="terminal-only")
        _receipt_run(conn, task_id, _candidate(head))
        assert kb.claim_task(
            conn,
            task_id,
            reconciliation_probe=_current_probe(head),
            profile_roster=set(),
        ) is None
        assert kb.get_task(conn, task_id).status == "ready"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'claimed'",
            (task_id,),
        ).fetchone()[0] == 0


def test_fingerprint_cas_rejects_change_after_probe_without_claim_side_effects(
    kanban_home,
):
    head = "a" * 40
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="raced", assignee="alice")
        _receipt_run(conn, task_id, _candidate(head))
        report = kb._reconciliation_reports(
            conn,
            [task_id],
            git_probe=_current_probe(head),
            profile_roster={"alice", "bob"},
        )[task_id]
        events_before = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        conn.execute("UPDATE tasks SET assignee = 'bob' WHERE id = ?", (task_id,))

        assert kb.claim_task(
            conn, task_id, reconciliation_report=report,
        ) is None
        assert kb.get_task(conn, task_id).status == "ready"
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == events_before


def test_dry_run_is_byte_and_semantic_no_write_even_for_maintenance(
    kanban_home, monkeypatch,
):
    with kb.connect() as conn:
        expired = kb.create_task(conn, title="expired", assignee="alice")
        claimed = kb.claim_task(conn, expired)
        assert claimed is not None
        conn.execute(
            "UPDATE tasks SET claim_expires = 1, worker_pid = NULL WHERE id = ?",
            (expired,),
        )
        crashed = kb.create_task(conn, title="crashed", assignee="alice")
        assert kb.claim_task(conn, crashed) is not None
        conn.execute(
            "UPDATE tasks SET worker_pid = 111, started_at = 1, "
            "claim_expires = 9999999999 WHERE id = ?",
            (crashed,),
        )
        timed_out = kb.create_task(
            conn,
            title="timed out",
            assignee="alice",
            max_runtime_seconds=1,
        )
        assert kb.claim_task(conn, timed_out) is not None
        conn.execute(
            "UPDATE tasks SET worker_pid = 222, started_at = 1, "
            "claim_expires = 9999999999 WHERE id = ?",
            (timed_out,),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = 1 WHERE task_id = ? AND ended_at IS NULL",
            (timed_out,),
        )
        promoted = kb.create_task(conn, title="promote", assignee="alice")
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (promoted,))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db_path = kb.kanban_db_path()
        bytes_before = db_path.read_bytes()
        semantic_before = conn.serialize()
        lock_path = db_path.with_name(db_path.name + ".dispatch.lock")
        lock_before = lock_path.exists()
        monkeypatch.setattr(
            kb,
            "reap_worker_zombies",
            lambda: (_ for _ in ()).throw(AssertionError("dry-run reaped a process")),
        )
        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("dry-run signalled a process")
            ),
        )
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == 222)

        result = kb.dispatch_once(
            conn,
            dry_run=True,
            profile_roster={"alice"},
            enable_continuations=True,
        )

        assert result.reclaimed == 1
        assert result.crashed == [crashed]
        assert result.timed_out == [timed_out]
        assert result.promoted == 1
        assert kb.get_task(conn, expired).status == "running"
        assert kb.get_task(conn, crashed).status == "running"
        assert kb.get_task(conn, timed_out).status == "running"
        assert kb.get_task(conn, promoted).status == "todo"
        assert conn.serialize() == semantic_before
        assert db_path.read_bytes() == bytes_before
        assert hashlib.sha256(db_path.read_bytes()).digest() == hashlib.sha256(bytes_before).digest()
        assert lock_path.exists() is lock_before


def _old_block(conn, *, title: str, kind: str | None, reason: str, priority: int) -> str:
    task_id = kb.create_task(conn, title=title, assignee="alice", priority=priority)
    assert kb.block_task(conn, task_id, reason=reason, kind=kind)
    conn.execute(
        "UPDATE tasks SET created_at = 1, started_at = 1 WHERE id = ?", (task_id,)
    )
    conn.execute(
        "UPDATE task_events SET created_at = 1 WHERE task_id = ?", (task_id,)
    )
    return task_id


def test_continuation_queue_is_bounded_ordered_idempotent_and_classified(
    kanban_home,
):
    with kb.connect() as conn:
        human = _old_block(
            conn,
            title="needs approval",
            kind="needs_input",
            reason="human approval required",
            priority=99,
        )
        ops = _old_block(
            conn,
            title="repair worktree",
            kind="capability",
            reason="workspace checkout unavailable",
            priority=1,
        )
        transient = _old_block(
            conn,
            title="retry probe",
            kind="transient",
            reason="temporary timeout",
            priority=3,
        )
        proof = _old_block(
            conn,
            title="unknown proof",
            kind=None,
            reason="unclassified state",
            priority=50,
        )
        review = _old_block(
            conn,
            title="review handoff",
            kind=None,
            reason="review-required: exact head",
            priority=0,
        )
        parent = kb.create_task(conn, title="known parent", assignee="alice")
        dependency = kb.create_task(
            conn,
            title="known dependency wait",
            assignee="alice",
            parents=[parent],
        )
        conn.execute(
            "UPDATE tasks SET created_at = 1 WHERE id = ?", (dependency,)
        )

        first = kb.queue_reconciliation_continuations(
            conn, {}, now=1000, limit=3,
        )
        assert [item["task_id"] for item in first] == [review, ops, transient]
        assert [item["classification"] for item in first] == [
            "review", "ops", "transient_retry",
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_continuations"
        ).fetchone()[0] == 3

        all_first = kb.queue_reconciliation_continuations(
            conn, {}, now=1000, limit=20,
        )
        all_second = kb.queue_reconciliation_continuations(
            conn, {}, now=1000, limit=20,
        )
        assert len(all_first) == len(all_second) == 5
        assert dependency not in {item["task_id"] for item in all_second}
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_continuations"
        ).fetchone()[0] == 4
        assert next(item for item in all_second if item["task_id"] == human)["state"] == "untouched"
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_continuations WHERE task_id = ?", (human,)
        ).fetchone()[0] == 0
        proof_row = next(item for item in all_second if item["task_id"] == proof)
        assert proof_row["classification"] == "proof_needed"
        assert proof_row["state"] == "proof_needed"

        retry_due = kb.queue_reconciliation_continuations(
            conn, {}, now=1060, limit=20,
        )
        transient_row = next(
            item for item in retry_due if item["task_id"] == transient
        )
        assert transient_row["disposition"] == "retry_due"
        assert transient_row["attempts"] == 1
        assert transient_row["next_retry_at"] > 1060
