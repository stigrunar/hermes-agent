"""Local-only, privacy-safe execution-envelope telemetry.

The plugin deliberately consumes only a small allow-list of hook metadata. Raw
prompts, assistant responses, tool arguments/results, request/response bodies,
and error text are never retained. One JSONL record is appended after each
completed turn (``post_llm_call``).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "execution-envelope-telemetry/v1"
_OUTPUT_RELATIVE_PATH = Path("telemetry") / "execution-envelope.jsonl"
_NOT_EXPOSED = "existing plugin hooks do not expose this field"
_NOT_RELIABLE = "cannot be derived reliably without inspecting private tool payloads or repository state"
_LOCK = threading.RLock()


def available(value: Any) -> Dict[str, Any]:
    """Represent an observed value without conflating zero with missing."""
    return {"status": "available", "value": value}


def not_available(reason: str) -> Dict[str, str]:
    """Represent a field the runtime cannot observe."""
    return {"status": "not_available", "reason": reason}


def _bounded_metadata(value: Any, *, maximum: int = 256) -> Optional[str]:
    """Accept only short scalar metadata; never serialize arbitrary objects."""
    if value is None:
        return None
    if not isinstance(value, (str, int, float, bool)):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:maximum]


def _non_negative_int(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _first_int(mapping: Dict[str, Any], names: Iterable[str]) -> Optional[int]:
    for name in names:
        if name in mapping:
            value = _non_negative_int(mapping.get(name))
            if value is not None:
                return value
    return None


@dataclass
class _RunState:
    session_id: str = ""
    task_id: str = ""
    started_at: Optional[float] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    api_mode: Optional[str] = None
    iterations: int = 0
    tool_calls: int = 0
    commands_run: int = 0
    worker_count: int = 0
    usage_seen: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    seen_requests: set[str] = field(default_factory=set)


_STATES: Dict[Tuple[str, str], _RunState] = {}


def _state_key(kwargs: Dict[str, Any]) -> Tuple[str, str]:
    session_id = (
        _bounded_metadata(kwargs.get("session_id"))
        or _bounded_metadata(kwargs.get("parent_session_id"))
        or ""
    )
    task_id = _bounded_metadata(kwargs.get("task_id")) or ""
    return session_id, task_id


def _get_state(kwargs: Dict[str, Any]) -> _RunState:
    key = _state_key(kwargs)
    state = _STATES.get(key)
    if state is None and key[0] and not key[1]:
        session_matches = [
            candidate
            for candidate_key, candidate in _STATES.items()
            if candidate_key[0] == key[0]
        ]
        if len(session_matches) == 1:
            state = session_matches[0]
    if state is None:
        state = _RunState(session_id=key[0], task_id=key[1])
        _STATES[key] = state
    for attr in ("model", "provider", "api_mode"):
        value = _bounded_metadata(kwargs.get(attr))
        if value:
            setattr(state, attr, value)
    return state


def _observe_request(state: _RunState, kwargs: Dict[str, Any]) -> None:
    request_id = _bounded_metadata(kwargs.get("api_request_id"))
    request_key = request_id or f"anonymous:{state.iterations}"
    if request_key in state.seen_requests:
        return
    state.seen_requests.add(request_key)
    state.iterations += 1

    started_at = kwargs.get("started_at")
    if isinstance(started_at, (int, float)) and started_at >= 0:
        if state.started_at is None or started_at < state.started_at:
            state.started_at = float(started_at)
    elif state.started_at is None:
        state.started_at = time.time()


def _observe_usage(state: _RunState, usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    prompt = _first_int(usage, ("prompt_tokens", "input_tokens"))
    completion = _first_int(usage, ("completion_tokens", "output_tokens"))
    cache_read = _first_int(usage, ("cache_read_tokens", "cached_tokens"))
    cache_write = _first_int(usage, ("cache_write_tokens",))
    reasoning = _first_int(usage, ("reasoning_tokens",))
    total = _first_int(usage, ("total_tokens",))
    observed = [prompt, completion, cache_read, cache_write, reasoning, total]
    if all(value is None for value in observed):
        return

    state.usage_seen = True
    prompt_value = prompt or 0
    completion_value = completion or 0
    cached_value = (cache_read or 0) + (cache_write or 0)
    state.prompt_tokens += prompt_value
    state.completion_tokens += completion_value
    state.cached_tokens += cached_value
    state.reasoning_tokens += reasoning or 0
    state.total_tokens += total if total is not None else prompt_value + completion_value


def on_pre_llm_call(**kwargs: Any) -> None:
    """Start the wall clock while ignoring raw turn/context payloads."""
    with _LOCK:
        state = _get_state(kwargs)
        if state.started_at is None:
            state.started_at = time.time()


def on_post_api_request(**kwargs: Any) -> None:
    """Observe safe request metadata and normalized token usage only."""
    with _LOCK:
        state = _get_state(kwargs)
        _observe_request(state, kwargs)
        _observe_usage(state, kwargs.get("usage"))


def on_api_request_error(**kwargs: Any) -> None:
    """Count failed API attempts without retaining error/request payloads."""
    with _LOCK:
        state = _get_state(kwargs)
        _observe_request(state, kwargs)


def on_post_tool_call(**kwargs: Any) -> None:
    """Count tools and terminal/code commands without retaining args/results."""
    with _LOCK:
        state = _get_state(kwargs)
        state.tool_calls += 1
        tool_name = _bounded_metadata(kwargs.get("tool_name"))
        if tool_name in {"terminal", "execute_code"}:
            state.commands_run += 1


def on_subagent_start(**kwargs: Any) -> None:
    """Count workers; ignore delegated prompts and context."""
    with _LOCK:
        state = _get_state(kwargs)
        state.worker_count += 1


def _observed(value: Any, reason: str = _NOT_EXPOSED) -> Dict[str, Any]:
    return available(value) if value is not None else not_available(reason)


def build_record(state: _RunState, *, ended_at: float) -> Dict[str, Any]:
    """Build the stable v1 record. Pure apart from reading ``state``."""
    wall_time = None
    if state.started_at is not None:
        wall_time = round(max(0.0, ended_at - state.started_at), 6)

    usage = (
        {
            "prompt_tokens": available(state.prompt_tokens),
            "completion_tokens": available(state.completion_tokens),
            "cached_tokens": available(state.cached_tokens),
            "reasoning_tokens": available(state.reasoning_tokens),
            "total_tokens": available(state.total_tokens),
        }
        if state.usage_seen
        else {
            name: not_available("provider response did not include normalized token usage")
            for name in (
                "prompt_tokens",
                "completion_tokens",
                "cached_tokens",
                "reasoning_tokens",
                "total_tokens",
            )
        }
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "session_id": _observed(state.session_id or None),
            "task_id": _observed(state.task_id or None),
            "quality_mode": not_available(_NOT_EXPOSED),
            "risk_tier": not_available(_NOT_EXPOSED),
        },
        "runtime": {
            "model": _observed(state.model),
            "provider": _observed(state.provider),
            "reasoning_effort": not_available(_NOT_EXPOSED),
            "api_mode": _observed(state.api_mode),
            "fallbacks": not_available(_NOT_EXPOSED),
            "enabled_toolsets": not_available(_NOT_EXPOSED),
            "skills_loaded": not_available(_NOT_EXPOSED),
        },
        "timing": {"wall_time_seconds": _observed(wall_time)},
        "usage": {
            "iterations": available(state.iterations),
            **usage,
        },
        "delivery": {
            "tool_calls": available(state.tool_calls),
            "commands_run": available(state.commands_run),
            "tests_run": not_available(_NOT_RELIABLE),
            "files_changed": not_available(_NOT_RELIABLE),
            "worker_count": available(state.worker_count),
            "handoff_count": not_available(_NOT_RELIABLE),
            "review_count": not_available(_NOT_RELIABLE),
            "acceptance": not_available(_NOT_EXPOSED),
            "rollback_events": not_available(_NOT_RELIABLE),
        },
    }


def serialize_record(record: Dict[str, Any]) -> str:
    """Canonical single-line JSON for deterministic fixtures and JSONL."""
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _default_writer(line: str) -> None:
    path = get_hermes_home() / _OUTPUT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def emit_completed_run(
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Pop and emit one completed run record.

    ``_clock`` and ``_writer`` are private test seams, intentionally ignored by
    hook callers. The writer receives canonical JSON without a trailing newline.
    """
    clock: Callable[[], float] = kwargs.pop("_clock", time.time)
    writer: Callable[[str], None] = kwargs.pop("_writer", _default_writer)
    with _LOCK:
        key = _state_key(kwargs)
        state = _STATES.pop(key, None)
        if state is None:
            state = _get_state(kwargs)
            _STATES.pop(key, None)
        ended_at = float(clock())
        if state.started_at is None:
            state.started_at = ended_at
        record = build_record(state, ended_at=ended_at)
        try:
            writer(serialize_record(record))
        except Exception as exc:  # telemetry must never fail the agent turn
            logger.warning("execution-envelope telemetry write failed: %s", exc)
            return None
        return record


def on_post_llm_call(**kwargs: Any) -> None:
    """Emit after a completed turn; raw turn payload fields are ignored."""
    emit_completed_run(**kwargs)


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("api_request_error", on_api_request_error)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("subagent_start", on_subagent_start)
