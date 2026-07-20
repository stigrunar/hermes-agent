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
            None, "not-applicable", None, "direct"
        ),
    )

    def send(pid: int, sig: int) -> None:
        signals.append((pid, sig))

    return states, signals, send


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
    kanban_home, process_harness, terminal,
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
    kanban_home, process_harness, action, expected_status,
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
        assert decision[0]["state"] == "finalized"
        assert signals == []
        assert kb.get_task(conn, task_id).status == "done"


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
    with kb.connect() as conn:
        task_id, run_id, identity = _active_worker(conn, title="survivor")
        assert kb.complete_task(conn, task_id, result="ok", expected_run_id=run_id)
        kb.reconcile_worker_reaps(
            conn, now=6000, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        kb.reconcile_worker_reaps(
            conn, now=6001, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        kb.reconcile_worker_reaps(
            conn, now=6002, signal_fn=send, protected_pid_fn=lambda: set(),
        )
        assert signals == [(identity.pid, kp.TERM_SIGNAL), (identity.pid, kp.KILL_SIGNAL)]
        states[identity.pid] = "unknown"
        before = list(signals)
        decision = kb.reconcile_worker_reaps(
            conn, now=6020, signal_fn=send, protected_pid_fn=lambda: set(),
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
    kanban_home, process_harness,
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
            decision = kb.reconcile_worker_reaps(
                conn, now=9000 + attempt * 20, owner_id=f"owner-{attempt}",
                signal_fn=send, protected_pid_fn=lambda: set(),
            )
        assert decision[0]["state"] == "gave_up"
        assert signals == []
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.get_task(conn, task_id).current_run_id == run_id


def test_timeout_breaker_gave_up_happens_only_after_reap(
    kanban_home, process_harness,
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
            "reap_error", "reap_completed_at",
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
