"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


def _diagnostic_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(kb.SCHEMA_SQL)
    conn.execute(
        "CREATE INDEX idx_runs_task_outcome_id "
        "ON task_runs(task_id, outcome, id)"
    )
    return conn


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------
















def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48






def test_review_gate_retry_storm_classifies_legacy_invalid_workspace():
    task = _task(
        id="t_oldgate",
        status="blocked",
        consecutive_failures=4,
        last_failure_error="workspace resolution failed",
    )
    events = [
        _event(
            "review_gate_invalid_workspace",
            ts=100,
            classification="invalid_worktree_source",
            source_task_id="t_source",
            review_task_id="t_oldgate",
            next_task_id="t_next",
            board="historical-review-board",
            error="no board default_workdir",
        ),
    ]
    diags = kd.compute_task_diagnostics(task, events, [], now=200)
    assert [d.kind for d in diags] == ["review_gate_retry_storm"]
    assert diags[0].severity == "critical"
    assert "--replace-review-gate" in diags[0].actions[0].payload["command"]
    assert "--board historical-review-board" in diags[0].actions[0].payload["command"]
    assert diags[0].data["board"] == "historical-review-board"
    assert diags[0].to_dict()["actions"][0]["payload"]["command"].startswith(
        "hermes kanban --board historical-review-board"
    )
    assert "no board default_workdir" in diags[0].detail


def test_review_gate_retry_storm_clears_after_repair_event():
    task = _task(id="t_oldgate", status="archived")
    events = [
        _event(
            "review_gate_invalid_workspace", ts=100,
            source_task_id="t_source", review_task_id="t_oldgate",
        ),
        _event("review_gate_replaced", ts=200),
    ]
    assert kd.compute_task_diagnostics(task, events, [], now=300) == []


def test_repeated_crashes_truncates_huge_tracebacks():
    """Full Python tracebacks can be tens of KB. The title stays one
    line (≤160 chars); the detail caps at 500 chars + ellipsis so the
    card doesn't explode visually."""
    huge = "Traceback (most recent call last):\n" + ("  File\n" * 500)
    task = _task(status="ready")
    runs = [
        _run(outcome="crashed", run_id=1, error=huge),
        _run(outcome="crashed", run_id=2, error=huge),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    d = diags[0]
    # Title only the first line, capped.
    assert "\n" not in d.title
    assert len(d.title) < 250
    # Detail contains the snippet with ellipsis.
    assert d.detail.endswith("…") or len(d.detail) < 700


# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


def test_bounded_history_loader_ignores_retained_audit_lifetime():
    """Irrelevant retained rows do not change selected history or active rules."""
    now = 2_000_000
    conn = _diagnostic_conn()
    try:
        task_id = kb.create_task(conn, title="bounded", assignee="worker")
        conn.execute(
            "UPDATE tasks SET status='ready', consecutive_failures=1, "
            "created_at=? WHERE id=?",
            (now - 100, task_id),
        )
        conn.execute("DELETE FROM task_events WHERE task_id=?", (task_id,))

        def add_event(kind, created_at, payload=None):
            cursor = conn.execute(
                "INSERT INTO task_events(task_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, kind, payload, created_at),
            )
            return int(cursor.lastrowid)

        def add_run(outcome, error=None):
            cursor = conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, "
                "outcome, error) VALUES (?, 'done', ?, ?, ?, ?)",
                (task_id, now - 10, now - 5, outcome, error),
            )
            return int(cursor.lastrowid)

        conn.executemany(
            "INSERT INTO task_events(task_id, kind, payload, created_at) "
            "VALUES (?, 'commented', NULL, ?)",
            [(task_id, now - 200_000 + i) for i in range(2_000)],
        )
        add_event(
            "completion_blocked_hallucination",
            now - 1_000,
            '{"phantom_cards":["t_resolved000"]}',
        )
        add_event("completed", now - 900)
        active_warning = add_event(
            "completion_blocked_hallucination",
            now - 800,
            '{"phantom_cards":["t_active0000"]}',
        )
        active_prose = add_event(
            "suspected_hallucinated_references",
            now - 700,
            '{"phantom_refs":["t_deadbeef00"]}',
        )
        cycle_cutoff = now - 24 * 3600
        boundary_unblock = add_event("unblocked", cycle_cutoff)
        boundary_block = add_event("blocked", cycle_cutoff)
        latest_ready = add_event("promoted", now - 100)

        for _ in range(1_000):
            add_run("crashed", "resolved")
        add_run("completed")
        active_crash_1 = add_run("crashed", "first active")
        active_crash_2 = add_run("crashed", "latest active")
        latest_failure = add_run("timed_out", "timeout")
        conn.commit()

        history = kd.load_diagnostic_history(conn, [task_id], now=now)
        assert history.event_ids[task_id] == [
            boundary_unblock,
            boundary_block,
            active_warning,
            active_prose,
            latest_ready,
        ]
        assert history.run_ids[task_id] == [
            active_crash_1,
            active_crash_2,
            latest_failure,
        ]
        assert history.selected_event_count == 5
        assert history.selected_run_count == 3

        before = [
            diagnostic.to_dict()
            for diagnostic in kd.compute_database_diagnostics(
                conn, [task_id], now=now,
            )[task_id]
        ]
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        full_history = [
            diagnostic.to_dict()
            for diagnostic in kd.compute_task_diagnostics(
                task,
                conn.execute(
                    "SELECT * FROM task_events WHERE task_id=? "
                    "ORDER BY created_at, id",
                    (task_id,),
                ).fetchall(),
                conn.execute(
                    "SELECT * FROM task_runs WHERE task_id=? ORDER BY started_at, id",
                    (task_id,),
                ).fetchall(),
                now=now,
            )
        ]
        assert before == full_history

        conn.executemany(
            "INSERT INTO task_events(task_id, kind, payload, created_at) "
            "VALUES (?, 'commented', NULL, ?)",
            [(task_id, now - 300_000 + i) for i in range(5_000)],
        )
        conn.commit()
        grown = kd.load_diagnostic_history(conn, [task_id], now=now)
        after = [
            diagnostic.to_dict()
            for diagnostic in kd.compute_database_diagnostics(
                conn, [task_id], now=now,
            )[task_id]
        ]

        assert grown.event_ids == history.event_ids
        assert grown.run_ids == history.run_ids
        assert grown.selected_event_count == 5
        assert grown.selected_run_count == 3
        assert len(kb.list_events(conn, task_id)) == 7_007
        assert len(kb.list_runs(conn, task_id)) == 1_004
        assert after == before
    finally:
        conn.close()


def test_bounded_history_matches_fleet_chronology_and_id_ties():
    """Bounded rows use the base's timestamp chronology with id tie-breaks."""
    now = 400
    conn = _diagnostic_conn()
    try:
        chronology_task = kb.create_task(
            conn, title="backfilled chronology", assignee="worker",
        )
        tie_task = kb.create_task(conn, title="id ties", assignee="worker")
        conn.execute(
            "DELETE FROM task_events WHERE task_id IN (?, ?)",
            (chronology_task, tie_task),
        )
        chronology_ids = []
        for kind, created_at in (
            ("blocked", 100),
            ("unblocked", 300),
            ("blocked", 200),
        ):
            chronology_ids.append(int(conn.execute(
                "INSERT INTO task_events(task_id, kind, created_at) "
                "VALUES (?, ?, ?)",
                (chronology_task, kind, created_at),
            ).lastrowid))

        tie_event_ids = []
        for kind, created_at in (
            ("blocked", 100),
            ("unblocked", 200),
            ("blocked", 200),
        ):
            tie_event_ids.append(int(conn.execute(
                "INSERT INTO task_events(task_id, kind, created_at) "
                "VALUES (?, ?, ?)",
                (tie_task, kind, created_at),
            ).lastrowid))
        tie_run_ids = []
        for started_at in (300, 200, 200):
            tie_run_ids.append(int(conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, "
                "outcome, error) VALUES (?, 'crashed', ?, ?, 'crashed', 'boom')",
                (tie_task, started_at, started_at + 1),
            ).lastrowid))
        conn.commit()

        config = {
            "block_cycle_threshold": 1,
            "block_cycle_window_seconds": 1_000,
        }
        history = kd.load_diagnostic_history(
            conn,
            [chronology_task, tie_task],
            now=now,
            config=config,
        )
        assert history.event_ids[chronology_task] == [
            chronology_ids[0],
            chronology_ids[2],
            chronology_ids[1],
        ]
        assert history.event_ids[tie_task] == tie_event_ids
        assert history.run_ids[tie_task] == [
            tie_run_ids[1],
            tie_run_ids[2],
            tie_run_ids[0],
        ]

        for task_id in (chronology_task, tie_task):
            bounded = kd.compute_database_diagnostics(
                conn, [task_id], now=now, config=config,
            )
            task = conn.execute(
                "SELECT * FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            full = kd.compute_task_diagnostics(
                task,
                conn.execute(
                    "SELECT * FROM task_events WHERE task_id=? "
                    "ORDER BY created_at, id",
                    (task_id,),
                ).fetchall(),
                conn.execute(
                    "SELECT * FROM task_runs WHERE task_id=? "
                    "ORDER BY started_at, id",
                    (task_id,),
                ).fetchall(),
                now=now,
                config=config,
            )
            assert [
                diagnostic.to_dict()
                for diagnostic in bounded.get(task_id, [])
            ] == [diagnostic.to_dict() for diagnostic in full]

        assert chronology_task not in kd.compute_database_diagnostics(
            conn, [chronology_task], now=now, config=config,
        )
        assert {
            diagnostic.kind
            for diagnostic in kd.compute_database_diagnostics(
                conn, [tie_task], now=now, config=config,
            )[tie_task]
        } == {
            "block_unblock_cycling",
            "repeated_crashes",
        }
    finally:
        conn.close()


def test_bounded_history_preserves_review_gate_and_terminal_run_diagnostics():
    conn = _diagnostic_conn()
    try:
        task_id = kb.create_task(conn, title="landed rules", assignee="worker")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=4 WHERE id=?",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, kind, payload, created_at) "
            "VALUES (?, 'review_gate_invalid_workspace', ?, 100)",
            (task_id, '{"board":"release","source_task_id":"t_source"}'),
        )
        run = conn.execute(
            "INSERT INTO task_runs(task_id, status, worker_pid, started_at, "
            "reap_state, terminal_requested_at) "
            "VALUES (?, 'running', 1234, 90, 'reap_pending', 95)",
            (task_id,),
        ).lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id=?, worker_pid=1234 WHERE id=?",
            (run, task_id),
        )
        conn.commit()

        history = kd.load_diagnostic_history(conn, [task_id], now=200)
        assert any(
            row["kind"] == "review_gate_invalid_workspace"
            for row in history.events_by_task[task_id]
        )
        assert [row["id"] for row in history.runs_by_task[task_id]] == [run]
        diagnostics = kd.compute_database_diagnostics(
            conn, [task_id], now=200,
        )[task_id]
        assert {diagnostic.kind for diagnostic in diagnostics} == {
            "review_gate_retry_storm",
            "worker_identity_unverifiable",
        }
    finally:
        conn.close()


def test_bounded_history_query_plans_use_diagnostic_indexes():
    conn = _diagnostic_conn()
    try:
        task_id = kb.create_task(conn, title="plans", assignee="worker")
        plans = kd.diagnostic_history_query_plans(conn, [task_id])
        details = [
            detail
            for group in plans.values()
            for plan in group
            for detail in plan
        ]
        assert not any("USE TEMP B-TREE" in detail for detail in details)
        assert not any("SCAN task_events" in detail for detail in details)
        assert not any("SCAN task_runs" in detail for detail in details)
        assert any("idx_events_task_kind_id" in detail for detail in details)
        assert any("idx_events_task_kind_time" in detail for detail in details)
        assert any("idx_runs_task_outcome_id" in detail for detail in details)
    finally:
        conn.close()


def test_bounded_history_loader_chunks_large_fleets():
    conn = _diagnostic_conn()
    try:
        task_ids = [
            kb.create_task(conn, title=f"chunk {i}", assignee="worker")
            for i in range(1_005)
        ]
        history = kd.load_diagnostic_history(conn, task_ids)
        assert set(history.events_by_task) == set(task_ids)
        assert set(history.runs_by_task) == set(task_ids)
        assert history.selected_event_count == len(task_ids)
        assert history.selected_run_count == 0
        assert kd.compute_database_diagnostics(conn, task_ids) == {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"




# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True
