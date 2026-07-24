#!/usr/bin/env python3
"""External, fail-safe Kanban dispatcher loop.

This process is deliberately outside the gateway.  It refuses to run when
the gateway owns dispatch, probes every selected board read-only before any
spawn, and limits the whole tick rather than applying a cap independently to
each board.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("HERMES_HOME") or "/home/hermes/.hermes")
REPO = Path(
    os.environ.get("HERMES_REPO")
    or str(ROOT / "hermes-agent")
)
PYTHON = Path(
    os.environ.get("HERMES_PYTHON")
    or "/home/hermes/.hermes/hermes-agent/venv/bin/python"
)
DB = Path(os.environ.get("HERMES_KANBAN_DB") or str(ROOT / "kanban.db"))
_DEFAULT_DB_AT_IMPORT = DB
STATE_DIR = ROOT / "state"
LOCK_PATH = STATE_DIR / "kanban-safe-dispatcher.lock"
CURSOR_PATH = STATE_DIR / "kanban-safe-dispatcher.cursor"
DEFAULT_INTERVAL = 60
DEFAULT_MAX_SPAWN = 1
DEFAULT_FAILURE_LIMIT = 2
DEFAULT_BOARDS = ("default",)


def _load_config() -> dict[str, Any]:
    sys.path.insert(0, str(REPO))
    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _kanban_module():
    sys.path.insert(0, str(REPO))
    from hermes_cli import kanban_db

    return kanban_db


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _root_dispatch_enabled(cfg: dict[str, Any]) -> bool:
    kanban = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    if not isinstance(kanban, dict):
        return True
    return bool(kanban.get("dispatch_in_gateway", True))


def _dispatch_limits(cfg: dict[str, Any]) -> tuple[int, int]:
    kanban = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    if not isinstance(kanban, dict):
        kanban = {}
    # The wrapper owns one aggregate cap. Native kanban.max_spawn remains a
    # per-board live cap and is supplied separately for each child CLI call.
    max_spawn = _positive_int(
        os.environ.get("HERMES_SAFE_DISPATCH_MAX"), DEFAULT_MAX_SPAWN,
    )
    failure_limit = _positive_int(
        kanban.get("failure_limit"), DEFAULT_FAILURE_LIMIT
    )
    return max_spawn, failure_limit


def _parse_board_allowlist(raw: str | None = None) -> list[str]:
    """Return the configured board order, or fail closed on bad input."""
    if raw is None:
        if "HERMES_SAFE_DISPATCH_BOARDS" not in os.environ:
            return list(DEFAULT_BOARDS)
        raw = os.environ["HERMES_SAFE_DISPATCH_BOARDS"]
    if not raw.strip():
        raise ValueError("HERMES_SAFE_DISPATCH_BOARDS must not be empty")

    kb = _kanban_module()
    boards: list[str] = []
    seen: set[str] = set()
    for value in raw.split(","):
        value = value.strip()
        if not value:
            raise ValueError(
                "HERMES_SAFE_DISPATCH_BOARDS contains an empty board slug"
            )
        try:
            board = kb._normalize_board_slug(value)
        except ValueError as exc:
            raise ValueError(f"invalid safe-dispatch board {value!r}: {exc}") from exc
        if not board:
            raise ValueError(
                f"HERMES_SAFE_DISPATCH_BOARDS contains empty board {value!r}"
            )
        if board not in seen:
            seen.add(board)
            boards.append(board)
    if not boards:
        raise ValueError("HERMES_SAFE_DISPATCH_BOARDS selected no boards")
    if os.environ.get("HERMES_KANBAN_DB", "").strip() and len(boards) > 1:
        raise ValueError(
            "HERMES_KANBAN_DB pins one database and cannot be used with "
            "multiple safe-dispatch boards"
        )
    return boards


def _state_dir() -> Path:
    return Path(os.environ.get("HERMES_SAFE_DISPATCH_STATE_DIR") or STATE_DIR)


def _cursor_path() -> Path:
    return _state_dir() / CURSOR_PATH.name


def _read_cursor(board_count: int) -> int:
    if board_count <= 0:
        return 0
    try:
        return int(_cursor_path().read_text(encoding="utf-8").strip()) % board_count
    except (OSError, ValueError):
        return 0


def _write_cursor(value: int) -> None:
    path = _cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(f"{value}\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _board_db_path(board: str) -> Path:
    # Preserve the legacy explicit DB override for the default-only mode. A
    # named-board allowlist is rejected above when that override is present.
    if os.environ.get("HERMES_KANBAN_DB", "").strip():
        return Path(os.environ["HERMES_KANBAN_DB"]).expanduser()
    # Keep the live script's patchable ``DB`` seam for default-board tests and
    # explicit operator overrides, while resolving named boards through core.
    if board == "default" and DB != _DEFAULT_DB_AT_IMPORT:
        return Path(DB).expanduser()
    return _kanban_module().kanban_db_path(board=board)


def _probe_db(board: str = "default") -> tuple[bool, str]:
    """Run the health check against exactly ``board``'s SQLite file."""
    try:
        resolved = _board_db_path(board).resolve()
        if not resolved.is_file():
            return False, f"board {board!r} missing db: {resolved}"
        kanban_db = _kanban_module()
        with kanban_db.connect_readonly_closing(db_path=resolved) as con:
            quick = con.execute("PRAGMA quick_check").fetchone()
            integ = con.execute("PRAGMA integrity_check").fetchone()
        quick_value = quick[0] if quick else None
        integrity_value = integ[0] if integ else None
        if (quick_value or "").lower() != "ok" or (
            integrity_value or ""
        ).lower() != "ok":
            return (
                False,
                f"board {board!r} sqlite check failed: "
                f"quick={quick_value!r} integrity={integrity_value!r}",
            )
        return True, "ok"
    except Exception as exc:
        return (
            False,
            f"board {board!r} sqlite probe failed: {type(exc).__name__}: {exc}",
        )


def _count_running(board: str) -> int:
    """Count live rows on a board without opening the writer connection."""
    resolved = _board_db_path(board).resolve()
    kanban_db = _kanban_module()
    with kanban_db.connect_readonly_closing(db_path=resolved) as con:
        row = con.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
        ).fetchone()
        return int(row[0] if row else 0)


def _dispatch_board(
    board: str, *, failure_limit: int, dry_run: bool,
    max_spawn: int = 1, spawn_budget: int = 1,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(ROOT)
    env.setdefault("HERMES_PROFILE", "default")
    env["HERMES_KANBAN_BOARD"] = board
    cmd = [
        str(PYTHON),
        "-m",
        "hermes_cli.main",
        "kanban",
        "--board",
        board,
        "dispatch",
        "--max",
        str(max_spawn),
        "--spawn-budget",
        str(spawn_budget),
        "--failure-limit",
        str(failure_limit),
        "--json",
    ]
    if dry_run:
        cmd.append("--dry-run")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "board": board,
            "error": f"dispatch timeout after {exc.timeout}s",
        }
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "board": board,
            "returncode": proc.returncode,
            "error": f"invalid dispatcher JSON: {exc}",
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }
    if proc.returncode != 0:
        return {
            "ok": False,
            "board": board,
            "returncode": proc.returncode,
            "stderr": proc.stderr[-4000:],
            "payload": payload,
        }
    return {"ok": True, "board": board, "dry_run": dry_run, "payload": payload}


def _spawn_count(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    spawned = payload.get("spawned")
    return len(spawned) if isinstance(spawned, list) else 0


def _aggregate_payload(records: list[dict[str, Any]]) -> dict[str, Any]:
    aggregate: dict[str, Any] = {
        "spawned": [],
        "reclaimed": 0,
        "crashed": [],
        "timed_out": [],
        "stale": [],
        "auto_blocked": [],
        "promoted": 0,
    }
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        board = record.get("board")
        for item in payload.get("spawned", []) or []:
            if isinstance(item, dict):
                item = {**item, "board": board}
            aggregate["spawned"].append(item)
        for key in ("crashed", "timed_out", "stale", "auto_blocked"):
            values = payload.get(key, [])
            if isinstance(values, list):
                aggregate[key].extend(values)
        for key in ("reclaimed", "promoted"):
            try:
                aggregate[key] += int(payload.get(key) or 0)
            except (TypeError, ValueError):
                pass
    return aggregate


def _board_summaries(
    records: list[dict[str, Any]], boards: list[str],
) -> list[dict[str, Any]]:
    """Collapse maintenance and allocation receipts into one row per board."""
    summaries = []
    for board in boards:
        board_records = [
            record for record in records if record.get("board") == board
        ]
        if not board_records:
            continue
        payload = _aggregate_payload(board_records)
        summaries.append(
            {
                "board": board,
                "ok": all(record.get("ok") for record in board_records),
                "spawned": _spawn_count(payload),
                "payload": payload,
            }
        )
    return summaries


def _dispatch_once(*, dry_run: bool = False) -> dict[str, Any]:
    cfg = _load_config()
    if _root_dispatch_enabled(cfg):
        return {
            "ok": True,
            "skipped": "embedded_dispatcher_enabled",
        }
    try:
        boards = _parse_board_allowlist()
        max_spawn, failure_limit = _dispatch_limits(cfg)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    # Probe every selected DB before dispatching any board. This prevents a
    # partial green tick when one configured project board is unhealthy.
    for board in boards:
        healthy, reason = _probe_db(board)
        if not healthy:
            return {
                "ok": False,
                "error": reason,
                "boards": [{"board": board, "ok": False, "error": reason}],
            }

    try:
        running_by_board = {board: _count_running(board) for board in boards}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"could not count running tasks read-only: {type(exc).__name__}: {exc}",
        }
    start = _read_cursor(len(boards))
    ordered_boards = boards[start:] + boards[:start]
    records: list[dict[str, Any]] = []
    maintenance_errors: list[str] = []

    # Maintenance is independent of aggregate spawn capacity. Visit boards in
    # configured order so the persisted allocation cursor cannot bias reclaim
    # or promotion, and explicitly disable all new spawns in the native core.
    for board in boards:
        record = _dispatch_board(
            board,
            failure_limit=failure_limit,
            dry_run=dry_run,
            max_spawn=max(1, running_by_board[board] + 1),
            spawn_budget=0,
        )
        records.append(record)
        if not record.get("ok"):
            maintenance_errors.append(f"safe maintenance failed on board {board!r}")
            continue
        if _spawn_count(record.get("payload")):
            maintenance_errors.append(
                f"board {board!r} exceeded zero-spawn maintenance budget"
            )

    if maintenance_errors:
        return {
            "ok": False,
            "dry_run": dry_run,
            "max_spawn": max_spawn,
            "boards": _board_summaries(records, boards),
            "payload": _aggregate_payload(records),
            "error": "; ".join(maintenance_errors),
        }

    try:
        post_maintenance_running = sum(_count_running(board) for board in boards)
    except Exception as exc:
        return {
            "ok": False,
            "dry_run": dry_run,
            "max_spawn": max_spawn,
            "boards": _board_summaries(records, boards),
            "payload": _aggregate_payload(records),
            "error": (
                "could not re-count running tasks after maintenance: "
                f"{type(exc).__name__}: {exc}"
            ),
        }

    # This is the conservative floor for allocation. Reclamation may have
    # freed a slot, but a later reclaim/race must not manufacture another one
    # during this tick.
    baseline_running = post_maintenance_running
    allocation_attempted = baseline_running < max_spawn
    spawned_total = 0
    while baseline_running + spawned_total < max_spawn:
        round_spawned = 0
        for board in ordered_boards:
            if baseline_running + spawned_total >= max_spawn:
                break
            # Re-read occupancy before each subprocess call, but retain the
            # tick's post-maintenance occupancy plus reservations made by
            # earlier calls as the conservative floor. Maintenance in a child dispatch can
            # reclaim rows; that must never manufacture extra global slots in
            # the same wrapper tick.
            try:
                current_running = sum(_count_running(item) for item in boards)
            except Exception as exc:
                return {
                    "ok": False,
                    "max_spawn": max_spawn,
                    "boards": _board_summaries(records, boards),
                    "payload": _aggregate_payload(records),
                    "error": f"could not re-count running tasks: {type(exc).__name__}: {exc}",
                }
            effective_running = max(baseline_running + spawned_total, current_running)
            if effective_running >= max_spawn:
                break
            try:
                board_running = _count_running(board)
            except Exception as exc:
                return {
                    "ok": False,
                    "max_spawn": max_spawn,
                    "boards": _board_summaries(records, boards),
                    "payload": _aggregate_payload(records),
                    "error": f"could not count board {board!r}: {type(exc).__name__}: {exc}",
                }
            record = _dispatch_board(
                board,
                failure_limit=failure_limit,
                dry_run=dry_run,
                max_spawn=max(1, board_running + 1),
                spawn_budget=1,
            )
            records.append(record)
            if not record.get("ok"):
                return {
                    "ok": False,
                    "max_spawn": max_spawn,
                    "boards": _board_summaries(records, boards),
                    "payload": _aggregate_payload(records),
                    "error": f"safe dispatch failed on board {board!r}",
                }
            count = _spawn_count(record.get("payload"))
            if count > 1:
                return {
                    "ok": False,
                    "max_spawn": max_spawn,
                    "boards": records,
                    "payload": _aggregate_payload(records),
                    "error": f"board {board!r} exceeded one-spawn budget",
                }
            spawned_total += count
            round_spawned += count
        if round_spawned == 0:
            break

    if not dry_run and allocation_attempted:
        _write_cursor((start + 1) % len(boards))
    return {
        "ok": True,
        "dry_run": dry_run,
        "max_spawn": max_spawn,
        "running": baseline_running + spawned_total,
        "boards": _board_summaries(records, boards),
        "payload": _aggregate_payload(records),
    }


def _interesting(result: dict[str, Any]) -> bool:
    if not result.get("ok"):
        return True
    payload = result.get("payload") or {}
    if not isinstance(payload, dict):
        return False
    return bool(
        payload.get("spawned")
        or payload.get("reclaimed")
        or payload.get("crashed")
        or payload.get("timed_out")
        or payload.get("stale")
        or payload.get("auto_blocked")
        or payload.get("promoted")
    )


def _summarize(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return json.dumps({"safe_dispatch_error": result}, ensure_ascii=False)
    payload = result.get("payload") or {}
    if not isinstance(payload, dict):
        return ""
    summary = {
        "safe_dispatch": "dry_run" if result.get("dry_run") else "tick",
        "max_spawn": result.get("max_spawn"),
        "boards": [
            {
                "board": item.get("board"),
                "spawned": item.get("spawned", 0),
                "reclaimed": (item.get("payload") or {}).get("reclaimed", 0),
                "promoted": (item.get("payload") or {}).get("promoted", 0),
            }
            for item in result.get("boards", [])
        ],
        "spawned": payload.get("spawned", []),
        "reclaimed": payload.get("reclaimed", 0),
        "crashed": payload.get("crashed", []),
        "timed_out": payload.get("timed_out", []),
        "stale": payload.get("stale", []),
        "auto_blocked": payload.get("auto_blocked", []),
        "promoted": payload.get("promoted", 0),
    }
    return json.dumps(summary, ensure_ascii=False)


def _run_loop(*, once: bool, dry_run: bool, interval: int) -> int:
    """Run ticks after any required non-dry-run coordination is established."""
    while True:
        try:
            result = _dispatch_once(dry_run=dry_run)
        except Exception as exc:
            result = {
                "ok": False,
                "error": f"unexpected: {type(exc).__name__}: {exc}",
            }
        if _interesting(result):
            print(_summarize(result), flush=True)
        if once:
            return 0 if result.get("ok") else 1
        time.sleep(max(10, int(interval or DEFAULT_INTERVAL)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="Run one tick and exit")
    ap.add_argument("--dry-run", action="store_true", help="Preview dispatch without spawning")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    args = ap.parse_args()

    # A preview has no shared mutable state and must not bootstrap any of the
    # persistent dispatcher state. In particular, do not create a state
    # directory, lock file, cursor, or any temporary sibling file.
    if args.dry_run:
        return _run_loop(
            once=args.once,
            dry_run=True,
            interval=args.interval,
        )

    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / LOCK_PATH.name
    with open(lock_path, "w", encoding="utf-8") as lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("kanban safe dispatcher already running", file=sys.stderr)
            return 0
        return _run_loop(
            once=args.once,
            dry_run=False,
            interval=args.interval,
        )


if __name__ == "__main__":
    raise SystemExit(main())
