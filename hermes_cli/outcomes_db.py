"""Root-shared Project Outcome and conversation coordination store.

Projects themselves remain profile-scoped in :mod:`hermes_cli.projects_db`.
Outcomes, conversation lanes, and mutation leases coordinate work *between*
profiles/boards, so they live at the root Hermes home instead of one profile.

This store is intentionally coordination-only:

* Git remains source authority for code and candidate identity.
* Runtime remains authority for what is actually deployed.
* Conversation lanes carry context/projection, never mutation authority.
* A mutation lease prevents competing executions from mutating overlapping
  repository/path scopes for the same installation.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional

from hermes_cli.sqlite_util import add_column_if_missing, write_txn
from hermes_constants import get_default_hermes_root


_OUTCOME_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LANE_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_GLOB_CHARS = frozenset("*?[")
DEFAULT_MUTATION_LEASE_TTL_SECONDS = 6 * 60 * 60
MIN_MUTATION_LEASE_TTL_SECONDS = 60
MAX_MUTATION_LEASE_TTL_SECONDS = 24 * 60 * 60

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outcomes (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL,
    outcome_key             TEXT NOT NULL,
    name                    TEXT NOT NULL,
    state                   TEXT NOT NULL DEFAULT 'planning',
    visible_owner           TEXT,
    current_base_ref        TEXT,
    current_candidate_ref   TEXT,
    current_live_ref        TEXT,
    frozen_acceptance_json  TEXT,
    next_action             TEXT,
    created_at              INTEGER NOT NULL,
    updated_at              INTEGER NOT NULL,
    archived                INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, outcome_key)
);

CREATE INDEX IF NOT EXISTS idx_outcomes_project
    ON outcomes(project_id, archived, updated_at);

CREATE TABLE IF NOT EXISTS conversation_lanes (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    outcome_id  TEXT REFERENCES outcomes(id) ON DELETE SET NULL,
    platform    TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    thread_id   TEXT NOT NULL DEFAULT '',
    label       TEXT,
    lane_kind   TEXT NOT NULL DEFAULT 'workstream',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    UNIQUE(platform, chat_id, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_conversation_lanes_project
    ON conversation_lanes(project_id, outcome_id, updated_at);

CREATE TABLE IF NOT EXISTS outcome_dependencies (
    id                     TEXT PRIMARY KEY,
    outcome_id             TEXT NOT NULL REFERENCES outcomes(id) ON DELETE CASCADE,
    depends_on_outcome_id  TEXT NOT NULL REFERENCES outcomes(id) ON DELETE CASCADE,
    dependency_kind        TEXT NOT NULL DEFAULT 'requires',
    created_at             INTEGER NOT NULL,
    UNIQUE(outcome_id, depends_on_outcome_id, dependency_kind),
    CHECK(outcome_id <> depends_on_outcome_id)
);

CREATE INDEX IF NOT EXISTS idx_outcome_dependencies_outcome
    ON outcome_dependencies(outcome_id, depends_on_outcome_id);
CREATE INDEX IF NOT EXISTS idx_outcome_dependencies_required
    ON outcome_dependencies(depends_on_outcome_id, outcome_id);

CREATE TABLE IF NOT EXISTS mutation_leases (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    outcome_id          TEXT REFERENCES outcomes(id) ON DELETE CASCADE,
    repository          TEXT NOT NULL,
    scope_json          TEXT NOT NULL,
    owner_execution_id  TEXT NOT NULL,
    base_ref            TEXT,
    acquired_at         INTEGER NOT NULL,
    expires_at          INTEGER,
    released_at         INTEGER,
    release_reason      TEXT
);

CREATE INDEX IF NOT EXISTS idx_mutation_leases_active_repo
    ON mutation_leases(repository, released_at, acquired_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mutation_leases_active_owner
    ON mutation_leases(owner_execution_id)
    WHERE released_at IS NULL;
"""


class OutcomeError(ValueError):
    """Base class for invalid outcome/coordination operations."""


class MutationLeaseConflict(OutcomeError):
    """Raised when another active execution owns an overlapping scope."""

    def __init__(self, *, requested: Mapping[str, Any], conflicting: Mapping[str, Any]):
        self.requested = dict(requested)
        self.conflicting = dict(conflicting)
        super().__init__(
            "mutation scope overlaps active execution "
            f"{self.conflicting.get('owner_execution_id')} "
            f"(lease {self.conflicting.get('id')})"
        )


def outcomes_db_path() -> Path:
    return get_default_hermes_root() / "outcomes.db"


def _now() -> int:
    return int(time.time())


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


def _text(value: Any, *, field: str, max_chars: int = 512) -> str:
    text = str(value or "").strip()
    if not text:
        raise OutcomeError(f"{field} is required")
    if len(text) > max_chars:
        raise OutcomeError(f"{field} exceeds {max_chars} characters")
    return text


def _optional_text(value: Any, *, max_chars: int = 4096) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_chars:
        raise OutcomeError(f"text exceeds {max_chars} characters")
    return text


def _normalize_outcome_key(value: Any) -> str:
    key = _text(value, field="outcome_key", max_chars=128)
    if not _OUTCOME_KEY_RE.fullmatch(key):
        raise OutcomeError(
            "outcome_key must use only letters, numbers, dot, underscore, or hyphen"
        )
    return key


def _normalize_lane_kind(value: Any) -> str:
    kind = str(value or "workstream").strip().lower()
    if not _LANE_KIND_RE.fullmatch(kind):
        raise OutcomeError("lane_kind must be lowercase alphanumeric/hyphen/underscore")
    return kind


def _normalize_repository(value: Any) -> str:
    repo = _text(value, field="repository", max_chars=1024)
    # Filesystem paths are valid when a repository has no remote. Prefer a
    # portable remote identity when callers have one. GitHub URL/scp forms are
    # collapsed to owner/repo so a project-root-derived remote and a roadmap
    # binding cannot accidentally acquire independent leases for the same repo.
    if repo.startswith(("/", "~")):
        return os.path.normcase(str(Path(repo).expanduser().resolve(strict=False)))
    github_patterns = (
        r"^git@github\.com:([^/]+/[^/]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/([^/]+/[^/]+?)(?:\.git)?/?$",
        r"^https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$",
    )
    for pattern in github_patterns:
        match = re.fullmatch(pattern, repo, flags=re.IGNORECASE)
        if match:
            return match.group(1).removesuffix(".git")
    return repo.rstrip("/").removesuffix(".git")


def _normalize_scope_entry(value: Any) -> str:
    raw = _text(value, field="path_scope entry", max_chars=1024).replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    raw = raw.strip("/")
    if not raw:
        raise OutcomeError("path_scope entry must not resolve to repository root")
    # Keep glob syntax, but reject traversal/control path components.
    plain = raw.replace("**", "x").replace("*", "x").replace("?", "x")
    if any(part in {"", ".", ".."} for part in PurePosixPath(plain).parts):
        raise OutcomeError(f"unsafe path_scope entry: {value!r}")
    return raw


def normalize_scope(scope: Iterable[Any]) -> list[str]:
    if isinstance(scope, (str, bytes)):
        raise OutcomeError("path_scope must be a list of repository-relative paths")
    result: list[str] = []
    seen: set[str] = set()
    for value in scope:
        item = _normalize_scope_entry(value)
        if item not in seen:
            seen.add(item)
            result.append(item)
    if not result:
        raise OutcomeError("path_scope must contain at least one path")
    return result


def _scope_anchor(pattern: str) -> tuple[str, bool]:
    """Return (static prefix, contains_glob) for conservative overlap checks."""
    parts = pattern.split("/")
    prefix: list[str] = []
    has_glob = False
    for part in parts:
        if any(ch in part for ch in _GLOB_CHARS):
            has_glob = True
            break
        prefix.append(part)
    return "/".join(prefix), has_glob


def _path_related(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def scopes_overlap(left: Iterable[Any], right: Iterable[Any]) -> bool:
    """Conservatively decide whether two repository-relative scope sets overlap.

    Exact paths are treated as subtree roots because task scopes commonly name a
    directory without a trailing ``/**``. For glob patterns we compare their
    static prefixes and additionally test direct fnmatch relations. Ambiguous
    patterns fail closed as overlapping rather than allowing a collision.
    """
    a_scope = normalize_scope(left)
    b_scope = normalize_scope(right)
    for a in a_scope:
        a_anchor, a_glob = _scope_anchor(a)
        for b in b_scope:
            b_anchor, b_glob = _scope_anchor(b)
            if fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a):
                return True
            if not a_anchor or not b_anchor:
                return True
            if _path_related(a_anchor, b_anchor):
                return True
            # Two patterns may diverge before their first wildcard and are then
            # safely independent (e.g. apps/salg/** vs apps/bemanning/**).
            if not a_glob and not b_glob and _path_related(a, b):
                return True
    return False


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path if db_path is not None else outcomes_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="outcomes.db")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA_SQL)
        lease_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(mutation_leases)")
        }
        if "expires_at" not in lease_columns:
            add_column_if_missing(
                conn, "mutation_leases", "expires_at", "expires_at INTEGER"
            )
    except Exception:
        conn.close()
        raise
    return conn


@contextlib.contextmanager
def connect_closing(db_path: Optional[Path] = None):
    conn = connect(db_path=db_path)
    try:
        yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class Outcome:
    id: str
    project_id: str
    outcome_key: str
    name: str
    state: str
    created_at: int
    updated_at: int
    visible_owner: Optional[str] = None
    current_base_ref: Optional[str] = None
    current_candidate_ref: Optional[str] = None
    current_live_ref: Optional[str] = None
    frozen_acceptance: Any = None
    next_action: Optional[str] = None
    archived: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "outcome_key": self.outcome_key,
            "name": self.name,
            "state": self.state,
            "visible_owner": self.visible_owner,
            "current_base_ref": self.current_base_ref,
            "current_candidate_ref": self.current_candidate_ref,
            "current_live_ref": self.current_live_ref,
            "frozen_acceptance": self.frozen_acceptance,
            "next_action": self.next_action,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived": self.archived,
        }


@dataclass(frozen=True)
class ConversationLane:
    id: str
    project_id: str
    platform: str
    chat_id: str
    thread_id: str
    lane_kind: str
    created_at: int
    updated_at: int
    outcome_id: Optional[str] = None
    label: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "outcome_id": self.outcome_id,
            "platform": self.platform,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id or None,
            "label": self.label,
            "lane_kind": self.lane_kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _decode_json(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _outcome_from_row(row: sqlite3.Row) -> Outcome:
    return Outcome(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        outcome_key=str(row["outcome_key"]),
        name=str(row["name"]),
        state=str(row["state"]),
        visible_owner=row["visible_owner"],
        current_base_ref=row["current_base_ref"],
        current_candidate_ref=row["current_candidate_ref"],
        current_live_ref=row["current_live_ref"],
        frozen_acceptance=_decode_json(row["frozen_acceptance_json"]),
        next_action=row["next_action"],
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        archived=bool(row["archived"]),
    )


def _lane_from_row(row: sqlite3.Row) -> ConversationLane:
    return ConversationLane(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        outcome_id=row["outcome_id"],
        platform=str(row["platform"]),
        chat_id=str(row["chat_id"]),
        thread_id=str(row["thread_id"] or ""),
        label=row["label"],
        lane_kind=str(row["lane_kind"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def get_outcome(conn: sqlite3.Connection, id_or_key: str, *, project_id: Optional[str] = None) -> Optional[Outcome]:
    token = str(id_or_key or "").strip()
    if not token:
        return None
    if project_id:
        row = conn.execute(
            "SELECT * FROM outcomes WHERE project_id=? AND (id=? OR outcome_key=?)",
            (str(project_id), token, token),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM outcomes WHERE id=? ORDER BY updated_at DESC LIMIT 1",
            (token,),
        ).fetchone()
        if row is None:
            rows = conn.execute(
                "SELECT * FROM outcomes WHERE outcome_key=? ORDER BY updated_at DESC LIMIT 2",
                (token,),
            ).fetchall()
            row = rows[0] if len(rows) == 1 else None
    return _outcome_from_row(row) if row is not None else None


def list_outcomes(conn: sqlite3.Connection, project_id: str, *, include_archived: bool = False) -> list[Outcome]:
    project_id = _text(project_id, field="project_id", max_chars=256)
    sql = "SELECT * FROM outcomes WHERE project_id=?"
    params: list[Any] = [project_id]
    if not include_archived:
        sql += " AND archived=0"
    sql += " ORDER BY updated_at DESC, created_at ASC"
    return [_outcome_from_row(row) for row in conn.execute(sql, params).fetchall()]


def create_outcome(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    outcome_key: str,
    name: Optional[str] = None,
    state: str = "planning",
    visible_owner: Optional[str] = None,
    current_base_ref: Optional[str] = None,
    frozen_acceptance: Any = None,
    next_action: Optional[str] = None,
) -> str:
    project_id = _text(project_id, field="project_id", max_chars=256)
    outcome_key = _normalize_outcome_key(outcome_key)
    display_name = _text(name or outcome_key, field="name", max_chars=512)
    state = _text(state, field="state", max_chars=64).lower()
    now = _now()
    oid = _new_id("o_")
    acceptance_json = (
        json.dumps(frozen_acceptance, ensure_ascii=False, sort_keys=True)
        if frozen_acceptance is not None
        else None
    )
    with write_txn(conn):
        existing = conn.execute(
            "SELECT id FROM outcomes WHERE project_id=? AND outcome_key=?",
            (project_id, outcome_key),
        ).fetchone()
        if existing is not None:
            return str(existing["id"])
        conn.execute(
            """INSERT INTO outcomes (
                   id, project_id, outcome_key, name, state, visible_owner,
                   current_base_ref, frozen_acceptance_json, next_action,
                   created_at, updated_at, archived
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                oid,
                project_id,
                outcome_key,
                display_name,
                state,
                _optional_text(visible_owner, max_chars=256),
                _optional_text(current_base_ref),
                acceptance_json,
                _optional_text(next_action, max_chars=8192),
                now,
                now,
            ),
        )
    return oid


def update_outcome(conn: sqlite3.Connection, outcome_id: str, **fields: Any) -> bool:
    allowed = {
        "name",
        "state",
        "visible_owner",
        "current_base_ref",
        "current_candidate_ref",
        "current_live_ref",
        "frozen_acceptance",
        "next_action",
        "archived",
    }
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise OutcomeError("unknown outcome field(s): " + ", ".join(unknown))
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key == "frozen_acceptance":
            sets.append("frozen_acceptance_json=?")
            params.append(
                json.dumps(value, ensure_ascii=False, sort_keys=True)
                if value is not None
                else None
            )
        elif key == "archived":
            sets.append("archived=?")
            params.append(1 if bool(value) else 0)
        elif key == "state":
            sets.append("state=?")
            params.append(_text(value, field="state", max_chars=64).lower())
        elif key == "name":
            sets.append("name=?")
            params.append(_text(value, field="name", max_chars=512))
        else:
            column = key
            sets.append(f"{column}=?")
            params.append(_optional_text(value, max_chars=8192))
    if not sets:
        return False
    sets.append("updated_at=?")
    params.append(_now())
    params.append(_text(outcome_id, field="outcome_id", max_chars=256))
    with write_txn(conn):
        cur = conn.execute(
            f"UPDATE outcomes SET {', '.join(sets)} WHERE id=?",
            params,
        )
    return cur.rowcount == 1


def bind_conversation_lane(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    outcome_id: Optional[str] = None,
    label: Optional[str] = None,
    lane_kind: str = "workstream",
) -> str:
    project_id = _text(project_id, field="project_id", max_chars=256)
    platform = _text(platform, field="platform", max_chars=64).lower()
    chat_id = _text(chat_id, field="chat_id", max_chars=512)
    thread = str(thread_id or "").strip()
    if len(thread) > 512:
        raise OutcomeError("thread_id exceeds 512 characters")
    lane_kind = _normalize_lane_kind(lane_kind)
    if outcome_id:
        outcome = get_outcome(conn, outcome_id)
        if outcome is None:
            raise OutcomeError(f"unknown outcome: {outcome_id}")
        if outcome.project_id != project_id:
            raise OutcomeError("conversation lane outcome belongs to a different project")
        outcome_id = outcome.id
    now = _now()
    with write_txn(conn):
        existing = conn.execute(
            "SELECT * FROM conversation_lanes WHERE platform=? AND chat_id=? AND thread_id=?",
            (platform, chat_id, thread),
        ).fetchone()
        if existing is not None:
            if str(existing["project_id"]) != project_id:
                raise OutcomeError("conversation coordinate is already bound to another project")
            conn.execute(
                """UPDATE conversation_lanes
                      SET outcome_id=?, label=?, lane_kind=?, updated_at=?
                    WHERE id=?""",
                (
                    outcome_id,
                    _optional_text(label, max_chars=512),
                    lane_kind,
                    now,
                    existing["id"],
                ),
            )
            return str(existing["id"])
        lane_id = _new_id("cl_")
        conn.execute(
            """INSERT INTO conversation_lanes (
                   id, project_id, outcome_id, platform, chat_id, thread_id,
                   label, lane_kind, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                lane_id,
                project_id,
                outcome_id,
                platform,
                chat_id,
                thread,
                _optional_text(label, max_chars=512),
                lane_kind,
                now,
                now,
            ),
        )
    return lane_id


def find_conversation_lane(
    conn: sqlite3.Connection,
    *,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> Optional[ConversationLane]:
    row = conn.execute(
        "SELECT * FROM conversation_lanes WHERE platform=? AND chat_id=? AND thread_id=?",
        (str(platform).strip().lower(), str(chat_id).strip(), str(thread_id or "").strip()),
    ).fetchone()
    return _lane_from_row(row) if row is not None else None


def list_conversation_lanes(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    outcome_id: Optional[str] = None,
) -> list[ConversationLane]:
    sql = "SELECT * FROM conversation_lanes WHERE project_id=?"
    params: list[Any] = [_text(project_id, field="project_id", max_chars=256)]
    if outcome_id is not None:
        sql += " AND outcome_id=?"
        params.append(str(outcome_id))
    sql += " ORDER BY lane_kind, created_at, id"
    return [_lane_from_row(row) for row in conn.execute(sql, params).fetchall()]


def add_outcome_dependency(
    conn: sqlite3.Connection,
    *,
    outcome_id: str,
    depends_on_outcome_id: str,
    dependency_kind: str = "requires",
) -> str:
    outcome = get_outcome(conn, outcome_id)
    required = get_outcome(conn, depends_on_outcome_id)
    if outcome is None or required is None:
        raise OutcomeError("both dependency Outcomes must exist")
    if outcome.id == required.id:
        raise OutcomeError("Outcome cannot depend on itself")
    kind = _normalize_lane_kind(dependency_kind)
    now = _now()
    dep_id = _new_id("od_")
    with write_txn(conn):
        existing = conn.execute(
            """SELECT id FROM outcome_dependencies
                 WHERE outcome_id=? AND depends_on_outcome_id=? AND dependency_kind=?""",
            (outcome.id, required.id, kind),
        ).fetchone()
        if existing is not None:
            return str(existing["id"])
        conn.execute(
            """INSERT INTO outcome_dependencies
               (id, outcome_id, depends_on_outcome_id, dependency_kind, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (dep_id, outcome.id, required.id, kind, now),
        )
    return dep_id


def list_outcome_dependencies(
    conn: sqlite3.Connection, *, outcome_id: Optional[str] = None, project_id: Optional[str] = None
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if outcome_id:
        clauses.append("(d.outcome_id=? OR d.depends_on_outcome_id=?)")
        params.extend([str(outcome_id), str(outcome_id)])
    if project_id:
        clauses.append("(o.project_id=? OR r.project_id=?)")
        params.extend([str(project_id), str(project_id)])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        """SELECT d.id, d.outcome_id, d.depends_on_outcome_id, d.dependency_kind, d.created_at,
                  o.project_id AS project_id, o.outcome_key AS outcome_key,
                  r.project_id AS depends_on_project_id, r.outcome_key AS depends_on_outcome_key
             FROM outcome_dependencies d
             JOIN outcomes o ON o.id=d.outcome_id
             JOIN outcomes r ON r.id=d.depends_on_outcome_id""" + where +
        " ORDER BY d.created_at, d.id",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _lease_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "outcome_id": row["outcome_id"],
        "repository": str(row["repository"]),
        "path_scope": _decode_json(row["scope_json"]) or [],
        "owner_execution_id": str(row["owner_execution_id"]),
        "base_ref": row["base_ref"],
        "acquired_at": int(row["acquired_at"]),
        "expires_at": int(row["expires_at"]) if "expires_at" in row.keys() and row["expires_at"] is not None else None,
        "released_at": int(row["released_at"]) if row["released_at"] is not None else None,
        "release_reason": row["release_reason"],
    }


def active_mutation_leases(
    conn: sqlite3.Connection,
    *,
    repository: Optional[str] = None,
    project_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    now = _now()
    clauses = ["released_at IS NULL", "(expires_at IS NULL OR expires_at>?)"]
    params: list[Any] = [now]
    if repository is not None:
        clauses.append("repository=?")
        params.append(_normalize_repository(repository))
    if project_id is not None:
        clauses.append("project_id=?")
        params.append(str(project_id).strip())
    rows = conn.execute(
        "SELECT * FROM mutation_leases WHERE " + " AND ".join(clauses) + " ORDER BY acquired_at, id",
        params,
    ).fetchall()
    return [_lease_from_row(row) for row in rows]


def acquire_mutation_lease(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    outcome_id: str,
    repository: str,
    path_scope: Iterable[Any],
    owner_execution_id: str,
    base_ref: Optional[str] = None,
    ttl_seconds: int = DEFAULT_MUTATION_LEASE_TTL_SECONDS,
) -> dict[str, Any]:
    project_id = _text(project_id, field="project_id", max_chars=256)
    outcome = get_outcome(conn, outcome_id)
    if outcome is None:
        raise OutcomeError(f"unknown outcome: {outcome_id}")
    if outcome.project_id != project_id:
        raise OutcomeError("mutation lease outcome belongs to a different project")
    repository = _normalize_repository(repository)
    scope = normalize_scope(path_scope)
    owner = _text(owner_execution_id, field="owner_execution_id", max_chars=512)
    base = _optional_text(base_ref, max_chars=4096)
    if isinstance(ttl_seconds, bool):
        raise OutcomeError("ttl_seconds must be an integer")
    try:
        ttl_seconds = int(ttl_seconds)
    except (TypeError, ValueError):
        raise OutcomeError("ttl_seconds must be an integer") from None
    if not MIN_MUTATION_LEASE_TTL_SECONDS <= ttl_seconds <= MAX_MUTATION_LEASE_TTL_SECONDS:
        raise OutcomeError(
            f"ttl_seconds must be between {MIN_MUTATION_LEASE_TTL_SECONDS} and "
            f"{MAX_MUTATION_LEASE_TTL_SECONDS}"
        )
    requested = {
        "project_id": project_id,
        "outcome_id": outcome.id,
        "repository": repository,
        "path_scope": scope,
        "owner_execution_id": owner,
        "base_ref": base,
    }
    now = _now()
    expires = now + ttl_seconds
    with write_txn(conn):
        # Expiry is a crash fence, not a normal release path. Mark expired rows
        # released before the partial unique-index checks so a dead execution
        # cannot permanently reserve an owner id or repository scope.
        conn.execute(
            """UPDATE mutation_leases
                  SET released_at=?, release_reason=COALESCE(release_reason, 'expired')
                WHERE released_at IS NULL AND expires_at IS NOT NULL AND expires_at<=?""",
            (now, now),
        )
        existing_owner = conn.execute(
            "SELECT * FROM mutation_leases WHERE owner_execution_id=? AND released_at IS NULL",
            (owner,),
        ).fetchone()
        if existing_owner is not None:
            existing = _lease_from_row(existing_owner)
            if (
                existing["project_id"] == project_id
                and existing["outcome_id"] == outcome.id
                and existing["repository"] == repository
                and existing["path_scope"] == scope
                and existing["base_ref"] == base
            ):
                return existing
            raise MutationLeaseConflict(requested=requested, conflicting=existing)

        rows = conn.execute(
            "SELECT * FROM mutation_leases WHERE repository=? AND released_at IS NULL",
            (repository,),
        ).fetchall()
        for row in rows:
            existing = _lease_from_row(row)
            if scopes_overlap(scope, existing["path_scope"]):
                raise MutationLeaseConflict(requested=requested, conflicting=existing)

        lease_id = _new_id("ml_")
        conn.execute(
            """INSERT INTO mutation_leases (
                   id, project_id, outcome_id, repository, scope_json,
                   owner_execution_id, base_ref, acquired_at, expires_at,
                   released_at, release_reason
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)""",
            (
                lease_id,
                project_id,
                outcome.id,
                repository,
                json.dumps(scope, ensure_ascii=False, separators=(",", ":")),
                owner,
                base,
                now,
                expires,
            ),
        )
        row = conn.execute("SELECT * FROM mutation_leases WHERE id=?", (lease_id,)).fetchone()
    return _lease_from_row(row)


def renew_mutation_lease(
    conn: sqlite3.Connection,
    *,
    owner_execution_id: str,
    ttl_seconds: int = DEFAULT_MUTATION_LEASE_TTL_SECONDS,
) -> bool:
    owner = _text(owner_execution_id, field="owner_execution_id", max_chars=512)
    if isinstance(ttl_seconds, bool):
        raise OutcomeError("ttl_seconds must be an integer")
    try:
        ttl_seconds = int(ttl_seconds)
    except (TypeError, ValueError):
        raise OutcomeError("ttl_seconds must be an integer") from None
    if not MIN_MUTATION_LEASE_TTL_SECONDS <= ttl_seconds <= MAX_MUTATION_LEASE_TTL_SECONDS:
        raise OutcomeError(
            f"ttl_seconds must be between {MIN_MUTATION_LEASE_TTL_SECONDS} and "
            f"{MAX_MUTATION_LEASE_TTL_SECONDS}"
        )
    now = _now()
    with write_txn(conn):
        cur = conn.execute(
            """UPDATE mutation_leases SET expires_at=?
                 WHERE owner_execution_id=? AND released_at IS NULL
                   AND (expires_at IS NULL OR expires_at>?)""",
            (now + ttl_seconds, owner, now),
        )
    return cur.rowcount == 1


def release_mutation_lease(
    conn: sqlite3.Connection,
    *,
    lease_id: Optional[str] = None,
    owner_execution_id: Optional[str] = None,
    reason: Optional[str] = None,
) -> bool:
    if bool(lease_id) == bool(owner_execution_id):
        raise OutcomeError("release requires exactly one of lease_id or owner_execution_id")
    column = "id" if lease_id else "owner_execution_id"
    value = str(lease_id or owner_execution_id).strip()
    now = _now()
    with write_txn(conn):
        cur = conn.execute(
            f"""UPDATE mutation_leases
                   SET released_at=?, release_reason=?
                 WHERE {column}=? AND released_at IS NULL""",
            (now, _optional_text(reason, max_chars=2048), value),
        )
    return cur.rowcount > 0


def project_snapshot(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    outcomes = list_outcomes(conn, project_id)
    lanes = list_conversation_lanes(conn, project_id)
    leases = active_mutation_leases(conn, project_id=project_id)
    dependencies = list_outcome_dependencies(conn, project_id=project_id)
    return {
        "project_id": str(project_id),
        "outcomes": [outcome.to_dict() for outcome in outcomes],
        "conversation_lanes": [lane.to_dict() for lane in lanes],
        "outcome_dependencies": dependencies,
        "active_mutation_leases": leases,
    }
