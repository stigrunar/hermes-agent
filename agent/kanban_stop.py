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


_TERMINAL_KANBAN_TOOLS = frozenset({
    "kanban_complete",
    "kanban_block",
    "kanban_request_review",
})

_DEFAULT_MAX_ATTEMPTS = 2
_DEFAULT_BUDGET_RESERVE = 6


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
    if session_called_kanban_terminal(messages):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no terminal lifecycle call).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable only if it is required for the current acceptance.\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the implementation phase "
        "is done and downstream review/QA is already represented by child cards; call "
        "`kanban_request_review(summary=..., metadata=...)` if this same card is ready for "
        "review; otherwise call `kanban_block(reason=...)` with the exact remaining blocker.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


def build_kanban_budget_reserve_nudge(
    *,
    messages: Iterable[dict] | None = None,
    api_call_count: int,
    max_iterations: int,
    already_nudged: bool = False,
    reserve: int = _DEFAULT_BUDGET_RESERVE,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a one-shot nudge before a kanban worker consumes its final calls.

    The normal stop guard only fires after the model attempts a text response. A
    tool-hungry worker can therefore spend every iteration on reads/tests and hit
    the hard budget with no opportunity to write the terminal board receipt. This
    guard reserves a small tail of the budget for acceptance-linked closeout.
    """
    if not kanban_stop_nudge_enabled() or already_nudged:
        return None
    if session_called_kanban_terminal(messages):
        return None
    try:
        used = max(0, int(api_call_count))
        maximum = max(1, int(max_iterations))
        tail = max(1, int(reserve))
    except (TypeError, ValueError):
        return None
    if maximum < 10 or used < max(1, maximum - tail):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    remaining = max(0, maximum - used)
    return (
        "[System: Kanban iteration budget reserve is active. "
        f"Task `{tid}` has {remaining} model-call slot(s) left before the hard limit.\n\n"
        "Stop expanding scope now. Do not start a new broad suite, repeated browser matrix, "
        "new refactor, extra proof family, or fresh exploratory pass. Use the remaining calls "
        "only to decide the current acceptance from evidence already gathered, run at most one "
        "directly affected check if a decisive check is still missing, preserve the current "
        "candidate/checkpoint, and then immediately make exactly one lifecycle transition: "
        "`kanban_complete`, `kanban_request_review`, or `kanban_block`. A partial but preserved "
        "candidate with an exact blocker is better than consuming the budget without a receipt.]"
    )


__all__ = [
    "build_kanban_budget_reserve_nudge",
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
