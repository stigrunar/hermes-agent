"""Deterministic Ultra Phase B terminal worker-reaping regressions."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd
from hermes_cli import kanban_worker_process as kp


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


@pytest.fixture
def process_harness(monkeypatch: pytest.MonkeyPatch):
    states: dict[int, str] = {}
    signals: list[tuple[int, int]] = []

    def capture(root, previous=()):
        targets = list(previous)
        if root not in targets:
            targets.insert(0, root)
        return kp.TreeCapture("captured", tuple(targets))

    monkeypatch.setattr(kp, "capture_process_tree", capture)
    monkeypatch.setattr(kp, "identity_state", lambda identity: states.get(identity.pid, "alive"))
    monkeypatch.setattr(
        kp,
        "protected_process_identities",
        lambda extra=(): ({int(pid) for pid in extra}, set()),
    )
    monkeypatch.setattr(
        kb,
        "_worker_scope_state",
        lambda *_args, **_kwargs: kb._WorkerScopeStatus(
            None, "not-applicable", None, "direct",
        ),
    )

    def send(pid: int, sig: int) -> None:
        signals.append((pid, sig))

    return states, signals, send


@pytest.fixture
def trusted_scope(monkeypatch: pytest.MonkeyPatch):
    def activate() -> None:
        monkeypatch.setattr(
            kb,
            "_worker_scope_state",
            lambda *_args, **_kwargs: kb._WorkerScopeStatus(
                "hermes-test-worker.scope", "inactive", "test-manager",
                "systemd-user-scope", True,
            ),
        )

    return activate


def _active_worker(
    conn,
    *,
    title: str = "phase b",
    pid: int = 42420,
    create_time: float = 100.25,
    workspace_path: Path | None = None,
) -> tuple[str, int, kp.ProcessIdentity]:
    task_id = kb.create_task(conn, title=title, assignee="worker")
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None and claimed.current_run_id is not None
    run_id = claimed.current_run_id
    identity = kp.ProcessIdentity(pid, create_time)
    conn.execute(
        "UPDATE tasks SET worker_pid=?, workspace_path=COALESCE(?, workspace_path) "
        "WHERE id=?",
        (pid, str(workspace_path) if workspace_path else None, task_id),
    )
    conn.execute(
        "UPDATE task_runs SET worker_pid=?, worker_identity=?, worker_tree=? WHERE id=?",
        (pid, json.dumps(identity.to_dict()), json.dumps([identity.to_dict()]), run_id),
    )
    conn.commit()
    return task_id, run_id, identity


@pytest.mark.parametrize("terminal", ["complete", "block"])
def test_normal_terminal_request_waits_for_worker_exit_before_finalization(
    kanban_home, process_harness, trusted_scope, terminal,
):
    states, signals, send = process_harness
    with kb.connect() as conn:
        scratch = kanban_home / "kanban" / "workspaces" / f"scratch-{terminal}"
        scratch.mkdir(parents=True)
        task_id, run_id, identity = _active_worker(
            conn, title=terminal, workspace_path=scratch,
        )
        child = kb.create_task(conn, title="dependent", assignee="worker")
        kb.link_tasks(conn, task_id, child)
        assert kb.get_task(conn, child).status == "todo"

        if terminal == "complete":
            assert kb.complete_task(
                conn, task_id, result="ok", expected_run_id=run_id,
            )
        else:
            assert kb.block_task(
                conn, task_id, reason="wait", expected_run_id=run_id,
            )

        pending = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert pending.status == "running"
        assert pending.current_run_id == run_id
        assert pending.worker_pid == identity.pid
        assert run.reap_state == "terminal_requested"
        assert kb.get_task(conn, child).status == "todo"
        assert scratch.exists()
        preview = kb.dispatch_once(
            conn, dry_run=True,
            spawn_fn=lambda *_a, **_kw: pytest.fail("replacement spawned before reap"),
        )
        assert preview.spawned == []

        first = kb.reconcile_worker_reaps(
            conn, now=1000, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert first[0]["signal"] == "TERM"
        assert kb.get_task(conn, task_id).status == "running"
        assert signals == [(identity.pid, kp.TERM_SIGNAL)]

        states[identity.pid] = "gone"
        trusted_scope()
        second = kb.reconcile_worker_reaps(
            conn, now=1001, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert second[0]["state"] == "finalized"
        final = kb.get_task(conn, task_id)
        assert final.current_run_id is None
        assert final.worker_pid is None
        assert final.status == ("done" if terminal == "complete" else "blocked")
        if terminal == "complete":
            assert kb.get_task(conn, child).status == "ready"
            # The active child intentionally deferred parent scratch cleanup;
            # completing it after reaping releases the deferred workspace.
            assert kb.complete_task(conn, child, result="child done")
            assert not scratch.exists()
        else:
            assert kb.get_task(conn, child).status == "todo"


def test_reassignment_is_deferred_applied_atomically_and_retryable(
    kanban_home, process_harness, trusted_scope,
):
    states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="reassign")
        assert kb.reassign_task(
            conn, task_id, "new-profile", reclaim_first=True, reason="switch",
        )
        pending = kb.get_task(conn, task_id)
        assert pending.status == "running"
        assert pending.assignee == "worker"
        assert kb.get_run(conn, run_id).terminal_payload == {
            "action": "reassign",
            "assignee": "new-profile",
            "clear_failure_counter": True,
            "error": "manual_reassign: switch",
            "event_kind": "reclaimed",
            "event_payload": {
                "manual": True,
                "reason": "switch",
                "reassigned_to": "new-profile",
            },
            "outcome": "reclaimed",
            "run_status": "reclaimed",
            "task_status": "ready",
        }

        # A byte-for-byte semantic retry is idempotent; changing the reason
        # changes the durable action payload and must be a typed conflict.
        with pytest.raises(kb.TerminalTransitionConflict):
            kb.reassign_task(
                conn, task_id, "new-profile", reclaim_first=True, reason="retry",
            )
        assert kb.reassign_task(
            conn, task_id, "new-profile", reclaim_first=True, reason="switch",
        )
        assert kb.get_task(conn, task_id).assignee == "worker"
        assert len([
            event for event in kb.list_events(conn, task_id)
            if event.kind == "terminal_requested"
        ]) == 1

        states[identity.pid] = "gone"
        trusted_scope()
        decision = kb.reconcile_worker_reaps(
            conn, now=1050, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert decision[0]["state"] == "finalized"
        final = kb.get_task(conn, task_id)
        assert final.status == "ready"
        assert final.assignee == "new-profile"
        assert final.current_run_id is None
        assert signals == []
        assigned = [event for event in kb.list_events(conn, task_id) if event.kind == "assigned"]
        assert assigned[-1].payload == {
            "assignee": "new-profile", "deferred": True, "run_id": run_id,
        }


def test_historical_deferred_reassignment_never_mutates_new_current_run(
    kanban_home, process_harness, trusted_scope,
):
    states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, old_run, old_identity = _active_worker(conn, title="historical reassign")
        assert kb.reassign_task(
            conn, task_id, "old-request", reclaim_first=True,
        )

        current_identity = kp.ProcessIdentity(42421, 101.25)
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, worker_pid, "
            "worker_identity, worker_tree, started_at) VALUES (?, 'current', 'running', "
            "'current-lock', ?, ?, ?, ?)",
            (
                task_id,
                current_identity.pid,
                json.dumps(current_identity.to_dict()),
                json.dumps([current_identity.to_dict()]),
                int(time.time()),
            ),
        )
        current_run = int(cur.lastrowid)
        conn.execute(
            "UPDATE tasks SET status='running', assignee='current-profile', "
            "current_run_id=?, worker_pid=?, claim_lock='current-lock' WHERE id=?",
            (current_run, current_identity.pid, task_id),
        )
        conn.commit()

        states[old_identity.pid] = "gone"
        trusted_scope()
        decision = kb.reconcile_worker_reaps(
            conn, now=1150, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert decision[0]["state"] == "finalized"
        current = kb.get_task(conn, task_id)
        assert current.status == "running"
        assert current.assignee == "current-profile"
        assert current.current_run_id == current_run
        assert kb.get_run(conn, old_run).reap_state == "finalized_historical"
        assert kb.get_run(conn, current_run).reap_state is None
        assert signals == []


def test_active_review_verdict_reaps_before_source_and_successor_transition(
    kanban_home, process_harness, trusted_scope,
):
    states, signals, send = process_harness
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source", assignee="builder")
        review = kb.create_task(conn, title="review", assignee="reviewer", parents=[source])
        successor = kb.create_task(conn, title="successor")
        source_claim = kb.claim_task(conn, source, claimer="builder:1")
        assert source_claim is not None
        kb.register_review_handoff(conn, source, review, next_task_id=successor)
        assert kb.block_task(
            conn,
            source,
            reason="review-required: inspect",
            expected_run_id=source_claim.current_run_id,
        )
        review_claim = kb.claim_review_task(conn, review, claimer="reviewer:1")
        assert review_claim is not None and review_claim.current_run_id is not None
        review_run_id = review_claim.current_run_id
        identity = kp.ProcessIdentity(42500, 200.5)
        conn.execute(
            "UPDATE tasks SET worker_pid=? WHERE id=?", (identity.pid, review),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid=?, worker_identity=?, worker_tree=? WHERE id=?",
            (identity.pid, json.dumps(identity.to_dict()),
             json.dumps([identity.to_dict()]), review_run_id),
        )
        conn.commit()

        assert not kb.submit_review_verdict(
            conn,
            review,
            verdict="approved",
            summary="stale",
            expected_run_id=review_run_id + 1,
        )
        assert kb.submit_review_verdict(
            conn,
            review,
            verdict="approved",
            summary="verified",
            expected_run_id=review_run_id,
        )
        assert kb.get_task(conn, review).status == "running"
        assert kb.get_task(conn, source).status == "blocked"
        assert kb.get_task(conn, successor).status == "todo"
        assert kb.get_run(conn, review_run_id).reap_state == "terminal_requested"

        states[identity.pid] = "gone"
        trusted_scope()
        decision = kb.reconcile_worker_reaps(
            conn, now=1100, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert decision[0]["state"] == "finalized"
        assert signals == []
        assert kb.get_task(conn, review).status == "done"
        assert kb.get_task(conn, source).status == "done"
        assert kb.get_task(conn, successor).status == "ready"
        assert kb.get_run(conn, review_run_id).terminal_payload["verdict"] == "approved"


def test_historical_tree_descendant_of_current_run_is_protected(
    kanban_home, process_harness,
):
    _states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="historical", pid=42002)
        assert kb.complete_task(conn, task_id, result="old", expected_run_id=run_id)

        current = kb.create_task(conn, title="current", assignee="worker")
        claimed = kb.claim_task(conn, current, claimer="worker:current")
        assert claimed is not None and claimed.current_run_id is not None
        current_root = kp.ProcessIdentity(42001, 301.0)
        conn.execute(
            "UPDATE tasks SET worker_pid=? WHERE id=?", (current_root.pid, current),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid=?, worker_identity=?, worker_tree=? WHERE id=?",
            (
                current_root.pid,
                json.dumps(current_root.to_dict()),
                json.dumps([current_root.to_dict(), identity.to_dict()]),
                claimed.current_run_id,
            ),
        )
        conn.commit()

        decision = kb.reconcile_worker_reaps(
            conn, now=1200, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert decision[0]["state"] == "identity_unverifiable"
        assert decision[0]["reason"] == "protected_identity"
        assert signals == []
        assert kb.get_task(conn, task_id).status == "running"


def test_lease_takeover_between_signals_fences_old_owner(
    kanban_home, process_harness, monkeypatch,
):
    _states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, root = _active_worker(conn, title="lease takeover")
        child = kp.ProcessIdentity(42003, 302.0, parent_pid=root.pid,
                                   parent_create_time=root.create_time)
        conn.execute(
            "UPDATE task_runs SET worker_tree=? WHERE id=?",
            (json.dumps([root.to_dict(), child.to_dict()]), run_id),
        )
        conn.commit()
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)

        original = kb._signal_with_reap_lease
        calls = 0

        def signal_once_then_takeover(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                conn.execute(
                    "UPDATE task_runs SET reap_lease_expires=? WHERE id=?",
                    (kwargs.get("now", 1300), run_id),
                )
                conn.commit()
                assert kb._lease_reap_run(
                    conn, run_id, now=kwargs.get("now", 1300) + 1,
                    owner="owner-b", lease_seconds=15,
                )
            return result

        monkeypatch.setattr(kb, "_signal_with_reap_lease", signal_once_then_takeover)
        decision = kb.reconcile_worker_reaps(
            conn,
            now=1300,
            owner_id="owner-a",
            signal_fn=send,
            protected_pid_fn=lambda: set(),
        )
        assert len(signals) == 1
        assert decision[0]["state"] == "lease_lost"
        row = conn.execute(
            "SELECT reap_lease_owner, reap_state FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert row["reap_lease_owner"] == "owner-b"
        assert row["reap_state"] == "reaping"
        assert kb.get_task(conn, task_id).status == "running"


def test_lease_takeover_during_scope_census_fences_scope_stop(
    kanban_home, process_harness, monkeypatch,
):
    """A stale owner cannot stop a managed scope after lease takeover."""
    _states, signals, send = process_harness
    stopped_scopes: list[tuple[str, object]] = []
    with kb.connect() as conn:
        task_id, run_id, _identity = _active_worker(
            conn, title="scope lease takeover",
        )
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        monkeypatch.setattr(
            kb,
            "_worker_scope_state",
            lambda *_args, **_kwargs: kb._WorkerScopeStatus(
                "hermes-test-worker.scope", "active", "test-manager",
                "systemd-user-scope", True,
            ),
        )
        monkeypatch.setattr(
            kb,
            "_stop_systemd_user_scope",
            lambda unit, **kwargs: (
                stopped_scopes.append((unit, kwargs.get("manager_target"))) or True
            ),
        )

        def census_then_takeover():
            conn.execute(
                "UPDATE task_runs SET reap_lease_expires=999 WHERE id=?",
                (run_id,),
            )
            conn.commit()
            assert kb._lease_reap_run(
                conn, run_id, now=1000, owner="owner-b", lease_seconds=15,
            )
            return set()

        decision = kb.reconcile_worker_reaps(
            conn,
            clock_fn=lambda: 1000,
            lease_seconds=15,
            owner_id="owner-a",
            signal_fn=send,
            protected_pid_fn=census_then_takeover,
        )

        assert decision[0]["state"] == "lease_lost"
        assert stopped_scopes == []
        assert signals == []
        row = conn.execute(
            "SELECT reap_lease_owner, reap_state FROM task_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert row["reap_lease_owner"] == "owner-b"
        assert row["reap_state"] == "reaping"
        assert kb.get_task(conn, task_id).status == "running"


def test_fresh_pre_signal_lease_loss_returns_truthful_no_effect_outcome(
    kanban_home, process_harness, monkeypatch,
):
    """The final reconcile-level lease CAS is observable and effect-fenced."""
    states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(
            conn, title="fresh pre-signal lease loss",
        )
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        states[identity.pid] = "alive"
        monkeypatch.setattr(kb, "_renew_reap_lease", lambda *_args, **_kwargs: False)

        decision = kb.reconcile_worker_reaps(
            conn,
            clock_fn=lambda: 1400,
            owner_id="owner-a",
            signal_fn=send,
            protected_pid_fn=lambda: set(),
        )

        assert decision == [
            {
                "task_id": task_id,
                "run_id": run_id,
                "attempt_uuid": decision[0]["attempt_uuid"],
                "lease_owner": "owner-a",
                "state": "lease_lost",
            }
        ]
        assert signals == []
        assert kb.get_task(conn, task_id).status == "running"


def test_capture_time_lease_loss_is_truthful_and_stops_all_effects(
    kanban_home, process_harness, monkeypatch,
):
    """A worker-tree capture CAS loss is a fenced no-effect outcome."""
    states, signals, send = process_harness
    stopped_scopes: list[str] = []
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(
            conn, title="capture lease loss",
        )
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        states[identity.pid] = "alive"

        def capture_after_takeover(root, previous=()):
            conn.execute(
                "UPDATE task_runs SET reap_attempt_uuid='owner-b-attempt', "
                "reap_lease_owner='owner-b', reap_lease_expires=2000 WHERE id=?",
                (run_id,),
            )
            conn.commit()
            return kp.TreeCapture("captured", tuple(previous) or (root,))

        monkeypatch.setattr(kp, "capture_process_tree", capture_after_takeover)
        monkeypatch.setattr(
            kb,
            "_stop_systemd_user_scope",
            lambda unit, **_kwargs: stopped_scopes.append(unit) or True,
        )

        decision = kb.reconcile_worker_reaps(
            conn,
            clock_fn=lambda: 1401,
            owner_id="owner-a",
            signal_fn=send,
            protected_pid_fn=lambda: pytest.fail(
                "protection census ran after capture-time lease loss"
            ),
        )

        assert decision[0]["state"] == "lease_lost"
        assert decision[0]["task_id"] == task_id
        assert decision[0]["run_id"] == run_id
        assert stopped_scopes == []
        assert signals == []
        assert kb.get_task(conn, task_id).status == "running"


def test_gone_root_after_incomplete_direct_capture_stays_fail_closed(
    kanban_home, process_harness, monkeypatch,
):
    states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="incomplete capture")
        assert kb.complete_task(conn, task_id, result="unsafe", expected_run_id=run_id)
        original = kp.capture_process_tree

        def incomplete_when_gone(root, previous=()):
            if states.get(root.pid) == "gone" and len(list(previous)) <= 1:
                targets = tuple(previous) or (root,)
                return kp.TreeCapture("incomplete", targets, "root_gone_before_tree_capture")
            return original(root, previous)

        monkeypatch.setattr(kp, "capture_process_tree", incomplete_when_gone)
        states[identity.pid] = "gone"
        decision = kb.reconcile_worker_reaps(
            conn, now=1400, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert decision[0]["state"] == "identity_unverifiable"
        assert decision[0]["reason"] == "root_gone_before_tree_capture"
        assert signals == []
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.get_run(conn, run_id).reap_state == "identity_unverifiable"


def test_capture_gone_root_without_prior_descendants_is_incomplete(monkeypatch):
    root = kp.ProcessIdentity(43000, 400.0)
    monkeypatch.setattr(kp, "identity_state", lambda _identity: "gone")
    capture = kp.capture_process_tree(root, [root])
    assert capture.state == "incomplete"
    assert capture.reason == "root_gone_before_tree_capture"


def test_capture_does_not_admit_late_reparented_descendant(monkeypatch):
    root = kp.ProcessIdentity(43100, 401.0)

    class Proc:
        def __init__(self, pid, ppid, created):
            self.info = {
                "pid": pid, "ppid": ppid, "create_time": created,
                "status": "running",
            }

    class FakePsutil:
        STATUS_ZOMBIE = "zombie"

        @staticmethod
        def process_iter(_attrs):
            # The child has already reparented before this first census; it
            # was never observed under the owned root and must not be adopted.
            return [Proc(root.pid, 1, root.create_time), Proc(43101, 1, 402.0)]

    monkeypatch.setattr(kp, "_psutil", lambda: FakePsutil)
    monkeypatch.setattr(kp, "identity_state", lambda _identity: "alive")
    capture = kp.capture_process_tree(root)
    assert [(item.pid, item.create_time) for item in capture.targets] == [
        (root.pid, root.create_time),
    ]


def test_direct_run_with_late_fork_after_census_never_finalizes(
    kanban_home, process_harness, monkeypatch,
):
    """A direct census cannot prove that a child forked after the census is gone."""
    states, signals, send = process_harness
    monkeypatch.setattr(
        kb,
        "_worker_scope_state",
        lambda *_args, **_kwargs: kb._WorkerScopeStatus(
            None, "not-applicable", None, "direct",
        ),
    )
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="late fork")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        census = {"done": False}

        def census_only(root, previous=()):
            census["done"] = True
            return kp.TreeCapture("captured", tuple(previous) or (root,))

        monkeypatch.setattr(kp, "capture_process_tree", census_only)
        states[identity.pid] = "gone"
        decision = kb.reconcile_worker_reaps(
            conn, clock_fn=lambda: 1200, signal_fn=send,
            protected_pid_fn=lambda: set(),
        )
        assert census["done"]
        assert decision[0]["state"] == "identity_unverifiable"
        assert decision[0]["reason"] == "complete_boundary_untrusted"
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.get_run(conn, run_id).reap_state == "identity_unverifiable"
        assert signals == []


def test_malformed_tree_receipts_fail_closed_as_a_whole():
    with pytest.raises(ValueError, match="worker_tree_malformed"):
        kb._decode_process_tree(
            json.dumps([{"pid": 1, "create_time": 1.0}, {"pid": "bad"}])
        )
    assert kp.ProcessIdentity.from_mapping({"pid": 1, "create_time": float("nan")}) is None


def test_signal_exact_uses_pidfd_identity_bound_delivery(monkeypatch):
    calls = []
    monkeypatch.setattr(kp, "identity_state", lambda _identity: "alive")
    monkeypatch.setattr(kp.os, "pidfd_open", lambda pid, flags: (calls.append(("open", pid, flags)) or 77), raising=False)
    monkeypatch.setattr(kp.signal, "pidfd_send_signal", lambda fd, sig: calls.append(("send", fd, sig)), raising=False)
    monkeypatch.setattr(kp.os, "close", lambda fd: calls.append(("close", fd)))
    identity = kp.ProcessIdentity(43110, 403.0)
    assert kp.signal_exact(
        identity, kp.TERM_SIGNAL, protected_pids=set(), protected_identities=set(),
    ) == "alive"
    assert calls == [
        ("open", identity.pid, 0),
        ("send", 77, kp.TERM_SIGNAL),
        ("close", 77),
    ]


def test_signal_exact_revalidates_identity_after_pidfd_open_before_send(monkeypatch):
    calls = []
    monkeypatch.setattr(kp.os, "pidfd_open", lambda pid, flags: (calls.append("open") or 77), raising=False)
    monkeypatch.setattr(kp.signal, "pidfd_send_signal", lambda *_args: calls.append("send"), raising=False)
    monkeypatch.setattr(kp.os, "close", lambda fd: calls.append("close"))
    monkeypatch.setattr(kp, "identity_state", lambda _identity: "reused")
    result = kp.signal_exact(
        kp.ProcessIdentity(43111, 404.0), kp.TERM_SIGNAL,
        protected_pids=set(), protected_identities=set(),
    )
    assert result == "reused"
    assert calls == ["open", "close"]


def test_signal_lease_rollover_after_slow_protection_census_fences_send(
    kanban_home, process_harness,
):
    """A census that crosses expiry cannot deliver a TERM using the old lease."""
    _states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="slow census")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        attempt = kb._lease_reap_run(
            conn, run_id, now=104, owner="slow-census", lease_seconds=1,
        )
        assert attempt
        clock_values = iter([104, 106])
        state, reason = kb._signal_with_reap_lease(
            conn, run_id, attempt, "slow-census", identity, kp.TERM_SIGNAL,
            now=104, lease_seconds=1, protected_pid_fn=lambda: set(),
            signal_fn=send, clock_fn=lambda: next(clock_values),
        )
        assert (state, reason) == ("lease_lost", None)
        assert signals == []
        row = conn.execute(
            "SELECT reap_term_intent_at FROM task_runs WHERE id=?", (run_id,),
        ).fetchone()
        assert row["reap_term_intent_at"] is None


@pytest.mark.parametrize("mismatch", ["task_pid", "run_pid", "identity_pid", "tree_root"])
def test_current_worker_receipt_pid_and_tree_mismatch_fences_reaping(
    kanban_home, process_harness, mismatch,
):
    _states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="candidate")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        protected_task, protected_run, protected_identity = _active_worker(
            conn, title=f"protected {mismatch}", pid=44000, create_time=440.0,
        )
        if mismatch == "task_pid":
            conn.execute("UPDATE tasks SET worker_pid=44001 WHERE id=?", (protected_task,))
        elif mismatch == "run_pid":
            conn.execute("UPDATE task_runs SET worker_pid=44001 WHERE id=?", (protected_run,))
        elif mismatch == "identity_pid":
            wrong = kp.ProcessIdentity(44001, 440.0)
            conn.execute(
                "UPDATE task_runs SET worker_identity=? WHERE id=?",
                (json.dumps(wrong.to_dict()), protected_run),
            )
        else:
            wrong = kp.ProcessIdentity(44001, 440.0)
            conn.execute(
                "UPDATE task_runs SET worker_tree=? WHERE id=?",
                (json.dumps([wrong.to_dict()]), protected_run),
            )
        conn.commit()
        _states[identity.pid] = "gone"
        decision = kb.reconcile_worker_reaps(
            conn, clock_fn=lambda: 1260, signal_fn=send,
            protected_pid_fn=lambda: set(),
        )
        assert decision[0]["state"] == "identity_unverifiable"
        assert signals == []


def test_malformed_service_pid_fences_protection_as_a_whole(
    kanban_home, process_harness,
):
    states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="bad service pid")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        states[identity.pid] = "gone"
        decision = kb.reconcile_worker_reaps(
            conn, clock_fn=lambda: 1250, signal_fn=send,
            protected_pid_fn=lambda: [float("nan")],
        )
        assert decision[0]["state"] == "identity_unverifiable"
        assert decision[0]["reason"] == "identity_or_protection_unknown"
        assert kb.get_task(conn, task_id).status == "running"
        assert signals == []


def test_malformed_current_run_protected_receipt_fences_reaper(
    kanban_home, process_harness,
):
    states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="bad protected receipt")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        replacement = kb.create_task(conn, title="replacement", assignee="worker")
        claim = kb.claim_task(conn, replacement)
        assert claim is not None and claim.current_run_id is not None
        conn.execute(
            "UPDATE tasks SET worker_pid=? WHERE id=?",
            (99991, replacement),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid=?, worker_identity=?, worker_tree=? WHERE id=?",
            (99991, json.dumps({"pid": "bad", "create_time": 1.0}), "[]", claim.current_run_id),
        )
        conn.commit()
        states[identity.pid] = "gone"
        decision = kb.reconcile_worker_reaps(
            conn, clock_fn=lambda: 1260, signal_fn=send,
            protected_pid_fn=lambda: set(),
        )
        assert decision[0]["state"] == "identity_unverifiable"
        assert decision[0]["reason"] == "identity_or_protection_unknown"
        assert kb.get_task(conn, task_id).status == "running"
        assert signals == []


@pytest.mark.parametrize("receipt", ["task_only", "run_only", "task_and_run"])
def test_current_pid_only_receipt_protects_historical_target(
    kanban_home, process_harness, receipt,
):
    """Every validated raw current PID path protects the same historical PID."""
    states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(
            conn, title="historical pid-only target", pid=44100,
        )
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)

        current = kb.create_task(conn, title=f"current {receipt}", assignee="worker")
        claim = kb.claim_task(conn, current)
        assert claim is not None and claim.current_run_id is not None
        task_pid = identity.pid if receipt in {"task_only", "task_and_run"} else None
        run_pid = identity.pid if receipt in {"run_only", "task_and_run"} else None
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (task_pid, current))
        conn.execute(
            "UPDATE task_runs SET worker_pid=?, worker_identity=NULL, worker_tree=NULL "
            "WHERE id=?",
            (run_pid, claim.current_run_id),
        )
        conn.commit()

        states[identity.pid] = "alive"
        decision = kb.reconcile_worker_reaps(
            conn, clock_fn=lambda: 1265, signal_fn=send,
            protected_pid_fn=lambda: set(),
        )

        assert decision[0]["state"] == "identity_unverifiable"
        assert decision[0]["reason"] == "protected_identity"
        assert signals == []
        assert kb.get_task(conn, task_id).status == "running"


def test_current_pid_only_receipt_mismatch_fences_protection_census(
    kanban_home, process_harness,
):
    """Conflicting raw task/run PIDs fail the entire protection census closed."""
    states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(
            conn, title="raw pid mismatch candidate", pid=44110,
        )
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        current = kb.create_task(conn, title="raw pid mismatch", assignee="worker")
        claim = kb.claim_task(conn, current)
        assert claim is not None and claim.current_run_id is not None
        conn.execute("UPDATE tasks SET worker_pid=44110 WHERE id=?", (current,))
        conn.execute(
            "UPDATE task_runs SET worker_pid=44111, worker_identity=NULL, "
            "worker_tree=NULL WHERE id=?",
            (claim.current_run_id,),
        )
        conn.commit()

        states[identity.pid] = "gone"
        decision = kb.reconcile_worker_reaps(
            conn, clock_fn=lambda: 1266, signal_fn=send,
            protected_pid_fn=lambda: set(),
        )

        assert decision[0]["state"] == "identity_unverifiable"
        assert decision[0]["reason"] == "identity_or_protection_unknown"
        assert signals == []


def test_reaped_run_is_restart_finalizable_after_crash(kanban_home, process_harness, monkeypatch):
    states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="restart reaped")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        states[identity.pid] = "gone"
        original = kb._finalize_reaped_run
        monkeypatch.setattr(kb, "_finalize_reaped_run", lambda *_args, **_kwargs: False)
        # A restart may resume only after the exact managed boundary is known.
        monkeypatch.setattr(
            kb,
            "_worker_scope_state",
            lambda *_args, **_kwargs: kb._WorkerScopeStatus(
                "hermes-test-worker.scope", "inactive", "test-manager",
                "systemd-user-scope", True,
            ),
        )
        first = kb.reconcile_worker_reaps(
            conn, now=1500, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert first[0]["state"] == "reaped"
        assert kb.get_run(conn, run_id).reap_state == "reaped"
        monkeypatch.setattr(kb, "_finalize_reaped_run", original)
        second = kb.reconcile_worker_reaps(
            conn, now=1501, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert second[0]["state"] == "finalized"
        assert kb.get_task(conn, task_id).status == "done"
        assert signals == []


def test_accepted_dependency_block_finalizes_after_parent_completes(
    kanban_home, process_harness, trusted_scope,
):
    states, signals, send = process_harness
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="dependency", assignee="worker")
        task_id, run_id, identity = _active_worker(conn, title="dependent")
        kb.link_tasks(conn, parent_id=parent, child_id=task_id)

        assert kb.block_task(
            conn,
            task_id,
            reason="waiting for dependency",
            kind="dependency",
            dependency_task_id=parent,
            expected_run_id=run_id,
        )
        assert kb.get_run(conn, run_id).reap_state == "terminal_requested"

        # The accepted intent remains finalizable even if its dependency gate
        # becomes satisfied while the worker is exiting.
        assert kb.complete_task(conn, parent, result="dependency complete")
        states[identity.pid] = "gone"
        trusted_scope()
        decision = kb.reconcile_worker_reaps(
            conn, now=1510, signal_fn=send, protected_pid_fn=lambda: set(),
        )

        assert decision[0]["state"] == "finalized"
        assert kb.get_run(conn, run_id).ended_at is not None
        assert kb.get_task(conn, task_id).status == "ready"
        assert signals == []


@pytest.mark.parametrize("parent_status", sorted(kb.TERMINAL_STATUSES))
def test_active_dependency_block_rejects_terminal_parent_before_intent(
    kanban_home, process_harness, parent_status,
):
    """A terminal parent cannot start phase-one reaping or strand its child."""
    _states, signals, _send = process_harness
    with kb.connect() as conn:
        parent = kb.create_task(conn, title=f"{parent_status} parent", assignee="worker")
        task_id, run_id, identity = _active_worker(
            conn, title=f"child of {parent_status}",
        )
        kb.link_tasks(conn, parent_id=parent, child_id=task_id)
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (parent_status, parent))
        conn.commit()
        before_events = [(event.id, event.kind) for event in kb.list_events(conn, task_id)]

        with pytest.raises(
            ValueError, match="dependency_wait_requires_unfinished_parent",
        ):
            kb.block_task(
                conn,
                task_id,
                reason="waiting for terminal parent",
                kind="dependency",
                dependency_task_id=parent,
                expected_run_id=run_id,
            )

        run = kb.get_run(conn, run_id)
        assert run.reap_state is None
        assert run.terminal_payload is None
        assert [(event.id, event.kind) for event in kb.list_events(conn, task_id)] == before_events
        child = kb.get_task(conn, task_id)
        assert child.status == "running"
        assert child.current_run_id == run_id
        assert child.worker_pid == identity.pid
        assert signals == []


def test_dispatch_surfaces_stale_terminal_conflict_without_false_outcome(
    kanban_home, process_harness, monkeypatch,
):
    """Dispatch reports a stale/complete conflict without replacing completion."""
    _states, signals, _send = process_harness
    with kb.connect() as conn:
        task_id, run_id, _identity = _active_worker(conn, title="stale conflict")
        old = int(time.time()) - (5 * 3600)
        conn.execute(
            "UPDATE tasks SET started_at=?, last_heartbeat_at=NULL WHERE id=?",
            (old, task_id),
        )
        conn.execute("UPDATE task_runs SET started_at=? WHERE id=?", (old, run_id))
        conn.commit()
        assert kb.complete_task(
            conn, task_id, result="accepted completion", expected_run_id=run_id,
        )
        monkeypatch.setattr(kb, "refresh_worker_process_ownership", lambda *_a, **_kw: [])
        monkeypatch.setattr(kb, "reconcile_worker_reaps", lambda *_a, **_kw: [])

        result = kb.dispatch_once(
            conn,
            stale_timeout_seconds=4 * 3600,
            spawn_fn=lambda *_a, **_kw: pytest.fail("conflicted task respawned"),
        )

        assert result.stale == []
        assert result.maintenance_conflicts == [{
            "task_id": task_id,
            "state": "conflict",
            "reason": "terminal_transition_conflict",
            "detail": "terminal_transition_conflict",
        }]
        run = kb.get_run(conn, run_id)
        assert run.terminal_payload["action"] == "complete"
        assert kb.get_task(conn, task_id).status == "running"
        assert not any(event.kind == "stale" for event in kb.list_events(conn, task_id))
        assert signals == []


def test_review_handoff_cannot_mutate_an_accepted_completion(
    kanban_home, process_harness, trusted_scope,
):
    states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="accepted completion")
        review = kb.create_task(conn, title="late review", assignee="reviewer")
        successor = kb.create_task(conn, title="late successor")

        assert kb.complete_task(
            conn, task_id, result="accepted", expected_run_id=run_id,
        )
        with pytest.raises(ValueError, match="terminal transition already accepted"):
            kb.register_review_handoff(
                conn, task_id, review, next_task_id=successor,
            )
        assert kb._review_handoff_row(conn, source_task_id=task_id) is None

        states[identity.pid] = "gone"
        trusted_scope()
        decision = kb.reconcile_worker_reaps(
            conn, now=1520, signal_fn=send, protected_pid_fn=lambda: set(),
        )

        assert decision[0]["state"] == "finalized"
        assert kb.get_run(conn, run_id).ended_at is not None
        assert kb.get_task(conn, task_id).status == "done"
        assert kb.get_task(conn, review).status == "ready"
        assert kb.get_task(conn, successor).status == "ready"
        assert signals == []


def test_historical_reaped_review_verdict_finalizes_without_graph_mutation(
    kanban_home, process_harness, monkeypatch,
):
    states, signals, send = process_harness
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source", assignee="builder")
        review = kb.create_task(conn, title="review", assignee="reviewer", parents=[source])
        successor = kb.create_task(conn, title="successor")
        source_claim = kb.claim_task(conn, source, claimer="builder:1")
        assert source_claim is not None
        kb.register_review_handoff(conn, source, review, next_task_id=successor)
        assert kb.block_task(
            conn, source, reason="review-required: inspect",
            expected_run_id=source_claim.current_run_id,
        )
        review_claim = kb.claim_review_task(conn, review, claimer="reviewer:1")
        assert review_claim is not None and review_claim.current_run_id is not None
        old_run = review_claim.current_run_id
        old_identity = kp.ProcessIdentity(42600, 210.5)
        conn.execute(
            "UPDATE tasks SET worker_pid=? WHERE id=?",
            (old_identity.pid, review),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid=?, worker_identity=?, worker_tree=? WHERE id=?",
            (old_identity.pid, json.dumps(old_identity.to_dict()),
             json.dumps([old_identity.to_dict()]), old_run),
        )
        conn.commit()
        assert kb.submit_review_verdict(
            conn, review, verdict="approved", summary="old verdict",
            expected_run_id=old_run,
        )
        new_identity = kp.ProcessIdentity(42601, 211.5)
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, worker_pid, "
            "worker_identity, worker_tree, started_at) VALUES (?, 'reviewer', 'running', "
            "'new-lock', ?, ?, ?, ?)",
            (review, new_identity.pid, json.dumps(new_identity.to_dict()),
             json.dumps([new_identity.to_dict()]), int(time.time())),
        )
        new_run = int(cur.lastrowid)
        conn.execute(
            "UPDATE tasks SET status='running', current_run_id=?, worker_pid=?, "
            "claim_lock='new-lock' WHERE id=?",
            (new_run, new_identity.pid, review),
        )
        conn.execute(
            "UPDATE task_runs SET reap_state='reaped', reap_completed_at=?, "
            "reap_lease_expires=? WHERE id=?",
            (1269, 1270, old_run),
        )
        conn.commit()
        monkeypatch.setattr(
            kb,
            "_worker_scope_state",
            lambda *_args, **_kwargs: kb._WorkerScopeStatus(
                "hermes-test-worker.scope", "inactive", "test-manager",
                "systemd-user-scope", True,
            ),
        )
        states[old_identity.pid] = "gone"
        decision = kb.reconcile_worker_reaps(
            conn, clock_fn=lambda: 1271, signal_fn=send,
            protected_pid_fn=lambda: set(),
        )
        assert decision[0]["state"] == "finalized"
        assert kb.get_run(conn, old_run).reap_state == "finalized_historical"
        assert kb.get_task(conn, review).status == "running"
        assert kb.get_task(conn, review).current_run_id == new_run
        handoff = kb._review_handoff_row(conn, source_task_id=source)
        assert handoff["state"] == "active"
        assert kb.get_task(conn, source).status == "blocked"
        assert kb.get_task(conn, successor).status == "todo"
        historical_events = conn.execute(
            "SELECT kind FROM task_events WHERE run_id=? ORDER BY id", (old_run,),
        ).fetchall()
        historical_kinds = [row["kind"] for row in historical_events]
        assert "review_verdict_historical" in historical_kinds
        assert not ({"completed", "blocked", "failed", "review_verdict"} & set(historical_kinds))
        assert signals == []


def test_terminal_transition_conflict_is_serialized_by_maintenance(
    kanban_home, process_harness, monkeypatch,
):
    states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="maintenance conflict")
        conn.execute(
            "UPDATE tasks SET started_at=? WHERE id=?",
            (int(time.time()) - 60, task_id),
        )
        conn.commit()
        monkeypatch.setattr(
            kb,
            "_request_terminal_transition",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                kb.TerminalTransitionConflict("terminal_transition_conflict")
            ),
        )
        states[identity.pid] = "gone"
        assert kb.detect_crashed_workers(conn) == []
        conflicts = kb.detect_crashed_workers._last_transition_conflicts
        assert conflicts[0]["state"] == "conflict"
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.get_run(conn, run_id).reap_state is None


@pytest.mark.parametrize("delivered", [False, True])
def test_signal_intent_uncertainty_never_repeats_phase(
    kanban_home, process_harness, delivered,
):
    states, signals, _send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="signal uncertainty")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)

        def uncertain(pid, sig):
            if delivered:
                signals.append((pid, sig))
            raise RuntimeError("simulated crash")

        first = kb.reconcile_worker_reaps(
            conn, now=1600, signal_fn=uncertain, protected_pid_fn=lambda: set(),
        )
        assert first[0]["state"] == "manual_recovery_required"
        row = conn.execute(
            "SELECT reap_term_intent_at, reap_term_sent_at FROM task_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert row["reap_term_intent_at"] is not None
        assert row["reap_term_sent_at"] is None
        states[identity.pid] = "gone"
        second = kb.reconcile_worker_reaps(
            conn, now=1700, signal_fn=lambda *_: signals.append(("repeat", "bad")),
            protected_pid_fn=lambda: set(),
        )
        assert second[0]["state"] == "manual_recovery_required"
        assert ("repeat", "bad") not in signals


def test_rollback_preflight_refuses_live_new_action_and_drains_reaped(
    kanban_home, process_harness, trusted_scope,
):
    states, _signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="rollback")
        assert kb.reassign_task(conn, task_id, None, reclaim_first=True)
        blocked = kb.phase_b_rollback_preflight(conn)
        assert blocked["ok"] is False
        states[identity.pid] = "gone"
        trusted_scope()
        kb.reconcile_worker_reaps(
            conn, now=1800, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        drained = kb.phase_b_rollback_preflight(conn)
        assert drained["ok"] is True
        assert kb.get_task(conn, task_id).assignee is None


@pytest.mark.parametrize(
    "action",
    [
        "complete",
        "block",
        "timed_out",
        "reclaimed",
        "crashed",
        "cancelled",
        "review_verdict",
        "reassign",
    ],
)
def test_rollback_preflight_refuses_every_pending_terminal_action(
    kanban_home, process_harness, action,
):
    with kb.connect() as conn:
        task_id, run_id, _identity = _active_worker(
            conn, title=f"rollback {action}",
        )
        assert kb._request_terminal_transition(
            conn,
            task_id,
            action=action,
            payload={"receipt": action},
            expected_run_id=run_id,
        )

        blocked = kb.phase_b_rollback_preflight(conn)
        assert blocked == {
            "ok": False,
            "reason": "phase_b_action_pending",
            "run_id": run_id,
            "task_id": task_id,
        }


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        ("timeout", "ready"),
        ("reclaimed", "ready"),
        ("crash", "ready"),
        ("cancel", "cancelled"),
    ],
)
def test_failure_cancel_and_reclaim_are_two_phase(
    kanban_home, process_harness, trusted_scope, action, expected_status,
):
    states, _signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title=action)
        if action == "timeout":
            conn.execute(
                "UPDATE tasks SET max_runtime_seconds=1 WHERE id=?", (task_id,)
            )
            conn.execute(
                "UPDATE task_runs SET max_runtime_seconds=1, started_at=? WHERE id=?",
                (int(time.time()) - 10, run_id),
            )
            assert kb.enforce_max_runtime(conn) == [task_id]
        elif action == "reclaimed":
            assert kb.reclaim_task(conn, task_id, reason="operator")
        elif action == "crash":
            states[identity.pid] = "gone"
            conn.execute(
                "UPDATE tasks SET started_at=? WHERE id=?",
                (int(time.time()) - 60, task_id),
            )
            assert kb.detect_crashed_workers(conn) == [task_id]
        else:
            assert kb.cancel_task(conn, task_id, reason="operator", expected_run_id=run_id)

        assert kb.get_task(conn, task_id).status == "running"
        assert kb.get_task(conn, task_id).current_run_id == run_id
        states[identity.pid] = "gone"
        trusted_scope()
        decision = kb.reconcile_worker_reaps(
            conn, now=2000, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert decision[0]["state"] == "finalized"
        assert kb.get_task(conn, task_id).status == expected_status


def test_legacy_pid_only_live_row_fails_closed(
    kanban_home, process_harness,
):
    _states, signals, send = process_harness
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="legacy", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        run_id = claimed.current_run_id
        conn.execute("UPDATE tasks SET worker_pid=31337 WHERE id=?", (task_id,))
        conn.execute("UPDATE task_runs SET worker_pid=31337 WHERE id=?", (run_id,))
        conn.commit()
        assert kb.complete_task(conn, task_id, result="unsafe", expected_run_id=run_id)
        assert kb.get_run(conn, run_id).reap_state == "identity_unverifiable"
        decision = kb.reconcile_worker_reaps(
            conn, now=3000, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert decision[0]["state"] == "identity_unverifiable"
        assert signals == []
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.get_task(conn, task_id).current_run_id == run_id


def test_pid_reuse_is_nonownership_and_never_signalled(
    kanban_home, process_harness,
):
    states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="pid reuse")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        states[identity.pid] = "reused"
        decision = kb.reconcile_worker_reaps(
            conn, now=4000, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert decision[0]["state"] == "identity_unverifiable"
        assert signals == []
        assert kb.get_task(conn, task_id).status == "running"


@pytest.mark.parametrize("protected_source", ["managed_service", "current_replacement"])
def test_protected_identities_are_never_reaped(
    kanban_home, process_harness, protected_source,
):
    _states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="protected")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        if protected_source == "current_replacement":
            replacement = kb.create_task(conn, title="replacement", assignee="worker")
            replacement_claim = kb.claim_task(conn, replacement)
            replacement_run = replacement_claim.current_run_id
            conn.execute(
                "UPDATE tasks SET worker_pid=? WHERE id=?", (identity.pid, replacement),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid=?, worker_identity=?, worker_tree=? WHERE id=?",
                (identity.pid, json.dumps(identity.to_dict()),
                 json.dumps([identity.to_dict()]), replacement_run),
            )
            conn.commit()
            protected_fn = lambda: set()
        else:
            protected_fn = lambda: {identity.pid}
        decision = kb.reconcile_worker_reaps(
            conn, now=5000, signal_fn=send, protected_pid_fn=protected_fn,
        )
        assert decision[0]["state"] == "identity_unverifiable"
        assert decision[0]["reason"] == "protected_identity"
        assert signals == []
        assert kb.get_task(conn, task_id).status == "running"


def test_detached_descendant_is_retained_only_from_captured_ancestry(monkeypatch):
    root = kp.ProcessIdentity(10, 1.0)
    detached = kp.ProcessIdentity(13, 4.0, parent_pid=1, parent_create_time=3.0)

    class Proc:
        def __init__(self, pid, ppid, created):
            self.info = {"pid": pid, "ppid": ppid, "create_time": created, "status": "running"}

    class FakePsutil:
        STATUS_ZOMBIE = "zombie"
        NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        ZombieProcess = type("ZombieProcess", (Exception,), {})
        AccessDenied = type("AccessDenied", (Exception,), {})

        @staticmethod
        def process_iter(_attrs):
            return [Proc(10, 1, 1.0), Proc(11, 10, 2.0), Proc(12, 11, 3.0), Proc(99, 1, 9.0)]

    monkeypatch.setattr(kp, "_psutil", lambda: FakePsutil)
    monkeypatch.setattr(kp, "identity_state", lambda _identity: "alive")
    capture = kp.capture_process_tree(root, [root, detached])
    assert {(item.pid, item.create_time) for item in capture.targets} == {
        (10, 1.0), (11, 2.0), (12, 3.0), (13, 4.0),
    }


def test_term_survivor_escalates_to_kill_and_unknown_never_signals(
    kanban_home, process_harness,
):
    states, signals, send = process_harness
    current_now = {"value": 6000}
    clock = lambda: current_now["value"]
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="survivor")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        kb.reconcile_worker_reaps(
            conn, clock_fn=clock, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        current_now["value"] = 6001
        kb.reconcile_worker_reaps(
            conn, clock_fn=clock, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        current_now["value"] = 6002
        kb.reconcile_worker_reaps(
            conn, clock_fn=clock, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert signals == [(identity.pid, kp.TERM_SIGNAL), (identity.pid, kp.KILL_SIGNAL)]
        states[identity.pid] = "unknown"
        before = list(signals)
        current_now["value"] = 6020
        decision = kb.reconcile_worker_reaps(
            conn, clock_fn=clock, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert decision[0]["state"] == "identity_unverifiable"
        assert signals == before
        assert kb.get_task(conn, task_id).status == "running"


def test_lease_single_owner_expiry_takeover_and_renewal(
    kanban_home, process_harness,
):
    with kb.connect() as conn:
        task_id, run_id, _identity = _active_worker(conn, title="lease")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        attempt_a = kb._lease_reap_run(
            conn, run_id, now=100, owner="owner-a", lease_seconds=5,
        )
        assert attempt_a
        assert kb._lease_reap_run(
            conn, run_id, now=103, owner="owner-b", lease_seconds=5,
        ) is None
        assert kb._renew_reap_lease(
            conn, run_id, attempt_a, now=104, owner="owner-a", lease_seconds=5,
        )
        assert kb._lease_reap_run(
            conn, run_id, now=108, owner="owner-b", lease_seconds=5,
        ) is None
        attempt_b = kb._lease_reap_run(
            conn, run_id, now=110, owner="owner-b", lease_seconds=5,
        )
        assert attempt_b and attempt_b != attempt_a


def test_reaper_uses_fresh_wall_time_when_tick_time_is_stale(
    kanban_home, process_harness, monkeypatch,
):
    states, _signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="fresh clock")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        states[identity.pid] = "alive"
        monkeypatch.setattr(kb.time, "time", lambda: 200)
        decision = kb.reconcile_worker_reaps(
            conn, now=100, signal_fn=send, protected_pid_fn=lambda: set(),
            lease_seconds=15,
        )
        assert decision[0]["signal"] == "TERM"
        row = conn.execute(
            "SELECT reap_term_sent_at, reap_lease_expires FROM task_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert row["reap_term_sent_at"] == 200
        assert row["reap_lease_expires"] == 215


def test_heartbeat_accepts_deterministic_clock_seam(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="clock heartbeat", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        assert kb.heartbeat_worker(conn, task_id, clock_fn=lambda: 321)
        task = kb.get_task(conn, task_id)
        assert task.last_heartbeat_at == 321


def test_simultaneous_reaper_ticks_have_zero_signal_overlap(
    kanban_home, process_harness,
):
    _states, _signals, _send = process_harness
    with kb.connect() as conn:
        task_id, run_id, _identity = _active_worker(conn, title="concurrent")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)

    entered = threading.Event()
    release = threading.Event()
    active = 0
    max_active = 0
    guard = threading.Lock()

    def send(_pid, _sig):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        entered.set()
        release.wait(timeout=2)
        with guard:
            active -= 1

    results: list[list[dict]] = []

    def tick(owner):
        with kb.connect() as thread_conn:
            results.append(kb.reconcile_worker_reaps(
                thread_conn, now=7000, owner_id=owner, signal_fn=send,
                protected_pid_fn=lambda: set(),
            ))

    first = threading.Thread(target=tick, args=("owner-a",))
    second = threading.Thread(target=tick, args=("owner-b",))
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    second.join(timeout=2)
    release.set()
    first.join(timeout=2)
    assert max_active == 1
    assert sum(bool(result) for result in results) == 1


def test_restart_resumes_after_expired_lease_and_historical_run_never_mutates_current(
    kanban_home, process_harness, monkeypatch,
):
    states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, old_run, old_identity = _active_worker(conn, title="history", pid=801)
        assert kb.complete_task(conn, task_id, result="old", expected_run_id=old_run)
        # Simulate corrupt pre-Phase-B replacement history: a newer exact run is
        # current while the older terminal request still needs reconciliation.
        new_identity = kp.ProcessIdentity(802, 8.02)
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, worker_pid, "
            "worker_identity, worker_tree, started_at) VALUES (?, 'worker', 'running', "
            "'replacement', ?, ?, ?, ?)",
            (task_id, new_identity.pid, json.dumps(new_identity.to_dict()),
             json.dumps([new_identity.to_dict()]), int(time.time())),
        )
        new_run = int(cur.lastrowid)
        conn.execute(
            "UPDATE tasks SET status='running', current_run_id=?, worker_pid=?, "
            "claim_lock='replacement' WHERE id=?",
            (new_run, new_identity.pid, task_id),
        )
        conn.commit()
        attempt = kb._lease_reap_run(
            conn, old_run, now=8000, owner="old-dispatcher", lease_seconds=5,
        )
        assert attempt
        states[old_identity.pid] = "gone"
        monkeypatch.setattr(
            kb,
            "_worker_scope_state",
            lambda *_args, **_kwargs: kb._WorkerScopeStatus(
                "hermes-test-worker.scope", "inactive", "test-manager",
                "systemd-user-scope", True,
            ),
        )
        decision = kb.reconcile_worker_reaps(
            conn, now=8006, owner_id="new-dispatcher", signal_fn=send,
            protected_pid_fn=lambda: set(),
        )
        assert decision[0]["attempt_uuid"] != attempt
        assert decision[0]["state"] == "finalized"
        current = kb.get_task(conn, task_id)
        assert current.status == "running"
        assert current.current_run_id == new_run
        assert current.worker_pid == new_identity.pid
        assert signals == []
        assert kb.get_run(conn, old_run).reap_state == "finalized_historical"


def test_identity_unknown_retries_eventually_give_up_without_finalizing(
    kanban_home, process_harness,
):
    states, signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="gave up")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        states[identity.pid] = "unknown"
        decision = []
        for attempt in range(kb._REAP_GIVE_UP_ATTEMPTS):
            current_now = 9000 + attempt * 20
            decision = kb.reconcile_worker_reaps(
                conn, clock_fn=lambda current_now=current_now: current_now,
                owner_id=f"owner-{attempt}",
                signal_fn=send, protected_pid_fn=lambda: set(),
            )
        assert decision[0]["state"] == "manual_recovery_required"
        assert signals == []
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.get_task(conn, task_id).current_run_id == run_id


def test_timeout_breaker_gave_up_happens_only_after_reap(
    kanban_home, process_harness, trusted_scope,
):
    states, _signals, send = process_harness
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="breaker")
        conn.execute(
            "UPDATE tasks SET max_runtime_seconds=1, max_retries=1 WHERE id=?",
            (task_id,),
        )
        conn.execute(
            "UPDATE task_runs SET max_runtime_seconds=1, started_at=? WHERE id=?",
            (int(time.time()) - 10, run_id),
        )
        assert kb.enforce_max_runtime(conn) == [task_id]
        assert kb.get_task(conn, task_id).status == "running"
        assert not any(event.kind == "gave_up" for event in kb.list_events(conn, task_id))
        states[identity.pid] = "gone"
        trusted_scope()
        kb.reconcile_worker_reaps(
            conn, now=9500, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert kb.get_task(conn, task_id).status == "blocked"
        assert any(event.kind == "gave_up" for event in kb.list_events(conn, task_id))


def test_diagnostics_show_lease_facts_without_corrupting_functional_payload(
    kanban_home, process_harness,
):
    with kb.connect() as conn:
        task_id, run_id, _identity = _active_worker(conn, title="diagnostic")
        functional = "Authorization: Bearer sk-proj-functional-value"
        assert kb.complete_task(
            conn, task_id, result=functional, expected_run_id=run_id,
        )
        attempt = kb._lease_reap_run(
            conn, run_id, now=10000, owner="diagnostic-owner", lease_seconds=10,
        )
        run = kb.get_run(conn, run_id)
        task = kb.get_task(conn, task_id)
        assert run.terminal_payload["result"] == functional
        diagnostic = kd._terminal_active_run_diagnostic(task, [run], 10001)
        assert diagnostic is not None
        assert diagnostic.kind == "worker_reaping"
        assert diagnostic.data["attempt_uuid"] == attempt
        assert diagnostic.data["lease_owner"] == "diagnostic-owner"
        serialized = json.dumps(diagnostic.to_dict(), sort_keys=True)
        assert "sk-proj-functional-value" not in serialized


def test_schema_migration_is_additive_and_idempotent(kanban_home):
    with kb.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}
        assert {
            "worker_identity", "worker_tree", "terminal_payload",
            "terminal_requested_at", "reap_state", "reap_attempt_uuid",
            "reap_lease_owner", "reap_lease_expires", "reap_heartbeat_at",
            "reap_term_sent_at", "reap_kill_sent_at", "reap_attempts",
            "reap_error", "reap_completed_at", "reap_term_intent_at",
            "reap_kill_intent_at", "reap_signal_progress",
        } <= columns
        kb._migrate_add_optional_columns(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM pragma_index_list('task_runs') "
            "WHERE name='idx_runs_reap'"
        ).fetchone()[0] == 1


def test_control_plane_self_and_ancestors_are_protected():
    protected_pids, protected_exact = kp.protected_process_identities()
    assert os.getpid() in protected_pids
    self_identity = kp.read_identity(os.getpid())
    assert self_identity is not None
    assert (self_identity.pid, self_identity.create_time) in protected_exact
