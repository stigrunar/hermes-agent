from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from hermes_cli import kanban_db as kb


def _make_legacy_db(path: Path) -> None:
    """Write a kanban DB with the pre-AUTOINCREMENT (TEXT PK) schema for the
    four tables #35096 affects, keeping every other table current so the
    additive-column migration runs cleanly on top.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(kb.SCHEMA_SQL)
    conn.executescript(
        """
        DROP TABLE task_events;
        DROP TABLE task_comments;
        DROP TABLE task_runs;
        DROP TABLE kanban_notify_subs;
        CREATE TABLE task_comments (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE task_events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE task_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            profile TEXT, status TEXT NOT NULL, started_at INTEGER NOT NULL);
        CREATE TABLE kanban_notify_subs (task_id TEXT NOT NULL, platform TEXT NOT NULL,
            chat_id TEXT NOT NULL, thread_id TEXT NOT NULL DEFAULT '', user_id TEXT,
            created_at INTEGER NOT NULL, last_event_id TEXT,
            PRIMARY KEY (task_id, platform, chat_id, thread_id));
        """
    )
    conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('task-1', 'T', 'done', 1000)")
    conn.execute("INSERT INTO task_comments VALUES ('c-1', 'task-1', 'agent', 'hi', 1500)")
    conn.execute("INSERT INTO task_events VALUES ('e-1', 'task-1', 'completed', NULL, 2000)")
    conn.execute("INSERT INTO task_events VALUES ('e-2', 'task-1', 'blocked', NULL, 2100)")
    conn.execute("INSERT INTO task_runs VALUES ('r-1', 'task-1', 'default', 'done', 1000)")
    conn.execute(
        "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at, last_event_id) "
        "VALUES ('task-1', 'telegram', '123', 1000, 'e-1')"
    )
    conn.commit()
    conn.close()


def _setup_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="legacy")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    return db_path


def _table_struct(conn: sqlite3.Connection, table: str):
    cols = [
        (r["name"], (r["type"] or "").upper(), r["notnull"], r["pk"])
        for r in conn.execute(f"PRAGMA table_info({table})")
    ]
    idx = sorted(
        r["name"]
        for r in conn.execute(f"PRAGMA index_list({table})")
        if not r["name"].startswith("sqlite_")
    )
    return cols, idx




def test_legacy_text_pk_tables_rebuilt_to_integer_autoincrement(tmp_path, monkeypatch):
    """A pre-AUTOINCREMENT DB is migrated in place: id columns become INTEGER
    PKs, ``last_event_id`` becomes INTEGER, data is preserved, and indexes
    are recreated (DROP TABLE would otherwise take them down)."""
    db_path = _setup_home(tmp_path, monkeypatch)
    _make_legacy_db(db_path)

    with kb.connect(db_path) as conn:
        for table in ("task_events", "task_comments", "task_runs"):
            id_col = {r["name"]: r for r in conn.execute(f"PRAGMA table_info({table})")}["id"]
            assert id_col["type"].upper() == "INTEGER" and id_col["pk"] == 1

        lei = {r["name"]: r for r in conn.execute("PRAGMA table_info(kanban_notify_subs)")}
        assert lei["last_event_id"]["type"].upper() == "INTEGER"
        assert "delivery_metadata" in lei

        # Data preserved across the rebuild.
        assert len(conn.execute("SELECT * FROM task_events").fetchall()) == 2
        assert conn.execute("SELECT body FROM task_comments").fetchone()["body"] == "hi"
        assert len(conn.execute("SELECT * FROM task_runs").fetchall()) == 1
        # The non-numeric legacy cursor cannot be preserved, so this legacy
        # row is safely baselined after event ids have been rebuilt.
        sub = conn.execute(
            "SELECT last_event_id, baseline_event_id FROM kanban_notify_subs"
        ).fetchone()
        task_max = conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id='task-1'"
        ).fetchone()[0]
        assert sub["last_event_id"] == task_max
        assert sub["baseline_event_id"] == task_max

        # Indexes restored, including idx_events_run (added by the additive pass).
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for name in ("idx_events_task", "idx_events_run", "idx_comments_task",
                     "idx_events_task_kind_id", "idx_events_task_kind_time",
                     "idx_runs_task", "idx_runs_task_outcome_id",
                     "idx_runs_status", "idx_notify_task"):
            assert name in indexes

        # AUTOINCREMENT actually works after the rebuild.
        conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('task-1', 'completed', 3000)")
        new_id = conn.execute("SELECT id FROM task_events ORDER BY id DESC LIMIT 1").fetchone()["id"]
        assert isinstance(new_id, int) and new_id >= 1




def test_migration_is_idempotent(tmp_path, monkeypatch):
    """Re-opening an already-migrated DB is a no-op and leaves data intact."""
    db_path = _setup_home(tmp_path, monkeypatch)
    _make_legacy_db(db_path)

    with kb.connect(db_path):
        pass
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path) as conn:
        id_col = {r["name"]: r for r in conn.execute("PRAGMA table_info(task_events)")}["id"]
        assert id_col["type"].upper() == "INTEGER"
        assert len(conn.execute("SELECT * FROM task_events").fetchall()) == 2
        sub = conn.execute(
            "SELECT last_event_id, baseline_event_id FROM kanban_notify_subs"
        ).fetchone()
        assert sub["last_event_id"] == 2
        assert sub["baseline_event_id"] == 2


def test_unseen_events_for_sub_survives_migrated_db(tmp_path, monkeypatch):
    """The crash that motivated #35096 — ``int(None)`` on a NULL cursor — is
    gone after migration; the notifier query returns an integer cursor."""
    db_path = _setup_home(tmp_path, monkeypatch)
    _make_legacy_db(db_path)

    with kb.connect(db_path) as conn:
        cursor, events = kb.unseen_events_for_sub(
            conn, task_id="task-1", platform="telegram", chat_id="123"
        )
        assert isinstance(cursor, int)
        assert events == []


def test_current_schema_legacy_rows_are_baselined_once(tmp_path, monkeypatch):
    """Rows created before the marker migration skip old history exactly once.

    The no-event row proves marker value 0 is durable: an event appended after
    migration remains unseen after a second initialization instead of being
    swallowed by another baseline.
    """
    db_path = _setup_home(tmp_path, monkeypatch)
    with kb.connect(db_path) as conn:
        history_task = kb.create_task(conn, title="history")
        kb._append_event(conn, history_task, kind="completed")
        no_event_task = "task-no-events"
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) "
            "VALUES (?, 'no events', 'ready', 1000)",
            (no_event_task,),
        )
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?", (history_task,)
        ).fetchone()[0]
        conn.execute("ALTER TABLE kanban_notify_subs RENAME TO notify_current")
        conn.execute(
            """
            CREATE TABLE kanban_notify_subs (
                task_id TEXT NOT NULL, platform TEXT NOT NULL, chat_id TEXT NOT NULL,
                thread_id TEXT NOT NULL DEFAULT '', user_id TEXT,
                notifier_profile TEXT, created_at INTEGER NOT NULL,
                last_event_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (task_id, platform, chat_id, thread_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO kanban_notify_subs "
            "(task_id, platform, chat_id, created_at, last_event_id) "
            "VALUES (?, 'telegram', 'history-chat', 1000, 0), "
            "       (?, 'telegram', 'empty-chat', 1000, 0)",
            (history_task, no_event_task),
        )
        conn.execute("DROP TABLE notify_current")
        conn.execute(
            "CREATE INDEX idx_notify_task ON kanban_notify_subs(task_id)"
        )

    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path) as conn:
        rows = {
            row["task_id"]: row
            for row in conn.execute(
                "SELECT task_id, last_event_id, baseline_event_id "
                "FROM kanban_notify_subs"
            )
        }
        history_max = conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id=?", (history_task,)
        ).fetchone()[0]
        assert rows[history_task]["last_event_id"] == history_max
        assert rows[history_task]["baseline_event_id"] == history_max
        assert rows[no_event_task]["last_event_id"] == 0
        assert rows[no_event_task]["baseline_event_id"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?", (history_task,)
        ).fetchone()[0] == audit_count
        kb._append_event(conn, no_event_task, kind="crashed")

    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_event_id, baseline_event_id FROM kanban_notify_subs "
            "WHERE task_id=?",
            (no_event_task,),
        ).fetchone()
        assert row["last_event_id"] == 0
        assert row["baseline_event_id"] == 0
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=no_event_task,
            platform="telegram",
            chat_id="empty-chat",
            kinds=["crashed"],
        )
        assert [event.kind for event in events] == ["crashed"]


def test_legacy_nonzero_cursor_preserves_events_recorded_during_downtime(
    tmp_path, monkeypatch,
):
    """Migration marks an advanced row without swallowing newer events."""
    _setup_home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="advanced subscription")
        prior_cursor = conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO kanban_notify_subs "
            "(task_id, platform, chat_id, created_at, last_event_id) "
            "VALUES (?, 'telegram', 'advanced-chat', 1000, ?)",
            (task_id, prior_cursor),
        )
        kb._append_event(conn, task_id, kind="crashed")
        post_baseline_event = conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id=?", (task_id,)
        ).fetchone()[0]

        kb._baseline_legacy_notify_subs(conn)

        sub = conn.execute(
            "SELECT last_event_id, baseline_event_id FROM kanban_notify_subs "
            "WHERE task_id=?",
            (task_id,),
        ).fetchone()
        assert sub["last_event_id"] == prior_cursor
        assert sub["baseline_event_id"] == post_baseline_event
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="advanced-chat",
            kinds=["crashed"],
        )
        assert [event.id for event in events] == [post_baseline_event]
