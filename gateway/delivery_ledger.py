"""Durable delivery-obligation ledger for gateway final responses.

A final agent response that was generated but not yet confirmed-delivered
to the messaging platform is the one artifact the gateway can lose without
a trace: the turn already burned its tokens, the text exists only in a
Python local, and a crash / planned restart between finalize and platform
ACK drops it silently (#58818, #41696, #63695).

This module records a small durable row per outbound final response in the
shared ``state.db`` (same file and conventions as
``tools.async_delegation`` — WAL, owner pid + process-start-time liveness,
bounded retention). The gateway writes three checkpoints around the send:

    record_obligation()   state='pending'     before any send attempt
    mark_attempting()     state='attempting'  immediately before the await
    mark_delivered() /    state='delivered'   only on SendResult.success
    mark_failed()         state='failed'      on a definitive rejection

On startup, ``sweep_recoverable()`` claims rows whose owning process is
dead and hands them to the gateway for redelivery. After a platform adapter
reconnects without a process restart, ``sweep_failed_for_runtime()`` may claim
only the same live process's explicitly allowlisted transient failures. Crash
semantics are explicit about ambiguity (the contract review of the earlier
delivery-outbox attempt, #61790, closed it for silently resending ambiguous
sends):

- ``pending``     — the send never started: redeliver plainly, no dup risk.
- ``attempting``  — crashed mid-await: the platform MAY already have the
  message. Redelivered WITH a visible recovered-reply marker so the
  contract is honest at-least-once, never a silent duplicate.
- ``failed``      — definitively rejected once; the restart is a natural
  retry boundary. Also carries the marker.
- ``delivered``   — nothing to do; retention prunes.

Poison rows cannot spin: attempts are capped, stale rows expire, and both
transition to ``abandoned`` (kept briefly for inspection, then pruned).

Everything here is best-effort by design: ledger failures must never block
or delay an actual send. Callers wrap every call in try/except.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DB_LOCK = threading.Lock()

# Redelivery policy knobs (module constants; deliberately not config — the
# ledger itself is gated by ``gateway.delivery_ledger`` and these bounds
# only matter in the rare recovery path).
MAX_ATTEMPTS = 3
STALE_AFTER_SECONDS = 24 * 60 * 60
_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_ROWS = 500
_MAX_DEFER_JITTER_SECONDS = 1.0
_ERROR_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Visible prefix for redeliveries that might duplicate an already-received
# message (crash mid-send / post-rejection retry). Honest at-least-once.
RECOVERED_MARKER = (
    "♻️ Recovered reply — the gateway restarted during delivery, "
    "so this may be a duplicate:\n\n"
)

# Runtime recovery uses a distinct marker because no gateway restart occurred.
# Keep the ambiguity explicit: a network rejection normally means the platform
# did not accept the message, but an acknowledgement can be lost independently.
RECONNECTED_MARKER = (
    "♻️ Recovered reply — the messaging platform reconnected after the original "
    "delivery failed, so this may be a duplicate:\n\n"
)

# Runtime replay is deliberately fail-closed. Only errors whose send contract
# proves they are transient reconnect failures belong here; permanent rejects
# (blocked bot, bad auth, missing chat) must not be retried merely because an
# adapter reconnected.
_RUNTIME_RETRYABLE_ERRORS = frozenset({"send_path_degraded"})


def _sanitize_error_kind(value: Any, fallback: str) -> str:
    """Keep durable receipts machine-categorical and free of raw error text."""
    candidate = str(value or "")
    return candidate if _ERROR_KIND_RE.fullmatch(candidate) else fallback


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        _initialize_schema(conn)
    except Exception:
        # A PRAGMA/DDL failure after a successful connect() must not leak the
        # just-opened connection back to the caller.
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="state.db (delivery_ledger)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_obligations (
            obligation_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT,
            content TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            last_error TEXT,
            adapter_profile TEXT,
            retry_not_before REAL
        )"""
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(delivery_obligations)")
    }
    if "adapter_profile" not in columns:
        try:
            conn.execute(
                "ALTER TABLE delivery_obligations ADD COLUMN adapter_profile TEXT"
            )
        except sqlite3.OperationalError as exc:
            # Concurrent first-use connections can both observe the old schema.
            if "duplicate column" not in str(exc).lower():
                raise
    if "retry_not_before" not in columns:
        try:
            conn.execute(
                "ALTER TABLE delivery_obligations ADD COLUMN retry_not_before REAL"
            )
        except sqlite3.OperationalError as exc:
            # Concurrent first-use connections can both observe the old schema.
            if "duplicate column" not in str(exc).lower():
                raise


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back the
    transaction; they do not close the connection. Using ``with _connect()``
    alone therefore leaks a connection — and its WAL/SHM file descriptors — on
    every call, deferring the close to the garbage collector. On a long-running
    gateway that exhausts ``RLIMIT_NOFILE`` (the cron-ledger sibling of this
    bug was #69567 / PR #69594). ``record_obligation`` runs on every outbound
    final response, so this ledger is the highest-frequency leaker.
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _owner_stamp() -> tuple[int, Optional[int]]:
    pid = os.getpid()
    try:
        from gateway.status import get_process_start_time

        return pid, get_process_start_time(pid)
    except Exception:
        return pid, None


def _owner_alive(pid: Any, started_at: Any) -> bool:
    """True when the recorded owning process still exists (pid + start time)."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        from gateway.status import get_process_start_time

        current_start = get_process_start_time(pid)
    except Exception:
        current_start = None
    if current_start is None:
        # No such process (or unreadable) — treat unreadable-but-extant
        # processes as alive only if the pid exists. Route through the
        # cross-platform probe: ``os.kill(pid, 0)`` on Windows is NOT a
        # no-op (bpo-14484 — CPython maps sig=0 to
        # ``GenerateConsoleCtrlEvent(0, pid)``), so a raw probe here could
        # Ctrl+C the gateway's own console group whenever psutil failed to
        # read the start time of a live pid. ``_pid_exists`` keeps the
        # EPERM-means-alive semantics (exists but owned by another user).
        try:
            from gateway.status import _pid_exists
        except Exception:
            if os.name == "nt":
                # Never fall back to a raw sig-0 probe on Windows.
                return False
            try:
                os.kill(pid, 0)  # windows-footgun: ok — POSIX-only fallback branch
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            except OSError:
                return False
            return True
        try:
            return bool(_pid_exists(pid))
        except Exception:
            return False
    if started_at is None:
        return True
    try:
        return int(current_start) == int(started_at)
    except (TypeError, ValueError):
        return True


def compute_obligation_id(session_key: str, message_ref: str, content: str) -> str:
    """Stable id: same turn + same content re-records idempotently, while
    distinct threads/topics on the same chat can never collide (the
    session_key carries platform, chat and thread; ``message_ref`` is the
    triggering inbound message id, distinguishing turns in one session)."""
    payload = f"{session_key}|{message_ref}|{content}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:24]


def record_obligation(
    *,
    obligation_id: str,
    session_key: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    content: str,
    adapter_profile: Optional[str] = None,
) -> None:
    """Record a final response as owed to the platform (state='pending')."""
    now = time.time()
    stored_profile = str(adapter_profile).strip() if adapter_profile else "default"
    pid, started = _owner_stamp()
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id,
                content, state, attempts, created_at, updated_at,
                owner_pid, owner_started_at, adapter_profile)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)""",
            (obligation_id, session_key, platform, str(chat_id),
             str(thread_id) if thread_id else None, content, now, now,
             pid, started, stored_profile),
        )
    _prune()


def mark_attempting(obligation_id: str) -> None:
    _update_state(obligation_id, "attempting")


def mark_delivered(obligation_id: str) -> None:
    _update_state(obligation_id, "delivered")


def mark_failed(obligation_id: str, error: str = "") -> None:
    _update_state(obligation_id, "failed", error=error)


def mark_deferred(
    obligation_id: str,
    retry_after: float,
    *,
    now: Optional[float] = None,
    error_kind: str = "flood_control",
) -> float:
    """Persist an explicit unsent Telegram flood rejection for later retry.

    ``retry_after`` is server authority.  Invalid, negative, or non-finite
    values are rejected rather than converted into an early retry.  A small
    bounded positive jitter prevents a fleet from waking on the same instant.
    Returns the persisted wall-clock due time for scheduling/tests.
    """
    try:
        delay = float(retry_after)
    except (TypeError, ValueError) as exc:
        raise ValueError("retry_after must be finite and nonnegative") from exc
    if not math.isfinite(delay) or delay < 0:
        raise ValueError("retry_after must be finite and nonnegative")
    current = time.time() if now is None else float(now)
    if not math.isfinite(current):
        raise ValueError("now must be finite")
    jitter = random.uniform(0.0, _MAX_DEFER_JITTER_SECONDS)
    due = current + delay + max(0.0, min(float(jitter), _MAX_DEFER_JITTER_SECONDS))
    sanitized_error = _sanitize_error_kind(error_kind, "deferred_retry")
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """UPDATE delivery_obligations
               SET state='deferred', retry_not_before=?, updated_at=?,
                   last_error=?
               WHERE obligation_id=? AND state != 'delivered'""",
            (due, current, sanitized_error, obligation_id),
        )
    return due


def mark_deferred_failed(obligation_id: str, error_kind: str) -> None:
    """Make a claimed deferred retry terminal without exposing raw errors."""
    sanitized = _sanitize_error_kind(error_kind, "deferred_send_failed")
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """UPDATE delivery_obligations
               SET state='failed', updated_at=?, last_error=?
               WHERE obligation_id=? AND state != 'delivered'
                 AND retry_not_before IS NOT NULL""",
            (time.time(), sanitized, obligation_id),
        )


def release_runtime_claim(obligation_id: str, error: str = "") -> bool:
    """Return an unsent runtime claim to ``failed`` without spending an attempt.

    Runtime recovery claims before clearing ``resume_pending`` so that two
    reconnect paths cannot send the same row. If the session flag cannot be
    cleared, no platform send was attempted and the claim must not consume the
    bounded redelivery budget. Release is fail-closed to the exact current
    process instance and the ``attempting`` state.
    """
    pid, started = _owner_stamp()
    if started is None:
        return False
    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state='failed', attempts=CASE
                       WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                   updated_at=?, last_error=?
               WHERE obligation_id=? AND state='attempting'
                 AND owner_pid IS ? AND owner_started_at IS ?""",
            (time.time(), error[:500] if error else None,
             obligation_id, pid, started),
        )
    return bool(cursor.rowcount)


def _update_state(obligation_id: str, state: str, error: str = "") -> None:
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """UPDATE delivery_obligations
               SET state=?, updated_at=?, last_error=?,
                   retry_not_before=CASE
                       WHEN ? IN ('delivered', 'failed', 'abandoned') THEN NULL
                       ELSE retry_not_before END
               WHERE obligation_id=?""",
            (state, time.time(), error[:500] if error else None,
             state, obligation_id),
        )


def _normalized_profile(profile: Optional[str]) -> str:
    return "default" if not profile or profile == "default" else str(profile)


def claim_due_deferred(
    *,
    profile: Optional[str] = None,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically claim the first due Telegram obligation for one bot.

    Ordering is deterministic by due time, creation time, then id.  A claim is
    also the point where one unit of the existing retry budget is spent.
    Rows owned by another live process are never touched; dead-owner rows are
    eligible so restart reconstructs the same durable queue.
    """
    current = time.time() if now is None else float(now)
    expected_profile = _normalized_profile(profile)
    pid, started = _owner_stamp()
    with _DB_LOCK, _transaction() as conn:
        in_flight = conn.execute(
            """SELECT owner_pid, owner_started_at
               FROM delivery_obligations
               WHERE platform='telegram' AND adapter_profile=?
                 AND retry_not_before IS NOT NULL AND state='attempting'""",
            (expected_profile,),
        ).fetchall()
        if any(_owner_alive(owner_pid, owner_started_at)
               for owner_pid, owner_started_at in in_flight):
            return None
        rows = conn.execute(
            """SELECT obligation_id, session_key, chat_id, thread_id, content,
                      attempts, created_at, owner_pid, owner_started_at,
                      retry_not_before, state
               FROM delivery_obligations
               WHERE platform='telegram' AND adapter_profile=?
                 AND retry_not_before IS NOT NULL
                 AND retry_not_before <= ?
                 AND state IN ('deferred', 'attempting')
               ORDER BY retry_not_before, created_at, obligation_id""",
            (expected_profile, current),
        ).fetchall()
        for (
            oid, session_key, chat_id, thread_id, content, attempts, created_at,
            owner_pid, owner_started_at, due, state,
        ) in rows:
            owner_is_current = owner_pid == pid and owner_started_at == started
            owner_is_alive = _owner_alive(owner_pid, owner_started_at)
            # A current-process attempting row already belongs to the active
            # send.  It becomes dead-owner restart work if shutdown interrupts.
            if state == "attempting" and owner_is_current:
                continue
            if owner_is_alive and not owner_is_current:
                continue
            if attempts >= MAX_ATTEMPTS or (current - created_at) > STALE_AFTER_SECONDS:
                conn.execute(
                    """UPDATE delivery_obligations
                       SET state='abandoned', retry_not_before=NULL, updated_at=?
                       WHERE obligation_id=? AND state=?
                         AND owner_pid IS ? AND owner_started_at IS ?""",
                    (current, oid, state, owner_pid, owner_started_at),
                )
                continue
            cursor = conn.execute(
                """UPDATE delivery_obligations
                   SET state='attempting', owner_pid=?, owner_started_at=?,
                       attempts=attempts+1, updated_at=?
                   WHERE obligation_id=? AND state=?
                     AND owner_pid IS ? AND owner_started_at IS ?""",
                (pid, started, current, oid, state, owner_pid, owner_started_at),
            )
            if cursor.rowcount:
                return {
                    "obligation_id": oid,
                    "session_key": session_key,
                    "platform": "telegram",
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "content": content,
                    "profile": expected_profile,
                    "attempts": attempts + 1,
                    "retry_not_before": due,
                }
    return None


def next_deferred_due(
    *,
    profile: Optional[str] = None,
    now: Optional[float] = None,
) -> Optional[float]:
    """Return the next schedulable due time for one Telegram bot scope."""
    current = time.time() if now is None else float(now)
    expected_profile = _normalized_profile(profile)
    pid, started = _owner_stamp()
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT retry_not_before, state, owner_pid, owner_started_at
               FROM delivery_obligations
               WHERE platform='telegram' AND adapter_profile=?
                 AND retry_not_before IS NOT NULL
                 AND state IN ('deferred', 'attempting')
               ORDER BY retry_not_before, created_at, obligation_id""",
            (expected_profile,),
        ).fetchall()
    for due, state, owner_pid, owner_started_at in rows:
        owner_is_current = owner_pid == pid and owner_started_at == started
        if state == "attempting" and owner_is_current:
            continue
        if _owner_alive(owner_pid, owner_started_at) and not owner_is_current:
            continue
        return max(current, float(due))
    return None


def release_deferred_claim(obligation_id: str) -> bool:
    """Release a claimed row when no transport send was started.

    The due time is preserved and the attempt increment is refunded.  This is
    deliberately separate from :func:`mark_deferred`, whose only caller-side
    meaning is an explicit Telegram flood rejection.
    """
    pid, started = _owner_stamp()
    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state='deferred', attempts=CASE
                       WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                   updated_at=?
               WHERE obligation_id=? AND state='attempting'
                 AND retry_not_before IS NOT NULL
                 AND owner_pid IS ? AND owner_started_at IS ?""",
            (time.time(), obligation_id, pid, started),
        )
    return bool(cursor.rowcount)


def sweep_recoverable(
    now: Optional[float] = None,
    *,
    deliverable_platforms: Optional[set] = None,
    deliverable_targets: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Claim undelivered rows owned by dead processes; return them for
    redelivery.

    Claiming atomically re-stamps the owner to THIS process and increments
    ``attempts``, so a second gateway racing the same sweep cannot
    double-claim (the UPDATE is guarded on the previous owner stamp).
    Rows over the attempts cap or older than the stale cutoff transition to
    'abandoned' instead of being returned.

    ``deliverable_platforms`` (platform value strings) restricts claiming to
    platforms the caller can actually send on this boot.  ``attempts`` is the
    redelivery budget, so it must only be spent on a real send: a platform
    that failed to connect would otherwise burn one attempt per boot and hit
    the cap having never been sent once.  Rows for absent platforms are left
    untouched for a later boot; the stale cutoff still bounds them.

    ``deliverable_targets`` further scopes multiplexed gateways by exact
    ``(platform, adapter_profile)`` identity, preventing one connected bot from
    spending another disconnected bot's retry budget.
    """
    now = now if now is not None else time.time()
    pid, started = _owner_stamp()
    claimed: List[Dict[str, Any]] = []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      content, state, attempts, created_at,
                      owner_pid, owner_started_at, adapter_profile
               FROM delivery_obligations
               WHERE state IN ('pending', 'attempting', 'failed')
                 AND NOT (platform='telegram' AND retry_not_before IS NOT NULL)"""
        ).fetchall()
        for (oid, session_key, platform, chat_id, thread_id, content, state,
             attempts, created_at, owner_pid, owner_started_at,
             adapter_profile) in rows:
            if _owner_alive(owner_pid, owner_started_at):
                continue  # a live gateway still owns this row
            if attempts >= MAX_ATTEMPTS or (now - created_at) > STALE_AFTER_SECONDS:
                conn.execute(
                    """UPDATE delivery_obligations
                       SET state='abandoned', updated_at=? WHERE obligation_id=?""",
                    (now, oid),
                )
                continue
            if (
                deliverable_platforms is not None
                and platform not in deliverable_platforms
            ):
                # No adapter for this platform this boot — the caller cannot
                # send, so claiming would spend an attempt on a no-op.
                continue
            if (
                deliverable_targets is not None
                and (platform, adapter_profile) not in deliverable_targets
            ):
                continue
            cursor = conn.execute(
                """UPDATE delivery_obligations
                   SET owner_pid=?, owner_started_at=?, attempts=attempts+1,
                       updated_at=?
                   WHERE obligation_id=? AND (owner_pid IS ? OR owner_pid=?)""",
                (pid, started, now, oid, owner_pid, owner_pid),
            )
            if cursor.rowcount:
                claimed.append({
                    "obligation_id": oid,
                    "session_key": session_key,
                    "platform": platform,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "content": content,
                    # pending = send never started, redeliver plainly;
                    # attempting/failed = ambiguous or rejected, carry marker.
                    "needs_marker": state != "pending",
                    "profile": adapter_profile,
                    "attempts": attempts + 1,
                })
    return claimed


def sweep_failed_for_runtime(
    platform: str,
    now: Optional[float] = None,
    *,
    profile: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Claim this process's reconnect-retryable failed rows for one adapter.

    ``profile`` scopes multiplexed gateways to the bot identity that actually
    owned the failed send; ``None`` means the primary/default adapter. The
    persisted adapter owner is independent of the routed session namespace.

    Startup recovery intentionally ignores rows owned by a live gateway. That
    protects concurrent processes, but it also means a final response rejected
    with ``send_path_degraded`` remains stranded when only the platform adapter
    reconnects. This runtime sweep closes that gap without weakening ownership:

    - only rows stamped to this exact process instance are eligible;
    - only explicitly allowlisted transient errors are eligible;
    - attempts/staleness bounds match startup recovery;
    - every update is guarded by the prior owner stamp and ``failed`` state.

    Unowned rows and rows owned by another process are left untouched for the
    normal startup/dead-owner sweep. Claimed rows always carry the reconnect
    marker because the failed send's acknowledgement is not safe to infer.
    """
    now = now if now is not None else time.time()
    pid, started = _owner_stamp()
    if started is None:
        # PID equality alone cannot distinguish this process from a stale row
        # left by an earlier process incarnation after PID reuse. Runtime replay
        # is optional recovery, so fail closed when the process fingerprint is
        # unavailable; startup recovery remains the durable fallback.
        return []
    claimed: List[Dict[str, Any]] = []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      content, attempts, created_at, owner_pid,
                      owner_started_at, last_error, adapter_profile
               FROM delivery_obligations
               WHERE state='failed' AND platform=?""",
            (platform,),
        ).fetchall()
        for (
            oid,
            session_key,
            row_platform,
            chat_id,
            thread_id,
            content,
            attempts,
            created_at,
            owner_pid,
            owner_started_at,
            last_error,
            adapter_profile,
        ) in rows:
            expected_profile = (
                "default" if not profile or profile == "default" else str(profile)
            )
            if adapter_profile != expected_profile:
                continue
            # Runtime reconnect recovery may act only on its own rows. Exact
            # process-start matching prevents PID reuse from stealing work.
            if owner_pid != pid or owner_started_at != started:
                continue
            if str(last_error or "").strip().lower() not in _RUNTIME_RETRYABLE_ERRORS:
                continue
            owner_guard = (oid, owner_pid, owner_started_at)
            if attempts >= MAX_ATTEMPTS or (now - created_at) > STALE_AFTER_SECONDS:
                conn.execute(
                    """UPDATE delivery_obligations
                       SET state='abandoned', updated_at=?
                       WHERE obligation_id=? AND state='failed'
                         AND owner_pid IS ? AND owner_started_at IS ?""",
                    (now, *owner_guard),
                )
                continue
            cursor = conn.execute(
                """UPDATE delivery_obligations
                   SET state='attempting', attempts=attempts+1, updated_at=?
                   WHERE obligation_id=? AND state='failed'
                     AND owner_pid IS ? AND owner_started_at IS ?""",
                (now, *owner_guard),
            )
            if cursor.rowcount:
                claimed.append({
                    "obligation_id": oid,
                    "session_key": session_key,
                    "platform": row_platform,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "content": content,
                    "needs_marker": True,
                    "marker": RECONNECTED_MARKER,
                    "profile": adapter_profile,
                    "runtime_recovery": True,
                    "attempts": attempts + 1,
                })
    return claimed


def _prune(now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    cutoff = now - _RETENTION_SECONDS
    try:
        with _transaction() as conn:
            conn.execute(
                """DELETE FROM delivery_obligations
                   WHERE (state IN ('delivered', 'abandoned')
                          OR (state='failed' AND retry_not_before IS NOT NULL))
                     AND updated_at < ?""",
                (cutoff,),
            )
            total = conn.execute(
                "SELECT COUNT(*) FROM delivery_obligations"
            ).fetchone()[0]
            excess = max(0, total - _MAX_ROWS)
            if excess:
                conn.execute(
                    """DELETE FROM delivery_obligations WHERE obligation_id IN (
                         SELECT obligation_id FROM delivery_obligations
                         ORDER BY CASE state
                                    WHEN 'delivered' THEN 0
                                    WHEN 'abandoned' THEN 1
                                    ELSE 2
                                  END, updated_at ASC
                         LIMIT ?)""",
                    (excess,),
                )
    except Exception:
        logger.debug("delivery ledger prune failed", exc_info=True)


def ledger_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Read the ``gateway.delivery_ledger`` config gate (default on)."""
    try:
        if config is None:
            from hermes_cli.config import load_config

            config = load_config()
        gw = config.get("gateway") or {}
        value = gw.get("delivery_ledger", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "off"}
        return bool(value)
    except Exception:
        return True


def debug_rows(limit: int = 20) -> str:
    """Human-readable dump for ad-hoc inspection (sqlite3-free path)."""
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, state, attempts,
                      created_at, updated_at, last_error
               FROM delivery_obligations
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return json.dumps(
        [
            {
                "id": r[0], "session": r[1], "state": r[2], "attempts": r[3],
                "created_at": r[4], "updated_at": r[5], "last_error": r[6],
            }
            for r in rows
        ],
        indent=2,
    )
