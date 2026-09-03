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
import hashlib
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
DEFAULT_GLOBAL_MUTATING_CAP = 3
DEFAULT_OWNER_MUTATING_CAP = 2
DEFAULT_RESOURCE_CAPACITIES = {"vectorworks-local": 1}
DEFAULT_RESOURCE_LEASE_TTL_SECONDS = 6 * 60 * 60

EXECUTION_MODES = frozenset({"direct_codex", "kanban", "external"})
EXECUTION_STATES = frozenset(
    {
        "queued", "waiting_resource", "running", "blocked", "needs_owner",
        "completed", "cancelled", "failed",
    }
)
TERMINAL_EXECUTION_STATES = frozenset({"completed", "cancelled", "failed"})
# Only admitted writers consume the canonical concurrency budget. Queued and
# resource-waiting requests have not acquired mutation ownership; blocked and
# needs-owner executions have released it and may be explicitly re-admitted.
ACTIVE_MUTATING_STATES = frozenset({"running"})
RESOURCE_LEASE_STATES = frozenset({"waiting", "acquired", "released", "cancelled"})

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

CREATE TABLE IF NOT EXISTS executions (
    execution_id          TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL,
    outcome_id            TEXT REFERENCES outcomes(id) ON DELETE CASCADE,
    execution_mode        TEXT NOT NULL,
    backend_id            TEXT,
    owner                 TEXT NOT NULL,
    mutating              INTEGER NOT NULL DEFAULT 1,
    state                 TEXT NOT NULL DEFAULT 'queued',
    conversation_lane_id  TEXT REFERENCES conversation_lanes(id) ON DELETE SET NULL,
    delivery_target       TEXT,
    repository            TEXT,
    mutation_scope_json   TEXT,
    base_ref              TEXT,
    resource_requirements_json TEXT,
    started_at            INTEGER,
    last_heartbeat_at     INTEGER,
    terminal_at           INTEGER,
    receipt_uri           TEXT,
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_executions_active
    ON executions(state, owner, updated_at);
CREATE INDEX IF NOT EXISTS idx_executions_project
    ON executions(project_id, outcome_id, updated_at);

CREATE TABLE IF NOT EXISTS resource_leases (
    id                  TEXT PRIMARY KEY,
    resource_key        TEXT NOT NULL,
    capacity            INTEGER NOT NULL,
    owner_execution_id  TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    outcome_id          TEXT REFERENCES outcomes(id) ON DELETE CASCADE,
    purpose             TEXT,
    state               TEXT NOT NULL DEFAULT 'waiting',
    requested_at        INTEGER NOT NULL,
    acquired_at         INTEGER,
    last_heartbeat_at   INTEGER,
    expires_at          INTEGER,
    released_at         INTEGER,
    release_reason      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_lease_owner_active
    ON resource_leases(resource_key, owner_execution_id)
    WHERE state IN ('waiting', 'acquired');
CREATE INDEX IF NOT EXISTS idx_resource_lease_fifo
    ON resource_leases(resource_key, state, requested_at, id);

CREATE TABLE IF NOT EXISTS visible_events (
    idempotency_key   TEXT PRIMARY KEY,
    execution_id      TEXT NOT NULL,
    event_kind        TEXT NOT NULL,
    candidate_revision TEXT NOT NULL,
    created_at        INTEGER NOT NULL
);
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


class ExecutionAdmissionBlocked(OutcomeError):
    """Raised when a root-wide mutation capacity policy rejects admission."""

    def __init__(self, reason: str, *, counts: Mapping[str, Any]):
        self.reason = reason
        self.counts = dict(counts)
        super().__init__(f"execution admission blocked: {reason}")


class ResourceUnavailable(OutcomeError):
    """Raised when a resource request is queued rather than acquired."""


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


def _normalize_execution_mode(value: Any) -> str:
    mode = _text(value, field="execution_mode", max_chars=64).lower()
    if mode not in EXECUTION_MODES:
        raise OutcomeError(
            "execution_mode must be one of: " + ", ".join(sorted(EXECUTION_MODES))
        )
    return mode


def _normalize_execution_state(value: Any) -> str:
    state = _text(value, field="state", max_chars=64).lower()
    if state not in EXECUTION_STATES:
        raise OutcomeError(
            "execution state must be one of: " + ", ".join(sorted(EXECUTION_STATES))
        )
    return state


def _normalize_resource_key(value: Any) -> str:
    key = _text(value, field="resource_key", max_chars=256).lower()
    if any(ch.isspace() for ch in key):
        raise OutcomeError("resource_key must not contain whitespace")
    return key


def normalize_resource_requirements(value: Any) -> list[str]:
    """Return a stable, duplicate-free list of generic resource keys."""
    if value is None:
        return []
    if isinstance(value, Mapping):
        raw_values = [key for key, required in value.items() if bool(required)]
    elif isinstance(value, (str, bytes)):
        raw_values = [value]
    else:
        try:
            raw_values = list(value)
        except TypeError:
            raise OutcomeError("resource_requirements must be a list or mapping") from None
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        key = _normalize_resource_key(raw)
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise OutcomeError(f"{field} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        raise OutcomeError(f"{field} must be a positive integer") from None
    if normalized < 1:
        raise OutcomeError(f"{field} must be a positive integer")
    return normalized


def _ttl_seconds(value: Any) -> int:
    ttl = _positive_int(value, field="ttl_seconds")
    if not MIN_MUTATION_LEASE_TTL_SECONDS <= ttl <= MAX_MUTATION_LEASE_TTL_SECONDS:
        raise OutcomeError(
            f"ttl_seconds must be between {MIN_MUTATION_LEASE_TTL_SECONDS} and "
            f"{MAX_MUTATION_LEASE_TTL_SECONDS}"
        )
    return ttl


def _optional_repository(value: Any) -> Optional[str]:
    return _normalize_repository(value) if _optional_text(value, max_chars=1024) else None


def _optional_scope(value: Any) -> list[str]:
    return [] if value is None else normalize_scope(value)


def _bool(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise OutcomeError(f"{field} must be a boolean")


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
        execution_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(executions)")
        }
        if "mutating" not in execution_columns:
            add_column_if_missing(
                conn, "executions", "mutating", "mutating INTEGER NOT NULL DEFAULT 1"
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


def _execution_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "execution_id": str(row["execution_id"]),
        "project_id": str(row["project_id"]),
        "outcome_id": row["outcome_id"],
        "execution_mode": str(row["execution_mode"]),
        "backend_id": row["backend_id"],
        "owner": str(row["owner"]),
        "mutating": bool(row["mutating"]),
        "state": str(row["state"]),
        "conversation_lane_id": row["conversation_lane_id"],
        "delivery_target": row["delivery_target"],
        "repository": row["repository"],
        "mutation_scope": _decode_json(row["mutation_scope_json"]) or [],
        "base_ref": row["base_ref"],
        "resource_requirements": (
            _decode_json(row["resource_requirements_json"]) or []
        ),
        "started_at": int(row["started_at"]) if row["started_at"] is not None else None,
        "last_heartbeat_at": (
            int(row["last_heartbeat_at"])
            if row["last_heartbeat_at"] is not None
            else None
        ),
        "terminal_at": int(row["terminal_at"]) if row["terminal_at"] is not None else None,
        "receipt_uri": row["receipt_uri"],
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
    }


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


def resolve_orchestration_mode(
    conn: sqlite3.Connection,
    *,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    force_portfolio: bool = False,
) -> dict[str, Any]:
    """Resolve one human conversation surface to portfolio or project mode.

    A first-class bound conversation lane selects project mode. Main-DM / 00
    Kontroll deployments may pass ``force_portfolio=True`` from their configured
    control surface without hard-coding deployment-specific chat ids here.
    """
    if not force_portfolio:
        lane = find_conversation_lane(
            conn,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        if lane is not None:
            return {
                "mode": "project",
                "project_id": lane.project_id,
                "outcome_id": lane.outcome_id,
                "conversation_lane_id": lane.id,
            }
    return {
        "mode": "portfolio",
        "project_id": None,
        "outcome_id": None,
        "conversation_lane_id": None,
    }


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


def _validate_execution_context(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    outcome_id: Optional[str],
    conversation_lane_id: Optional[str],
) -> tuple[str, Optional[str], Optional[ConversationLane]]:
    project = _text(project_id, field="project_id", max_chars=256)
    normalized_outcome: Optional[str] = None
    if outcome_id:
        outcome = get_outcome(conn, str(outcome_id), project_id=project)
        if outcome is None:
            raise OutcomeError(f"unknown outcome for project {project}: {outcome_id}")
        normalized_outcome = outcome.id

    lane: Optional[ConversationLane] = None
    if conversation_lane_id:
        row = conn.execute(
            "SELECT * FROM conversation_lanes WHERE id=?",
            (_text(conversation_lane_id, field="conversation_lane_id", max_chars=256),),
        ).fetchone()
        if row is None:
            raise OutcomeError(f"unknown conversation lane: {conversation_lane_id}")
        lane = _lane_from_row(row)
        if lane.project_id != project:
            raise OutcomeError("execution conversation lane belongs to a different project")
        if normalized_outcome and lane.outcome_id and lane.outcome_id != normalized_outcome:
            raise OutcomeError("execution conversation lane belongs to a different outcome")
    return project, normalized_outcome, lane


def conversation_lane_target(lane: ConversationLane) -> str:
    """Return the exact native delivery target for a bound conversation lane."""
    if lane.thread_id:
        return f"{lane.platform}:{lane.chat_id}:{lane.thread_id}"
    return f"{lane.platform}:{lane.chat_id}"


def get_execution(conn: sqlite3.Connection, execution_id: str) -> Optional[dict[str, Any]]:
    token = str(execution_id or "").strip()
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM executions WHERE execution_id=?", (token,)
    ).fetchone()
    return _execution_from_row(row) if row is not None else None


def list_executions(
    conn: sqlite3.Connection,
    *,
    project_id: Optional[str] = None,
    outcome_id: Optional[str] = None,
    owner: Optional[str] = None,
    states: Optional[Iterable[str]] = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if project_id is not None:
        clauses.append("project_id=?")
        params.append(str(project_id).strip())
    if outcome_id is not None:
        clauses.append("outcome_id=?")
        params.append(str(outcome_id).strip())
    if owner is not None:
        clauses.append("owner=?")
        params.append(str(owner).strip())
    if states is not None:
        normalized = [_normalize_execution_state(item) for item in states]
        if not normalized:
            return []
        clauses.append("state IN (" + ",".join("?" for _ in normalized) + ")")
        params.extend(normalized)
    elif active_only:
        clauses.append("state NOT IN ('completed','cancelled','failed')")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        "SELECT * FROM executions" + where + " ORDER BY created_at, execution_id",
        params,
    ).fetchall()
    return [_execution_from_row(row) for row in rows]


def create_execution(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    execution_mode: str,
    owner: str,
    outcome_id: Optional[str] = None,
    backend_id: Optional[str] = None,
    state: str = "queued",
    mutating: bool = True,
    conversation_lane_id: Optional[str] = None,
    delivery_target: Optional[str] = None,
    repository: Optional[str] = None,
    mutation_scope: Optional[Iterable[Any]] = None,
    base_ref: Optional[str] = None,
    resource_requirements: Any = None,
    receipt_uri: Optional[str] = None,
    execution_id: Optional[str] = None,
) -> str:
    project, normalized_outcome, lane = _validate_execution_context(
        conn,
        project_id=project_id,
        outcome_id=outcome_id,
        conversation_lane_id=conversation_lane_id,
    )
    mode = _normalize_execution_mode(execution_mode)
    normalized_state = _normalize_execution_state(state)
    normalized_owner = _text(owner, field="owner", max_chars=256)
    mutating_flag = _bool(mutating, field="mutating")
    normalized_repo = _optional_repository(repository)
    normalized_scope = _optional_scope(mutation_scope)
    if normalized_scope and not normalized_repo:
        raise OutcomeError("mutation_scope requires repository")
    if mutating_flag and normalized_repo and not normalized_scope:
        raise OutcomeError("mutating repository execution requires mutation_scope")
    if mutating_flag and mode == "direct_codex" and (
        normalized_outcome is None or not normalized_repo or not normalized_scope
    ):
        raise OutcomeError(
            "mutating direct_codex execution requires outcome_id, repository, and mutation_scope"
        )
    resources = normalize_resource_requirements(resource_requirements)
    target = _optional_text(delivery_target, max_chars=2048)
    if lane is not None:
        exact_target = conversation_lane_target(lane)
        if target is None:
            target = exact_target
        elif target != exact_target:
            raise OutcomeError("delivery_target does not match conversation lane")
    eid = _text(execution_id or _new_id("ex_"), field="execution_id", max_chars=256)
    now = _now()
    started_at = now if normalized_state == "running" else None
    heartbeat_at = now if normalized_state == "running" else None
    terminal_at = now if normalized_state in TERMINAL_EXECUTION_STATES else None
    with write_txn(conn):
        existing = conn.execute(
            "SELECT * FROM executions WHERE execution_id=?", (eid,)
        ).fetchone()
        if existing is not None:
            current = _execution_from_row(existing)
            invariant = {
                "project_id": project,
                "outcome_id": normalized_outcome,
                "execution_mode": mode,
                "owner": normalized_owner,
            }
            if all(current[key] == value for key, value in invariant.items()):
                return eid
            raise OutcomeError(f"execution_id already exists with different identity: {eid}")
        conn.execute(
            """INSERT INTO executions (
                   execution_id, project_id, outcome_id, execution_mode, backend_id,
                   owner, mutating, state, conversation_lane_id, delivery_target,
                   repository, mutation_scope_json, base_ref,
                   resource_requirements_json, started_at, last_heartbeat_at,
                   terminal_at, receipt_uri, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                eid,
                project,
                normalized_outcome,
                mode,
                _optional_text(backend_id, max_chars=512),
                normalized_owner,
                1 if mutating_flag else 0,
                normalized_state,
                lane.id if lane else None,
                target,
                normalized_repo,
                json.dumps(normalized_scope, ensure_ascii=False, separators=(",", ":"))
                if normalized_scope
                else None,
                _optional_text(base_ref, max_chars=4096),
                json.dumps(resources, ensure_ascii=False, separators=(",", ":")),
                started_at,
                heartbeat_at,
                terminal_at,
                _optional_text(receipt_uri, max_chars=4096),
                now,
                now,
            ),
        )
    return eid


def update_execution(conn: sqlite3.Connection, execution_id: str, **fields: Any) -> bool:
    existing = get_execution(conn, execution_id)
    if existing is None:
        return False
    allowed = {
        "backend_id", "state", "owner", "mutating", "conversation_lane_id",
        "delivery_target", "repository", "mutation_scope", "base_ref",
        "resource_requirements", "receipt_uri",
    }
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise OutcomeError("unknown execution field(s): " + ", ".join(unknown))

    next_project = existing["project_id"]
    next_outcome = existing["outcome_id"]
    next_lane_id = fields.get("conversation_lane_id", existing["conversation_lane_id"])
    _, _, lane = _validate_execution_context(
        conn,
        project_id=next_project,
        outcome_id=next_outcome,
        conversation_lane_id=next_lane_id,
    )
    sets: list[str] = []
    params: list[Any] = []
    target_value = fields.get("delivery_target", existing["delivery_target"])
    if lane is not None:
        exact_target = conversation_lane_target(lane)
        if target_value is None:
            target_value = exact_target
        elif str(target_value).strip() != exact_target:
            raise OutcomeError("delivery_target does not match conversation lane")

    for key, value in fields.items():
        if key == "state":
            state_value = _normalize_execution_state(value)
            sets.append("state=?")
            params.append(state_value)
            if state_value == "running" and existing["started_at"] is None:
                sets.extend(["started_at=?", "last_heartbeat_at=?"])
                params.extend([_now(), _now()])
            if state_value in TERMINAL_EXECUTION_STATES:
                sets.append("terminal_at=?")
                params.append(_now())
        elif key == "mutating":
            sets.append("mutating=?")
            params.append(1 if _bool(value, field="mutating") else 0)
        elif key == "repository":
            sets.append("repository=?")
            params.append(_optional_repository(value))
        elif key == "mutation_scope":
            scope = _optional_scope(value)
            sets.append("mutation_scope_json=?")
            params.append(
                json.dumps(scope, ensure_ascii=False, separators=(",", ":")) if scope else None
            )
        elif key == "resource_requirements":
            resources = normalize_resource_requirements(value)
            sets.append("resource_requirements_json=?")
            params.append(json.dumps(resources, ensure_ascii=False, separators=(",", ":")))
        elif key == "conversation_lane_id":
            sets.append("conversation_lane_id=?")
            params.append(lane.id if lane else None)
        elif key == "delivery_target":
            sets.append("delivery_target=?")
            params.append(_optional_text(target_value, max_chars=2048))
        elif key == "owner":
            sets.append("owner=?")
            params.append(_text(value, field="owner", max_chars=256))
        else:
            sets.append(f"{key}=?")
            params.append(_optional_text(value, max_chars=4096))

    if "conversation_lane_id" in fields and "delivery_target" not in fields:
        sets.append("delivery_target=?")
        params.append(conversation_lane_target(lane) if lane else None)
    if not sets:
        return False
    sets.append("updated_at=?")
    params.append(_now())
    params.append(existing["execution_id"])
    with write_txn(conn):
        cur = conn.execute(
            f"UPDATE executions SET {', '.join(sets)} WHERE execution_id=?", params
        )
    return cur.rowcount == 1


def heartbeat_execution(conn: sqlite3.Connection, execution_id: str) -> bool:
    eid = _text(execution_id, field="execution_id", max_chars=256)
    now = _now()
    with write_txn(conn):
        cur = conn.execute(
            """UPDATE executions
                  SET last_heartbeat_at=?, updated_at=?
                WHERE execution_id=?
                  AND state NOT IN ('completed','cancelled','failed')""",
            (now, now, eid),
        )
        if cur.rowcount:
            conn.execute(
                """UPDATE resource_leases
                      SET last_heartbeat_at=?, expires_at=?
                    WHERE owner_execution_id=? AND state='acquired'""",
                (now, now + DEFAULT_RESOURCE_LEASE_TTL_SECONDS, eid),
            )
    if cur.rowcount:
        try:
            renew_mutation_lease(conn, owner_execution_id=eid)
        except OutcomeError:
            pass
    return cur.rowcount == 1


def cross_project_orchestration_enabled() -> bool:
    """Return the explicit rollout gate for Cross-Project Orchestration V1."""
    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config()
        kanban = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        return bool(
            isinstance(kanban, dict)
            and kanban.get("cross_project_orchestration_v1_enabled") is True
        )
    except Exception:
        return False


def configured_execution_caps() -> tuple[int, int]:
    """Return cross-backend mutation caps from the existing Kanban authority.

    Hove West already configures ``kanban.max_in_progress`` and
    ``max_in_progress_per_profile``. Cross-project execution admission consumes
    those same values rather than introducing a second independent capacity
    configuration. Stable V1 fallbacks are 3 global / 2 per owner.
    """
    global_cap = DEFAULT_GLOBAL_MUTATING_CAP
    owner_cap = DEFAULT_OWNER_MUTATING_CAP
    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config()
        kanban = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if isinstance(kanban, dict):
            raw_global = kanban.get("max_in_progress")
            raw_owner = kanban.get("max_in_progress_per_profile")
            if type(raw_global) is int and raw_global > 0:
                global_cap = raw_global
            if type(raw_owner) is int and raw_owner > 0:
                owner_cap = raw_owner
    except Exception:
        pass
    return global_cap, owner_cap


def execution_admission_status(
    conn: sqlite3.Connection,
    *,
    owner: str,
    global_cap: Optional[int] = None,
    owner_cap: Optional[int] = None,
    exclude_execution_id: Optional[str] = None,
) -> dict[str, Any]:
    normalized_owner = _text(owner, field="owner", max_chars=256)
    configured_global, configured_owner = configured_execution_caps()
    global_cap = _positive_int(
        configured_global if global_cap is None else global_cap, field="global_cap"
    )
    owner_cap = _positive_int(
        configured_owner if owner_cap is None else owner_cap, field="owner_cap"
    )
    clauses = ["mutating=1", "state='running'"]
    params: list[Any] = []
    if exclude_execution_id:
        clauses.append("execution_id<>?")
        params.append(str(exclude_execution_id).strip())
    where = " AND ".join(clauses)
    global_running = int(
        conn.execute(f"SELECT COUNT(*) FROM executions WHERE {where}", params).fetchone()[0]
    )
    owner_running = int(
        conn.execute(
            f"SELECT COUNT(*) FROM executions WHERE {where} AND owner=?",
            [*params, normalized_owner],
        ).fetchone()[0]
    )
    reason: Optional[str] = None
    if global_running >= global_cap:
        reason = "global_mutating_cap"
    elif owner_running >= owner_cap:
        reason = "owner_mutating_cap"
    return {
        "allowed": reason is None,
        "reason": reason,
        "global_running": global_running,
        "global_cap": global_cap,
        "owner_running": owner_running,
        "owner_cap": owner_cap,
    }


def admit_execution(
    conn: sqlite3.Connection,
    execution_id: str,
    *,
    global_cap: Optional[int] = None,
    owner_cap: Optional[int] = None,
    mutation_ttl_seconds: int = DEFAULT_MUTATION_LEASE_TTL_SECONDS,
    require_feature_gate: bool = False,
) -> dict[str, Any]:
    if require_feature_gate and not cross_project_orchestration_enabled():
        raise ExecutionAdmissionBlocked(
            "feature_gate_disabled",
            counts={"feature_gate": "kanban.cross_project_orchestration_v1_enabled"},
        )

    blocked: Optional[tuple[str, dict[str, Any]]] = None
    admitted: Optional[dict[str, Any]] = None
    with write_txn(conn):
        execution = get_execution(conn, execution_id)
        if execution is None:
            raise OutcomeError(f"unknown execution: {execution_id}")
        if execution["state"] == "running":
            admitted = execution
        elif execution["state"] in TERMINAL_EXECUTION_STATES:
            raise OutcomeError("terminal execution cannot be re-admitted")
        else:
            # BEGIN IMMEDIATE makes capacity read + resource/mutation acquisition
            # + running transition one root-store admission decision. Two
            # concurrent backends cannot both consume the final slot.
            if execution["mutating"]:
                counts = execution_admission_status(
                    conn,
                    owner=execution["owner"],
                    global_cap=global_cap,
                    owner_cap=owner_cap,
                    exclude_execution_id=execution["execution_id"],
                )
                if not counts["allowed"]:
                    blocked = (str(counts["reason"]), counts)

            requirements = list(execution.get("resource_requirements") or [])
            if len(requirements) > 1:
                raise OutcomeError(
                    "Cross-Project Orchestration V1 supports at most one shared resource per execution"
                )
            if blocked is None and requirements:
                requirement = requirements[0]
                if isinstance(requirement, Mapping):
                    resource_key = requirement.get("resource_key")
                    capacity = requirement.get("capacity")
                else:
                    resource_key = requirement
                    capacity = None
                lease = _request_resource_lease_in_transaction(
                    conn,
                    resource_key=resource_key,
                    owner_execution_id=execution["execution_id"],
                    capacity=capacity,
                )
                if lease["state"] != "acquired":
                    blocked = (
                        "waiting_resource",
                        {
                            "execution_id": execution["execution_id"],
                            "resources": [lease["resource_key"]],
                        },
                    )

            if blocked is None:
                if execution["mutating"] and execution["repository"]:
                    if not execution["outcome_id"] or not execution["mutation_scope"]:
                        raise OutcomeError(
                            "mutating repository execution requires outcome_id and mutation_scope"
                        )
                    _acquire_mutation_lease_in_transaction(
                        conn,
                        project_id=execution["project_id"],
                        outcome_id=execution["outcome_id"],
                        repository=execution["repository"],
                        path_scope=execution["mutation_scope"],
                        owner_execution_id=execution["execution_id"],
                        base_ref=execution["base_ref"],
                        ttl_seconds=mutation_ttl_seconds,
                    )
                now = _now()
                conn.execute(
                    """UPDATE executions
                          SET state='running', started_at=COALESCE(started_at, ?),
                              last_heartbeat_at=?, terminal_at=NULL, updated_at=?
                        WHERE execution_id=?""",
                    (now, now, now, execution["execution_id"]),
                )
                admitted = get_execution(conn, execution["execution_id"])

    if blocked is not None:
        reason, counts = blocked
        raise ExecutionAdmissionBlocked(reason, counts=counts)
    if admitted is None:
        raise OutcomeError("execution admission produced no terminal decision")
    return admitted


def _resource_lease_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "resource_key": str(row["resource_key"]),
        "capacity": int(row["capacity"]),
        "owner_execution_id": str(row["owner_execution_id"]),
        "project_id": str(row["project_id"]),
        "outcome_id": row["outcome_id"],
        "purpose": row["purpose"],
        "state": str(row["state"]),
        "requested_at": int(row["requested_at"]),
        "acquired_at": int(row["acquired_at"]) if row["acquired_at"] is not None else None,
        "last_heartbeat_at": (
            int(row["last_heartbeat_at"]) if row["last_heartbeat_at"] is not None else None
        ),
        "expires_at": int(row["expires_at"]) if row["expires_at"] is not None else None,
        "released_at": int(row["released_at"]) if row["released_at"] is not None else None,
        "release_reason": row["release_reason"],
    }


def list_resource_leases(
    conn: sqlite3.Connection,
    *,
    resource_key: Optional[str] = None,
    project_id: Optional[str] = None,
    owner_execution_id: Optional[str] = None,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if resource_key:
        clauses.append("resource_key=?")
        params.append(_normalize_resource_key(resource_key))
    if project_id:
        clauses.append("project_id=?")
        params.append(str(project_id).strip())
    if owner_execution_id:
        clauses.append("owner_execution_id=?")
        params.append(str(owner_execution_id).strip())
    if active_only:
        clauses.append("state IN ('waiting','acquired')")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        "SELECT * FROM resource_leases" + where + " ORDER BY requested_at, rowid", params
    ).fetchall()
    return [_resource_lease_from_row(row) for row in rows]


def _promote_resource_waiters_in_txn(
    conn: sqlite3.Connection, resource_key: str, *, now: Optional[int] = None
) -> list[str]:
    key = _normalize_resource_key(resource_key)
    current_time = _now() if now is None else int(now)
    active_rows = conn.execute(
        "SELECT * FROM resource_leases WHERE resource_key=? AND state='acquired' ORDER BY acquired_at, id",
        (key,),
    ).fetchall()
    waiting_rows = conn.execute(
        "SELECT * FROM resource_leases WHERE resource_key=? AND state='waiting' ORDER BY requested_at, rowid",
        (key,),
    ).fetchall()
    if not waiting_rows:
        return []
    configured_capacity = DEFAULT_RESOURCE_CAPACITIES.get(key)
    declared_capacity = (
        configured_capacity
        if configured_capacity is not None
        else max([int(row["capacity"]) for row in active_rows + waiting_rows] or [1])
    )
    available = max(0, declared_capacity - len(active_rows))
    promoted: list[str] = []
    for row in waiting_rows[:available]:
        conn.execute(
            """UPDATE resource_leases
                  SET state='acquired', acquired_at=?, last_heartbeat_at=?, expires_at=?
                WHERE id=? AND state='waiting'""",
            (
                current_time,
                current_time,
                current_time + DEFAULT_RESOURCE_LEASE_TTL_SECONDS,
                row["id"],
            ),
        )
        promoted.append(str(row["id"]))
        conn.execute(
            """UPDATE executions
                  SET state=CASE WHEN state='waiting_resource' THEN 'queued' ELSE state END,
                      updated_at=?
                WHERE execution_id=?""",
            (current_time, row["owner_execution_id"]),
        )
    return promoted


def _resolve_resource_capacity(resource_key: str, capacity: Optional[int]) -> int:
    configured = DEFAULT_RESOURCE_CAPACITIES.get(resource_key)
    if configured is not None:
        if capacity is not None and _positive_int(capacity, field="capacity") != configured:
            raise OutcomeError(
                f"resource {resource_key} capacity is fixed at {configured}"
            )
        return configured
    return _positive_int(capacity if capacity is not None else 1, field="capacity")


def _request_resource_lease_in_transaction(
    conn: sqlite3.Connection,
    *,
    resource_key: str,
    owner_execution_id: str,
    purpose: Optional[str] = None,
    capacity: Optional[int] = None,
) -> dict[str, Any]:
    key = _normalize_resource_key(resource_key)
    execution = get_execution(conn, owner_execution_id)
    if execution is None:
        raise OutcomeError(f"unknown execution for resource lease: {owner_execution_id}")
    resolved_capacity = _resolve_resource_capacity(key, capacity)
    now = _now()
    existing = conn.execute(
        """SELECT * FROM resource_leases
             WHERE resource_key=? AND owner_execution_id=?
               AND state IN ('waiting','acquired')""",
        (key, execution["execution_id"]),
    ).fetchone()
    if existing is None:
        lease_id = _new_id("rl_")
        conn.execute(
            """INSERT INTO resource_leases (
                   id, resource_key, capacity, owner_execution_id, project_id,
                   outcome_id, purpose, state, requested_at, acquired_at,
                   last_heartbeat_at, expires_at, released_at, release_reason
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', ?, NULL, NULL, NULL, NULL, NULL)""",
            (
                lease_id,
                key,
                resolved_capacity,
                execution["execution_id"],
                execution["project_id"],
                execution["outcome_id"],
                _optional_text(purpose, max_chars=2048),
                now,
            ),
        )
    else:
        lease_id = str(existing["id"])
        if int(existing["capacity"]) != resolved_capacity:
            raise OutcomeError("resource capacity conflicts with existing request")
    _promote_resource_waiters_in_txn(conn, key, now=now)
    row = conn.execute("SELECT * FROM resource_leases WHERE id=?", (lease_id,)).fetchone()
    lease = _resource_lease_from_row(row)
    if lease["state"] == "waiting":
        conn.execute(
            """UPDATE executions SET state='waiting_resource', updated_at=?
                 WHERE execution_id=? AND state NOT IN ('completed','cancelled','failed')""",
            (now, execution["execution_id"]),
        )
    return lease


def request_resource_lease(
    conn: sqlite3.Connection,
    *,
    resource_key: str,
    owner_execution_id: str,
    purpose: Optional[str] = None,
    capacity: Optional[int] = None,
) -> dict[str, Any]:
    with write_txn(conn):
        return _request_resource_lease_in_transaction(
            conn,
            resource_key=resource_key,
            owner_execution_id=owner_execution_id,
            purpose=purpose,
            capacity=capacity,
        )


def renew_resource_lease(
    conn: sqlite3.Connection,
    *,
    lease_id: Optional[str] = None,
    owner_execution_id: Optional[str] = None,
    ttl_seconds: int = DEFAULT_RESOURCE_LEASE_TTL_SECONDS,
) -> bool:
    if bool(lease_id) == bool(owner_execution_id):
        raise OutcomeError("renew requires exactly one of lease_id or owner_execution_id")
    ttl = _ttl_seconds(ttl_seconds)
    column = "id" if lease_id else "owner_execution_id"
    value = str(lease_id or owner_execution_id).strip()
    now = _now()
    with write_txn(conn):
        cur = conn.execute(
            f"""UPDATE resource_leases
                    SET last_heartbeat_at=?, expires_at=?
                  WHERE {column}=? AND state='acquired'""",
            (now, now + ttl, value),
        )
    return cur.rowcount > 0


def release_resource_lease(
    conn: sqlite3.Connection,
    *,
    lease_id: Optional[str] = None,
    owner_execution_id: Optional[str] = None,
    reason: Optional[str] = None,
    stale: bool = False,
    verified_dead: bool = False,
) -> dict[str, Any]:
    if bool(lease_id) == bool(owner_execution_id):
        raise OutcomeError("release requires exactly one of lease_id or owner_execution_id")
    if stale and not verified_dead:
        raise OutcomeError("stale resource release requires verified_dead=true")
    column = "id" if lease_id else "owner_execution_id"
    value = str(lease_id or owner_execution_id).strip()
    now = _now()
    with write_txn(conn):
        rows = conn.execute(
            f"SELECT * FROM resource_leases WHERE {column}=? AND state IN ('waiting','acquired')",
            (value,),
        ).fetchall()
        if not rows:
            return {"released": 0, "promoted": []}
        keys = sorted({str(row["resource_key"]) for row in rows})
        cur = conn.execute(
            f"""UPDATE resource_leases
                    SET state='released', released_at=?, release_reason=?
                  WHERE {column}=? AND state IN ('waiting','acquired')""",
            (now, _optional_text(reason, max_chars=2048), value),
        )
        promoted: list[str] = []
        for key in keys:
            promoted.extend(_promote_resource_waiters_in_txn(conn, key, now=now))
    return {"released": cur.rowcount, "promoted": promoted}


def next_resource_waiter(conn: sqlite3.Connection, resource_key: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """SELECT * FROM resource_leases
             WHERE resource_key=? AND state='waiting'
             ORDER BY requested_at, rowid LIMIT 1""",
        (_normalize_resource_key(resource_key),),
    ).fetchone()
    return _resource_lease_from_row(row) if row is not None else None


def terminalize_execution(
    conn: sqlite3.Connection,
    execution_id: str,
    *,
    state: str,
    receipt_uri: Optional[str] = None,
    release_leases: bool = True,
    reason: Optional[str] = None,
) -> bool:
    eid = _text(execution_id, field="execution_id", max_chars=256)
    terminal_state = _normalize_execution_state(state)
    if terminal_state not in TERMINAL_EXECUTION_STATES:
        raise OutcomeError("terminalize_execution requires a terminal execution state")
    now = _now()
    with write_txn(conn):
        cur = conn.execute(
            """UPDATE executions
                  SET state=?, terminal_at=?, updated_at=?, receipt_uri=COALESCE(?, receipt_uri)
                WHERE execution_id=?""",
            (
                terminal_state,
                now,
                now,
                _optional_text(receipt_uri, max_chars=4096),
                eid,
            ),
        )
        if cur.rowcount and release_leases:
            conn.execute(
                """UPDATE mutation_leases
                      SET released_at=?, release_reason=COALESCE(?, release_reason, ?)
                    WHERE owner_execution_id=? AND released_at IS NULL""",
                (now, _optional_text(reason, max_chars=2048), terminal_state, eid),
            )
            resource_rows = conn.execute(
                """SELECT DISTINCT resource_key FROM resource_leases
                     WHERE owner_execution_id=? AND state IN ('waiting','acquired')""",
                (eid,),
            ).fetchall()
            conn.execute(
                """UPDATE resource_leases
                      SET state='released', released_at=?,
                          release_reason=COALESCE(?, release_reason, ?)
                    WHERE owner_execution_id=? AND state IN ('waiting','acquired')""",
                (now, _optional_text(reason, max_chars=2048), terminal_state, eid),
            )
            for row in resource_rows:
                _promote_resource_waiters_in_txn(conn, str(row["resource_key"]), now=now)
    return cur.rowcount == 1


def visible_event_idempotency_key(
    execution_id: str, event_kind: str, candidate_revision: Optional[str] = None
) -> str:
    material = "\0".join(
        [
            _text(execution_id, field="execution_id", max_chars=256),
            _text(event_kind, field="event_kind", max_chars=128).lower(),
            str(candidate_revision or "").strip(),
        ]
    )
    return "ve_" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def record_visible_event(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    event_kind: str,
    candidate_revision: Optional[str] = None,
) -> tuple[str, bool]:
    eid = _text(execution_id, field="execution_id", max_chars=256)
    if get_execution(conn, eid) is None:
        raise OutcomeError(f"unknown execution: {eid}")
    kind = _text(event_kind, field="event_kind", max_chars=128).lower()
    revision = str(candidate_revision or "").strip()
    key = visible_event_idempotency_key(eid, kind, revision)
    with write_txn(conn):
        cur = conn.execute(
            """INSERT OR IGNORE INTO visible_events
               (idempotency_key, execution_id, event_kind, candidate_revision, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (key, eid, kind, revision, _now()),
        )
    return key, cur.rowcount == 1


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
    with write_txn(conn):
        return _acquire_mutation_lease_in_transaction(
            conn,
            project_id=project_id,
            outcome_id=outcome_id,
            repository=repository,
            path_scope=path_scope,
            owner_execution_id=owner_execution_id,
            base_ref=base_ref,
            ttl_seconds=ttl_seconds,
        )


def _acquire_mutation_lease_in_transaction(
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
    ttl_seconds = _ttl_seconds(ttl_seconds)
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
    # Mutation leases retain their historical crash-fence behavior. Generic
    # resources below intentionally do not infer owner death from TTL alone.
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
    executions = list_executions(conn, project_id=project_id, active_only=True)
    resources = list_resource_leases(conn, project_id=project_id, active_only=True)
    dependencies = list_outcome_dependencies(conn, project_id=project_id)
    return {
        "project_id": str(project_id),
        "outcomes": [outcome.to_dict() for outcome in outcomes],
        "conversation_lanes": [lane.to_dict() for lane in lanes],
        "outcome_dependencies": dependencies,
        "active_executions": executions,
        "active_mutation_leases": leases,
        "active_resource_leases": resources,
    }
