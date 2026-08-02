"""Turn-end guard for kanban workers.

Kanban workers must end with ``kanban_complete`` or ``kanban_block``. Models
(especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
terminal board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional


_TERMINAL_KANBAN_TOOLS = frozenset({"kanban_complete", "kanban_block"})

_DEFAULT_MAX_ATTEMPTS = 2
_TERMINALIZATION_CHECKPOINT_MAX_CALL = 40
_TERMINALIZATION_RESERVE_CALLS = 10
_TERMINALIZATION_CHECKPOINT_MARKER = "[System: Kanban terminalization checkpoint v1"


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


def kanban_terminalization_enabled() -> bool:
    """True only for an exact dispatcher-owned, non-goal Kanban run."""
    if os.environ.get("HERMES_KANBAN_GOAL_MODE") == "1":
        return False
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    run_id = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    try:
        return bool(task_id) and int(run_id) > 0
    except (TypeError, ValueError):
        return False


def accepted_kanban_terminal_intent() -> Optional[dict]:
    """Read the DB authority for this process's exact current Kanban run."""
    if not kanban_terminalization_enabled():
        return None
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    run_id = int(os.environ["HERMES_KANBAN_RUN_ID"])
    try:
        from hermes_cli import kanban_db

        conn = kanban_db.connect()
        try:
            return kanban_db.accepted_terminal_intent(conn, task_id, run_id)
        finally:
            conn.close()
    except Exception:
        return None


def terminalization_checkpoint_call(max_iterations: int) -> int:
    """Return the one-shot provider-call threshold that reserves closeout room."""
    return min(
        _TERMINALIZATION_CHECKPOINT_MAX_CALL,
        max(1, int(max_iterations) - _TERMINALIZATION_RESERVE_CALLS),
    )


def append_kanban_terminalization_checkpoint(
    messages: list[dict],
    *,
    api_call_count: int,
    max_iterations: int,
) -> bool:
    """Append one cache/alternation-safe closeout instruction to a tool tail."""
    if not kanban_terminalization_enabled():
        return False
    if api_call_count < terminalization_checkpoint_call(max_iterations):
        return False
    if not messages or messages[-1].get("role") != "tool":
        return False
    content = messages[-1].get("content", "")
    if _TERMINALIZATION_CHECKPOINT_MARKER in str(content):
        return False
    instruction = (
        "\n\n[System: Kanban terminalization checkpoint v1. Stop broad work now. "
        "Preserve all existing work and perform only deterministic closeout: "
        "verify every referenced artifact path and every stated expected hash. "
        "If all referenced evidence matches, call kanban_complete now. If one "
        "path is missing or one expected hash differs from the actual hash, do "
        "not overwrite, auto-approve, or invent precision; call kanban_block "
        "with exactly one concrete expected-vs-actual mismatch. Do not repeat "
        "setup, discovery, browser work, source reading, or the full test suite.]"
    )
    if isinstance(content, str):
        messages[-1]["content"] = content + instruction
    elif isinstance(content, list):
        messages[-1]["content"] = list(content) + [
            {"type": "text", "text": instruction}
        ]
    else:
        messages[-1]["content"] = str(content) + instruction
    return True


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS:
                    return True
        elif role == "tool":
            name = str(msg.get("name") or "")
            if name in _TERMINAL_KANBAN_TOOLS:
                return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    already completed/blocked, or nudge budget exhausted).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    if kanban_terminalization_enabled():
        if accepted_kanban_terminal_intent() is not None:
            return None
    elif session_called_kanban_terminal(messages):
        # Goal-mode and legacy task-only workers retain the old attempt-based
        # stop-guard behavior. Exact non-goal workers use the DB journal above.
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no "
        "`kanban_complete` / `kanban_block`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, OR `kanban_block(reason=...)` if you are blocked.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "accepted_kanban_terminal_intent",
    "append_kanban_terminalization_checkpoint",
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "kanban_terminalization_enabled",
    "session_called_kanban_terminal",
    "terminalization_checkpoint_call",
]
