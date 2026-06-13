"""ByteRover memory plugin — MemoryProvider interface.

Persistent memory via the ByteRover CLI (``brv``). Organizes knowledge into
a hierarchical context tree with tiered retrieval (fuzzy text → LLM-driven
search). Local-first with optional cloud sync.

Original PR #3499 by hieuntg81, adapted to MemoryProvider ABC.

Requires: ``brv`` CLI installed (npm install -g byterover-cli or
curl -fsSL https://byterover.dev/install.sh | sh).

Config via environment variables (profile-scoped via each profile's .env):
  BRV_API_KEY   — ByteRover API key (for cloud features, optional for local)

Working directory: $HERMES_HOME/byterover/ (profile-scoped context tree)
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# Timeouts
_QUERY_TIMEOUT = 10   # brv query — should be fast
_CURATE_TIMEOUT = 120  # brv curate — may involve LLM processing

# Minimum lengths to filter noise
_MIN_QUERY_LEN = 10
_MIN_OUTPUT_LEN = 20


# ---------------------------------------------------------------------------
# brv binary resolution (cached, thread-safe)
# ---------------------------------------------------------------------------

_brv_path_lock = threading.Lock()
_cached_brv_path: Optional[str] = None


def _is_executable(path: Path) -> bool:
    """Return True when *path* points to an executable file."""
    return path.is_file() and os.access(path, os.X_OK)


def _real_user_home() -> Path:
    """Return the OS account home even when HOME is profile/wrapper-scoped."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path.home()


def _candidate_brv_paths() -> List[Path]:
    """Preferred brv locations, ordered before generic PATH lookup.

    Gateway/profile processes may run with a minimal PATH where an old system
    install (for example /usr/bin/brv) wins over the user-local npm install.
    Prefer explicit/configured and profile-local shims first so long-running
    Hermes processes do not silently select stale CLIs.
    """
    from hermes_constants import get_hermes_home

    candidates: List[Path] = []
    for env_name in ("BRV_CLI_PATH", "BYTEROVER_CLI_PATH"):
        value = os.environ.get(env_name, "").strip()
        if value:
            candidates.append(Path(value).expanduser())

    hermes_home = get_hermes_home()
    candidates.extend([
        hermes_home / "home" / ".local" / "bin" / "brv",
        hermes_home / ".local" / "bin" / "brv",
    ])

    homes = [Path.home(), _real_user_home()]
    for home in homes:
        candidates.extend([
            home / ".npm-global" / "bin" / "brv",
            home / ".local" / "bin" / "brv",
            home / ".brv-cli" / "bin" / "brv",
        ])

    candidates.append(Path("/usr/local/bin/brv"))

    seen = set()
    unique: List[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def _resolve_brv_path() -> Optional[str]:
    """Find the brv binary, preferring configured/current installs over PATH."""
    global _cached_brv_path
    with _brv_path_lock:
        if _cached_brv_path is not None:
            cached = Path(_cached_brv_path) if _cached_brv_path else None
            if cached and _is_executable(cached):
                return str(cached)
            _cached_brv_path = None

    found: Optional[str] = None
    for candidate in _candidate_brv_paths():
        if _is_executable(candidate):
            found = str(candidate)
            break

    if not found:
        found = shutil.which("brv")

    with _brv_path_lock:
        if _cached_brv_path is not None:
            return _cached_brv_path if _cached_brv_path != "" else None
        _cached_brv_path = found or ""
    return found


def _run_brv(args: List[str], timeout: int = _QUERY_TIMEOUT,
             cwd: str = None) -> dict:
    """Run a brv CLI command. Returns {success, output, error}."""
    brv_path = _resolve_brv_path()
    if not brv_path:
        return {"success": False, "error": "brv CLI not found. Install: npm install -g byterover-cli"}

    cmd = [brv_path] + args
    effective_cwd = cwd or str(_get_brv_cwd())
    Path(effective_cwd).mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    brv_bin_dir = str(Path(brv_path).parent)
    env["PATH"] = brv_bin_dir + os.pathsep + env.get("PATH", "")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=effective_cwd, env=env,
            stdin=subprocess.DEVNULL,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode == 0:
            return {"success": True, "output": stdout, "stderr": stderr}
        return {"success": False, "error": stderr or stdout or f"brv exited {result.returncode}", "output": stdout, "stderr": stderr}

    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"brv timed out after {timeout}s"}
    except FileNotFoundError:
        global _cached_brv_path
        with _brv_path_lock:
            _cached_brv_path = None
        return {"success": False, "error": "brv CLI not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _get_brv_cwd() -> Path:
    """Profile-scoped working directory for the brv context tree."""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "byterover"


def _extract_curate_log_id(output: str) -> Optional[str]:
    """Extract a ByteRover curate log id from command output when present."""
    match = re.search(r"\bcur-\d+\b", output or "")
    return match.group(0) if match else None


def _curate_detail_has_write(detail: str) -> bool:
    """Return True when a completed curate detail shows at least one operation."""
    if "Status:" not in detail or "completed" not in detail.lower():
        return False
    if "Operations:" not in detail:
        return False
    operations = detail.split("Operations:", 1)[1]
    operations = operations.split("Summary:", 1)[0]
    return "✓" in operations or "[UPSERT]" in operations or "created" in operations.lower() or "updated" in operations.lower()


def _verify_curate_write(curate_output: str, *, cwd: str) -> dict:
    """Verify that a brv curate command produced a durable completed write."""
    log_id = _extract_curate_log_id(curate_output)
    if not log_id:
        return {
            "verified": False,
            "log_id": None,
            "detail": "brv curate output did not include a log id; durable write not verified",
        }

    detail = _run_brv(["curate", "view", log_id], timeout=30, cwd=cwd)
    if not detail.get("success"):
        return {
            "verified": False,
            "log_id": log_id,
            "detail": detail.get("error", "curate verification failed"),
        }

    detail_output = detail.get("output", "")
    return {
        "verified": _curate_detail_has_write(detail_output),
        "log_id": log_id,
        "detail": detail_output[:4000],
    }


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

QUERY_SCHEMA = {
    "name": "brv_query",
    "description": (
        "Search ByteRover's persistent knowledge tree for relevant context. "
        "Returns memories, project knowledge, architectural decisions, and "
        "patterns from previous sessions. Use for any question where past "
        "context would help."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
        },
        "required": ["query"],
    },
}

CURATE_SCHEMA = {
    "name": "brv_curate",
    "description": (
        "Store important information in ByteRover's persistent knowledge tree. "
        "Use for architectural decisions, bug fixes, user preferences, project "
        "patterns — anything worth remembering across sessions. ByteRover's LLM "
        "automatically categorizes and organizes the memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The information to remember."},
        },
        "required": ["content"],
    },
}

STATUS_SCHEMA = {
    "name": "brv_status",
    "description": "Check ByteRover status — CLI version, context tree stats, cloud sync state.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class ByteRoverMemoryProvider(MemoryProvider):
    """ByteRover persistent memory via the brv CLI."""

    def __init__(self):
        self._cwd = ""
        self._session_id = ""
        self._turn_count = 0
        self._sync_thread: Optional[threading.Thread] = None

    @property
    def name(self) -> str:
        return "byterover"

    def is_available(self) -> bool:
        """Check if brv CLI is installed. No network calls."""
        return _resolve_brv_path() is not None

    def get_config_schema(self):
        return [
            {
                "key": "api_key",
                "description": "ByteRover API key (optional, for cloud sync)",
                "secret": True,
                "env_var": "BRV_API_KEY",
                "url": "https://app.byterover.dev",
            },
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        self._cwd = str(_get_brv_cwd())
        self._session_id = session_id
        self._turn_count = 0
        Path(self._cwd).mkdir(parents=True, exist_ok=True)

    def system_prompt_block(self) -> str:
        if not _resolve_brv_path():
            return ""
        return (
            "# ByteRover Memory\n"
            "Active. Persistent knowledge tree with hierarchical context.\n"
            "Use brv_query to search past knowledge, brv_curate to store "
            "important facts, brv_status to check state."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Run brv query synchronously before the agent's first LLM call.

        Blocks until the query completes (up to _QUERY_TIMEOUT seconds), ensuring
        the result is available as context before the model is called.
        """
        if not query or len(query.strip()) < _MIN_QUERY_LEN:
            return ""
        result = _run_brv(
            ["query", "--", query.strip()[:5000]],
            timeout=_QUERY_TIMEOUT, cwd=self._cwd,
        )
        if result["success"] and result.get("output"):
            output = result["output"].strip()
            if len(output) > _MIN_OUTPUT_LEN:
                return f"## ByteRover Context\n{output}"
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """No-op: prefetch() now runs synchronously at turn start."""
        pass

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Curate the conversation turn in background (non-blocking)."""
        self._turn_count += 1

        # Only curate substantive turns
        if len(user_content.strip()) < _MIN_QUERY_LEN:
            return

        def _sync():
            try:
                combined = f"User: {user_content[:2000]}\nAssistant: {assistant_content[:2000]}"
                _run_brv(
                    ["curate", "--", combined],
                    timeout=_CURATE_TIMEOUT, cwd=self._cwd,
                )
            except Exception as e:
                logger.debug("ByteRover sync failed: %s", e)

        # Wait for previous sync
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(
            target=_sync, daemon=True, name="brv-sync"
        )
        self._sync_thread.start()

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror built-in memory writes to ByteRover."""
        if action not in {"add", "replace"} or not content:
            return

        def _write():
            try:
                label = "User profile" if target == "user" else "Agent memory"
                _run_brv(
                    ["curate", "--", f"[{label}] {content}"],
                    timeout=_CURATE_TIMEOUT, cwd=self._cwd,
                )
            except Exception as e:
                logger.debug("ByteRover memory mirror failed: %s", e)

        t = threading.Thread(target=_write, daemon=True, name="brv-memwrite")
        t.start()

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Extract insights before context compression discards turns."""
        if not messages:
            return ""

        # Build a summary of messages about to be compressed
        parts = []
        for msg in messages[-10:]:  # last 10 messages
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip() and role in {"user", "assistant"}:
                parts.append(f"{role}: {content[:500]}")

        if not parts:
            return ""

        combined = "\n".join(parts)

        def _flush():
            try:
                _run_brv(
                    ["curate", "--", f"[Pre-compression context]\n{combined}"],
                    timeout=_CURATE_TIMEOUT, cwd=self._cwd,
                )
                logger.info("ByteRover pre-compression flush: %d messages", len(parts))
            except Exception as e:
                logger.debug("ByteRover pre-compression flush failed: %s", e)

        t = threading.Thread(target=_flush, daemon=True, name="brv-flush")
        t.start()
        return ""

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [QUERY_SCHEMA, CURATE_SCHEMA, STATUS_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name == "brv_query":
            return self._tool_query(args)
        elif tool_name == "brv_curate":
            return self._tool_curate(args)
        elif tool_name == "brv_status":
            return self._tool_status()
        return tool_error(f"Unknown tool: {tool_name}")

    def shutdown(self) -> None:
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=10.0)

    # -- Tool implementations ------------------------------------------------

    def _tool_query(self, args: dict) -> str:
        query = args.get("query", "")
        if not query:
            return tool_error("query is required")

        result = _run_brv(
            ["query", "--", query.strip()[:5000]],
            timeout=_QUERY_TIMEOUT, cwd=self._cwd,
        )

        if not result["success"]:
            return tool_error(result.get("error", "Query failed"))

        output = result.get("output", "").strip()
        if not output or len(output) < _MIN_OUTPUT_LEN:
            return json.dumps({"result": "No relevant memories found."})

        # Truncate very long results
        if len(output) > 8000:
            output = output[:8000] + "\n\n[... truncated]"

        return json.dumps({"result": output})

    def _tool_curate(self, args: dict) -> str:
        content = args.get("content", "")
        if not content:
            return tool_error("content is required")

        result = _run_brv(
            ["curate", "--", content],
            timeout=_CURATE_TIMEOUT, cwd=self._cwd,
        )

        if not result["success"]:
            return tool_error(result.get("error", "Curate failed"))

        verification = _verify_curate_write(result.get("output", ""), cwd=self._cwd)
        if not verification.get("verified"):
            return json.dumps({
                "result": "ByteRover curate command completed, but durable write was not verified.",
                "verified": False,
                "log_id": verification.get("log_id"),
                "verification_detail": verification.get("detail", ""),
                "curate_output": result.get("output", "")[:4000],
                "curate_stderr": result.get("stderr", "")[:2000],
            })

        return json.dumps({
            "result": "Memory curated and verified.",
            "verified": True,
            "log_id": verification.get("log_id"),
            "verification_detail": verification.get("detail", ""),
        })

    def _tool_status(self) -> str:
        result = _run_brv(["status"], timeout=15, cwd=self._cwd)
        if not result["success"]:
            return tool_error(result.get("error", "Status check failed"))
        return json.dumps({"status": result.get("output", "")})


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register ByteRover as a memory provider plugin."""
    ctx.register_memory_provider(ByteRoverMemoryProvider())
