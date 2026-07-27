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


def test_hallucinated_cards_fires_on_blocked_event():
    task = _task(status="ready")
    events = [
        _event("created", ts=100),
        _event("completion_blocked_hallucination", ts=200,
               phantom_cards=["t_bad1", "t_bad2"],
               verified_cards=["t_good1"]),
    ]
    # ``now=300`` keeps the synthetic event timestamps in scope without
    # tripping the stranded_in_ready rule (events are 100/200 epoch
    # which time.time() would treat as ~50yr old).
    diags = kd.compute_task_diagnostics(task, events, [], now=300)
    halluc = [d for d in diags if d.kind == "hallucinated_cards"]
    assert len(halluc) == 1
    d = halluc[0]
    assert d.severity == "error"
    assert d.data["phantom_ids"] == ["t_bad1", "t_bad2"]
    # Generic recovery actions always available; comment action too.
    kinds = [a.kind for a in d.actions]
    assert "comment" in kinds
    assert "reassign" in kinds


def test_hallucinated_cards_clears_on_subsequent_completion():
    task = _task(status="done")
    events = [
        _event("completion_blocked_hallucination", ts=100, phantom_cards=["t_x"]),
        _event("completed", ts=200, summary="retry worked"),
    ]
    diags = kd.compute_task_diagnostics(task, events, [])
    assert diags == []


def test_prose_phantom_refs_fires_after_clean_completion():
    # Prose scan emits its event AFTER the completed event in the DB
    # path, but a subsequent clean completion clears it. Phantom id
    # must be valid hex — the scanner regex is ``t_[a-f0-9]{8,}``.
    task = _task(status="done")
    events = [
        _event("completed", ts=100, summary="referenced t_bad", result_len=0),
        _event("suspected_hallucinated_references", ts=101,
               phantom_refs=["t_deadbeef99"], source="completion_summary"),
    ]
    diags = kd.compute_task_diagnostics(task, events, [])
    assert len(diags) == 1
    assert diags[0].kind == "prose_phantom_refs"
    assert diags[0].severity == "warning"
    assert diags[0].data["phantom_refs"] == ["t_deadbeef99"]


def test_prose_phantom_refs_clears_on_later_clean_edit():
    task = _task(status="done")
    events = [
        _event("completed", ts=100, summary="bad"),
        _event("suspected_hallucinated_references", ts=101,
               phantom_refs=["t_ffff0000cc"]),
        _event("edited", ts=200, fields=["result", "summary"]),
    ]
    diags = kd.compute_task_diagnostics(task, events, [])
    assert diags == []


def test_repeated_failures_fires_at_threshold_on_spawn():
    """A task with multiple spawn_failed runs gets a spawn-flavoured
    diagnostic (title mentions 'spawn', suggested action is ``doctor``).
    """
    task = _task(status="ready", consecutive_failures=3,
                 last_failure_error="Profile 'debugger' does not exist")
    runs = [
        _run(outcome="spawn_failed", run_id=1),
        _run(outcome="spawn_failed", run_id=2),
        _run(outcome="spawn_failed", run_id=3),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "repeated_failures"
    assert d.severity == "error"
    # CLI hints are what operators actually need here.
    suggested = [a.label for a in d.actions if a.suggested]
    assert any("doctor" in s for s in suggested)


def test_repeated_failures_fires_on_timeout_loop():
    """The rule surfaces for timeout loops too — that's the point of
    unifying the counter. Suggested action is 'check logs', not
    'fix profile'."""
    task = _task(status="ready", consecutive_failures=3,
                 last_failure_error="elapsed 600s > limit 300s")
    runs = [
        _run(outcome="timed_out", run_id=1),
        _run(outcome="timed_out", run_id=2),
        _run(outcome="timed_out", run_id=3),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "repeated_failures"
    assert d.data["most_recent_outcome"] == "timed_out"
    suggested = [a.label for a in d.actions if a.suggested]
    assert any("log" in s.lower() for s in suggested)


def test_repeated_failures_escalates_to_critical():
    task = _task(consecutive_failures=6, last_failure_error="boom")
    diags = kd.compute_task_diagnostics(task, [], [])
    assert diags[0].severity == "critical"


def test_repeated_failures_below_threshold_silent():
    task = _task(consecutive_failures=1)
    assert kd.compute_task_diagnostics(task, [], []) == []


def test_repeated_failures_default_matches_dispatcher_failure_limit():
    """Default dispatcher auto-blocks at 2 failures, so diagnostics must
    also surface at 2 instead of waiting for the stale threshold of 3.
    """
    task = _task(status="blocked", consecutive_failures=2,
                 last_failure_error="elapsed 600s > limit 300s")
    runs = [_run(outcome="timed_out", run_id=1)]
    diags = kd.compute_task_diagnostics(task, [], runs)
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    d = repeated[0]
    assert d.data["failure_threshold"] == 2
    assert d.data["failure_limit"] == 2
    assert "default 5" not in d.detail
    assert "configured for 2" in d.detail


def test_repeated_failures_derives_threshold_from_kanban_failure_limit():
    task = _task(status="ready", consecutive_failures=2,
                 last_failure_error="Profile 'debugger' does not exist")
    runs = [_run(outcome="spawn_failed", run_id=1)]
    assert kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 4}
    ) == []

    task = _task(status="blocked", consecutive_failures=4,
                 last_failure_error="Profile 'debugger' does not exist")
    diags = kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 4}
    )
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    assert repeated[0].data["failure_threshold"] == 4
    assert repeated[0].data["failure_limit"] == 4


def test_repeated_failures_explicit_threshold_overrides_failure_limit():
    task = _task(status="ready", consecutive_failures=3,
                 last_failure_error="Profile 'debugger' does not exist")
    runs = [_run(outcome="spawn_failed", run_id=1)]
    diags = kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 5, "failure_threshold": 3}
    )
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    assert repeated[0].data["failure_threshold"] == 3
    assert repeated[0].data["failure_limit"] == 5


def test_config_from_kanban_config_preserves_explicit_diagnostics_threshold():
    cfg = kd.config_from_kanban_config({
        "failure_limit": 5,
        "diagnostics": {"failure_threshold": 3},
    })
    assert cfg["failure_threshold"] == 3
    assert cfg["failure_limit"] == 5


def test_repeated_crashes_counts_trailing_streak_only():
    task = _task(status="ready", assignee="crashy")
    runs = [
        _run(outcome="completed", run_id=1),
        _run(outcome="crashed", run_id=2, error="OOM"),
        _run(outcome="crashed", run_id=3, error="OOM again"),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "repeated_crashes"
    # 2 consecutive crashes at the end → default threshold 2 → error severity.
    assert d.severity == "error"
    assert d.data["consecutive_crashes"] == 2


def test_repeated_crashes_breaks_on_recent_success():
    task = _task(status="ready", assignee="fixed")
    runs = [
        _run(outcome="crashed", run_id=1),
        _run(outcome="crashed", run_id=2),
        _run(outcome="completed", run_id=3),
    ]
    assert kd.compute_task_diagnostics(task, [], runs) == []


def test_repeated_crashes_escalates_on_many_crashes():
    task = _task(status="ready", assignee="x")
    runs = [_run(outcome="crashed", run_id=i) for i in range(1, 6)]  # 5 in a row
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert diags[0].severity == "critical"


def test_failure_rules_exempt_terminal_statuses():
    # A manual done (dashboard drag) ends no run, so the trailing crash
    # streak survives in run history — but done means done: neither
    # failure rule may keep flagging a terminal card.
    runs = [_run(outcome="crashed", run_id=1), _run(outcome="crashed", run_id=2)]
    for status in ("done", "archived"):
        task = _task(status=status, assignee="crashy", consecutive_failures=3)
        assert kd.compute_task_diagnostics(task, [], runs) == []


def test_failure_rules_exempt_running_retry():
    # Retrying a task (→ running) puts a fresh attempt in flight; its
    # in-flight run (no outcome) doesn't break the trailing crash scan,
    # so the past streak used to keep flagging over an active retry.
    # A running card must clear the failure/crash banner until this
    # attempt itself resolves.
    runs = [_run(outcome="crashed", run_id=1), _run(outcome="crashed", run_id=2)]
    task = _task(status="running", assignee="crashy", consecutive_failures=3)
    assert kd.compute_task_diagnostics(task, [], runs) == []


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


def test_stuck_in_blocked_silent_with_recent_comment():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48),
        _event("commented", ts=now - 3600 * 2, author="human"),
    ]
    assert kd.compute_task_diagnostics(task, events, [], now=now) == []


def test_stuck_in_blocked_silent_when_not_blocked():
    task = _task(status="ready")
    events = [_event("blocked", ts=1000)]
    assert kd.compute_task_diagnostics(task, events, [], now=9999999) == []


def test_repeated_crashes_surfaces_actual_error_in_title():
    """The title should lead with the actual error text so operators
    see WHAT broke (e.g. rate-limit, auth, OOM) without opening logs.
    """
    task = _task(status="ready", assignee="x")
    runs = [
        _run(outcome="crashed", run_id=1, error="openai: 429 Too Many Requests"),
        _run(outcome="crashed", run_id=2, error="openai: 429 Too Many Requests"),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert "429" in d.title
    assert "Too Many Requests" in d.title
    # Full error in detail.
    assert "429 Too Many Requests" in d.detail


def test_repeated_crashes_no_error_fallback_title():
    task = _task(status="ready", assignee="x")
    runs = [
        _run(outcome="crashed", run_id=1, error=None),
        _run(outcome="crashed", run_id=2, error=None),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert "no error recorded" in diags[0].title


def test_repeated_failures_surfaces_actual_error_in_title():
    task = _task(consecutive_failures=5,
                 last_failure_error="insufficient_quota: billing limit reached")
    diags = kd.compute_task_diagnostics(task, [], [])
    assert len(diags) == 1
    d = diags[0]
    assert "insufficient_quota" in d.title or "billing limit" in d.title
    assert "insufficient_quota" in d.detail


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


def test_diagnostics_sorted_critical_first():
    """A task with both a critical (many spawn failures) and a warning
    (prose phantoms) diagnostic should list the critical one first.

    Status must be non-terminal: done/archived are exempt from the
    failure rules (done means done). ``now=300`` keeps the synthetic
    timestamps from tripping stranded_in_ready — same dodge as above."""
    task = _task(status="ready", consecutive_failures=10,
                 last_failure_error="nope")
    events = [
        _event("completed", ts=100, summary="referenced t_missing"),
        _event("suspected_hallucinated_references", ts=101,
               phantom_refs=["t_missing11"]),
    ]
    diags = kd.compute_task_diagnostics(task, events, [], now=300)
    kinds = [d.kind for d in diags]
    assert kinds[0] == "repeated_failures"  # critical
    assert "prose_phantom_refs" in kinds


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


def test_broken_rule_is_isolated(monkeypatch):
    def _bad_rule(task, events, runs, now, cfg):
        raise RuntimeError("synthetic rule bug")

    # Insert a broken rule at the front of the registry; subsequent
    # rules should still run and produce their diagnostics.
    monkeypatch.setattr(kd, "_RULES", [_bad_rule] + kd._RULES)

    task = _task(consecutive_failures=5, last_failure_error="e")
    diags = kd.compute_task_diagnostics(task, [], [])
    # The broken rule silently drops, the real one still fires.
    kinds = [d.kind for d in diags]
    assert "repeated_failures" in kinds


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


def test_stranded_in_ready_silent_below_threshold():
    """A ready task only 10 min old should NOT fire."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [_event("created", ts=now - 10 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_skips_non_ready_status():
    """Tasks not in ready status are out of scope (running tasks have
    their own crash / failure rules)."""
    now = 100_000
    for status in ("running", "blocked", "done", "todo", "triage"):
        task = _task(status=status, assignee="demo")
        events = [_event("created", ts=now - 6 * 3600)]
        diags = kd.compute_task_diagnostics(task, events, [], now=now)
        assert [d for d in diags if d.kind == "stranded_in_ready"] == [], status


def test_stranded_in_ready_skips_unassigned_tasks():
    """Empty assignee = `skipped_unassigned` on the dispatcher already.
    Don't double-flag here."""
    now = 100_000
    task = _task(status="ready", assignee="", claim_lock=None)
    events = [_event("created", ts=now - 6 * 3600)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_skips_claimed_tasks():
    """A live claim_lock means a worker is on it — even an old one. Don't
    second-guess: the run-level liveness signal owns that decision."""
    now = 100_000
    task = _task(
        status="ready", assignee="demo", claim_lock="run_xyz",
    )
    events = [_event("created", ts=now - 6 * 3600)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_uses_latest_ready_transition():
    """When multiple ready-transition events exist, the rule should
    age-from the most recent — a task reclaimed 20 min ago is NOT
    stranded for 6h even if it was first created 6h ago."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    events = [
        _event("created", ts=now - 6 * 3600),       # 6 h ago
        _event("reclaimed", ts=now - 20 * 60),      # 20 min ago — wins
    ]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_severity_escalates_with_age():
    """warning → error → critical at 2x and 6x threshold."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    # Default threshold = 1800s.
    cases = [
        (45 * 60, "warning"),    # 1.5x → warning
        (90 * 60, "error"),      # 3x → error
        (4 * 3600, "critical"),  # 8x → critical
    ]
    for age, expected in cases:
        events = [_event("created", ts=now - age)]
        diags = kd.compute_task_diagnostics(task, events, [], now=now)
        stranded = [d for d in diags if d.kind == "stranded_in_ready"]
        assert len(stranded) == 1, f"age={age}"
        assert stranded[0].severity == expected, (
            f"age={age} expected {expected}, got {stranded[0].severity}"
        )


def test_stranded_in_ready_respects_config_override():
    """Config override changes the threshold."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    events = [_event("created", ts=now - 10 * 60)]  # 10 min
    # Default 30 min — wouldn't fire.
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []
    # Lower the threshold to 5 min — now it fires.
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
        config={"stranded_threshold_seconds": 5 * 60},
    )
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1


def test_stranded_in_ready_falls_back_to_created_at():
    """When events have no ready-transition kind, the rule falls back
    to the task's ``created_at`` so an ancient stranded task isn't
    invisible just because its events got pruned."""
    now = 100_000
    task = _task(
        status="ready", assignee="demo", created_at=now - 4 * 3600,
    )
    # No qualifying events.
    events = [_event("commented", ts=now - 100)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].data["age_seconds"] == 4 * 3600


def test_stranded_in_ready_works_on_real_db_row(kanban_home):
    """Round-trip through real kanban_db.connect() — confirms the rule
    works on sqlite3.Row objects, not just dicts."""
    import time as _t
    conn = kb.connect()
    try:
        # Create a task and force its created_at into the past.
        tid = kb.create_task(conn, title="stranded one", assignee="ghost")
        old_ts = int(_t.time()) - 90 * 60  # 90 min old
        conn.execute(
            "UPDATE tasks SET status = 'ready', created_at = ? WHERE id = ?",
            (old_ts, tid),
        )
        conn.commit()

        task_row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at",
            (tid,),
        ).fetchall())
        # Override created event timestamps too so age calc lines up.
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ?",
            (old_ts, tid),
        )
        conn.commit()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ?", (tid,),
        ).fetchall())

        diags = kd.compute_task_diagnostics(task_row, events, [])
        stranded = [d for d in diags if d.kind == "stranded_in_ready"]
        assert len(stranded) == 1
        assert stranded[0].data["assignee"] == "ghost"
    finally:
        conn.close()



# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")


def test_triage_aux_unavailable_silent_without_config_context():
    """Low-level callers passing no config dict should not see this rule."""
    diags = kd.compute_task_diagnostics(_triage_task(), [], [])
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_unavailable_silent_when_main_model_visible():
    """Default `provider: auto` falls back to the main model — no warning."""
    config = {
        "auxiliary": {},
        "model": {"provider": "openrouter", "default": "qwen/qwen3"},
        "kanban": {"auto_decompose": True},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_unavailable_silent_when_decomposer_explicit():
    """User explicitly configured decomposer → no warning, even without main."""
    config = {
        "auxiliary": {
            "kanban_decomposer": {"provider": "openrouter", "model": "qwen/qwen3"},
        },
        "kanban": {"auto_decompose": True},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_unavailable_fires_auto_decompose_on_no_fallback():
    """auto_decompose=True, no decomposer, no main model → warn about decomposer."""
    config = {
        "auxiliary": {},
        "kanban": {"auto_decompose": True},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    triage = [d for d in diags if d.kind == "triage_aux_unavailable"]
    assert len(triage) == 1
    d = triage[0]
    assert d.severity == "warning"
    assert "decomposer" in d.title.lower()
    assert d.data["auto_decompose"] is True
    assert d.data["primary_slot"] == "auxiliary.kanban_decomposer"
    suggested = [a for a in d.actions if a.suggested]
    assert suggested
    assert "auxiliary.kanban_decomposer" in suggested[0].payload["command"]


def test_triage_aux_unavailable_fires_auto_decompose_off_points_at_specifier():
    """auto_decompose=False → primary is specifier, not decomposer."""
    config = {
        "auxiliary": {},
        "kanban": {"auto_decompose": False},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    triage = [d for d in diags if d.kind == "triage_aux_unavailable"]
    assert len(triage) == 1
    d = triage[0]
    assert "specifier" in d.title.lower()
    assert d.data["auto_decompose"] is False
    assert d.data["primary_slot"] == "auxiliary.triage_specifier"
    # And it should offer the manual specify command as an action
    labels = [a.label for a in d.actions]
    assert any("hermes kanban specify" in l for l in labels)


def test_triage_aux_unavailable_skips_non_triage_tasks():
    config = {"auxiliary": {}, "kanban": {"auto_decompose": True}}
    task = _task(status="todo")
    diags = kd.compute_task_diagnostics(task, [], [], config=config)
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_status_recognises_auto_default_as_not_explicit():
    """Default `provider: auto` with empty fields → not 'explicit'."""
    status = kd.triage_aux_status({
        "auxiliary": {
            "kanban_decomposer": {"provider": "auto", "model": ""},
        },
        "kanban": {},
    })
    assert status is not None
    assert status["decomposer_explicit"] is False


def test_triage_aux_status_recognises_explicit_model_only():
    """Even with provider=auto, a non-empty model counts as explicit."""
    status = kd.triage_aux_status({
        "auxiliary": {
            "kanban_decomposer": {"provider": "auto", "model": "qwen/qwen3"},
        },
        "kanban": {},
    })
    assert status is not None
    assert status["decomposer_explicit"] is True


def test_config_from_runtime_config_carries_aux_and_model():
    cfg = kd.config_from_runtime_config({
        "kanban": {"failure_limit": 5, "auto_decompose": False},
        "auxiliary": {"kanban_decomposer": {"provider": "openrouter"}},
        "model": {"provider": "openrouter", "default": "qwen/qwen3"},
    })
    assert cfg["failure_threshold"] == 5
    assert cfg["kanban"]["auto_decompose"] is False
    assert cfg["auxiliary"]["kanban_decomposer"]["provider"] == "openrouter"
    assert cfg["model"]["default"] == "qwen/qwen3"


def test_config_from_runtime_config_handles_empty_input():
    assert kd.config_from_runtime_config(None) == {}
    assert kd.config_from_runtime_config({}) == {}


def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True
