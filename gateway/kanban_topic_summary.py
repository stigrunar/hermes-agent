"""Read-only Kanban summaries for mapped Telegram forum topics."""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from typing import Any

from hermes_constants import get_default_hermes_root
from hermes_cli import kanban_db as kb


_ACTIVE_STATUSES = (
    "triage",
    "todo",
    "ready",
    "running",
    "blocked",
    "scheduled",
)
_STATUS_LABELS = {
    "triage": "Triagering",
    "todo": "Å gjøre",
    "ready": "Klar",
    "running": "Kjører",
    "blocked": "Blokkert",
    "scheduled": "Planlagt",
}
_TOPIC_MAP_FILENAME = "state/kanban_topic_writeback_map.json"
_FAIL_CLOSED = "Kanban-visning ikke tilgjengelig for dette emnet."


def _clean_display_text(value: Any) -> str:
    """Collapse whitespace so mapped/user-facing values stay one-line."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()


def _load_view(
    root: Path, target: str
) -> tuple[str, list[tuple[str, list[str] | None]]] | None:
    """Load and strictly validate one topic view from the read-only map."""
    path = root / _TOPIC_MAP_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None

    if not isinstance(raw, dict) or not isinstance(raw.get("topic_views"), dict):
        return None
    view = raw["topic_views"].get(target)
    if not isinstance(view, dict):
        return None

    label = _clean_display_text(view.get("label"))
    sources = view.get("sources")
    if not label or not isinstance(sources, list) or not sources:
        return None

    selectors: list[tuple[str, list[str] | None]] = []
    for source in sources:
        if not isinstance(source, dict):
            return None
        board = _clean_display_text(source.get("board"))
        if not board:
            return None
        if "tenants" not in source:
            tenants = None
        else:
            raw_tenants = source.get("tenants")
            if not isinstance(raw_tenants, list):
                return None
            tenants = []
            for tenant in raw_tenants:
                cleaned = _clean_display_text(tenant)
                if not cleaned:
                    return None
                tenants.append(cleaned)
        selectors.append((board, tenants))
    return label, selectors


def _task_title_key(title: str) -> str:
    return " ".join(title.split()).casefold()


def _render_tasks(label: str, tasks: list[tuple[str, kb.Task]]) -> str:
    """Render selected tasks without exposing board or task metadata."""
    grouped: OrderedDict[str, dict[str, Any]] = OrderedDict()
    status_rank = {status: index for index, status in enumerate(_ACTIVE_STATUSES)}
    for _board, task in tasks:
        title = _clean_display_text(task.title)
        key = _task_title_key(title)
        if not key:
            continue
        group = grouped.get(key)
        if group is None:
            group = {
                "title": title,
                "count": 0,
                "status": task.status,
                "assignees": set(),
            }
            grouped[key] = group
        elif status_rank.get(task.status, len(status_rank)) < status_rank.get(
            group["status"], len(status_rank)
        ):
            group["status"] = task.status
        group["count"] += 1
        assignee = _clean_display_text(task.assignee)
        if assignee:
            group["assignees"].add(assignee)

    ordered_groups = sorted(
        grouped.values(),
        key=lambda group: (
            status_rank.get(group["status"], len(status_rank)),
            group["title"].casefold(),
        ),
    )
    lines = [f"📌 {label}"]
    if not ordered_groups:
        return f"{lines[0]}: ingen aktive oppgaver."

    current_status = None
    for group in ordered_groups:
        status = group["status"]
        if status != current_status:
            current_status = status
            lines.append(f"{_STATUS_LABELS[status]}:")
        suffix = f" (×{group['count']})" if group["count"] > 1 else ""
        assignees = group["assignees"]
        assignee_suffix = f" — {next(iter(assignees))}" if len(assignees) == 1 else ""
        lines.append(f"• {group['title']}{suffix}{assignee_suffix}")
    return "\n".join(lines)


def render_mapped_topic_summary(*, chat_id: str, thread_id: str) -> str:
    """Render the exact mapped Telegram topic, failing closed on any defect."""
    target = f"telegram:{chat_id}:{thread_id}"
    loaded = _load_view(get_default_hermes_root(), target)
    if loaded is None:
        return _FAIL_CLOSED
    label, selectors = loaded

    try:
        available_boards = {
            str(board.get("slug"))
            for board in kb.list_boards(include_archived=False)
            if isinstance(board, dict) and isinstance(board.get("slug"), str)
        }
        selected_tasks: list[tuple[str, kb.Task]] = []
        seen_task_keys: set[tuple[str, str]] = set()
        for board, tenants in selectors:
            if board not in available_boards:
                return _FAIL_CLOSED
            conn = kb.connect(board=board)
            try:
                tasks = kb.list_tasks(conn, include_archived=False)
            finally:
                conn.close()
            for task in tasks:
                if task.status not in _ACTIVE_STATUSES:
                    continue
                if tenants is not None and task.tenant not in tenants:
                    continue
                task_key = (board, task.id)
                if task_key not in seen_task_keys:
                    seen_task_keys.add(task_key)
                    selected_tasks.append((board, task))
    except Exception:
        return _FAIL_CLOSED

    return _render_tasks(label, selected_tasks)
