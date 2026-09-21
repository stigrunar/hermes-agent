"""Durable terminal-task wakeups for the exact owner of a bound Outcome."""

from __future__ import annotations

from typing import Any, Optional

from gateway.kanban_watchers_common import _to_thread_process_service, logger

OUTCOME_OWNER_WAKE_KINDS = frozenset({
    "completed", "blocked", "gave_up", "crashed", "timed_out",
    "iteration_exhausted", "review_requested", "changes_requested",
    "block_loop_detected",
})


def _body_fields(body: Any) -> dict[str, str]:
    if not isinstance(body, str):
        return {}
    fields = {}
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace("-", "_")
        value = value.strip().strip("`\"")
        if key and value and len(key) <= 80 and len(value) <= 1024:
            fields[key] = value
    return fields


def _truthy(value: Any) -> bool:
    return isinstance(value, bool) and value or str(value or "").strip().lower() in {
        "1", "true", "yes", "y", "on",
    }


def _first(mapping: Any, *keys: str) -> str:
    if not isinstance(mapping, dict):
        return ""
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _explicit_lane(
    odb: Any,
    conn: Any,
    *,
    project_id: str,
    outcome_id: str,
    lane_id: Any,
    topic_target: Any,
) -> tuple[Optional[Any], str]:
    """Resolve an explicit task lane without falling back to another route."""
    lane_token = str(lane_id or "").strip()
    target = str(topic_target or "").strip()
    if not lane_token or not target:
        return None, "explicit conversation lane and topic target are both required"
    lane = next(
        (
            item
            for item in odb.list_conversation_lanes(conn, project_id)
            if item.id == lane_token
        ),
        None,
    )
    if lane is None:
        return None, "explicit conversation lane does not belong to task project"
    if lane.outcome_id not in {None, outcome_id}:
        return None, "explicit conversation lane belongs to another Outcome"
    if odb.conversation_lane_target(lane) != target:
        return None, "explicit conversation lane target does not match task topic target"
    return lane, ""


def _owner_lane(
    odb: Any,
    conn: Any,
    *,
    project_id: str,
    outcome_id: str,
    lane_id: Any,
    topic_target: Any,
) -> tuple[Optional[Any], str]:
    """Prefer the unique Outcome control lane, then an exact task lane."""
    controls = [
        item
        for item in odb.list_conversation_lanes(
            conn, project_id, outcome_id=outcome_id,
        )
        if item.lane_kind == "control"
    ]
    if len(controls) == 1:
        return controls[0], ""
    if len(controls) > 1:
        return None, "exactly one bound control lane is required"
    if str(lane_id or "").strip() or str(topic_target or "").strip():
        return _explicit_lane(
            odb,
            conn,
            project_id=project_id,
            outcome_id=outcome_id,
            lane_id=lane_id,
            topic_target=topic_target,
        )
    return None, "exactly one bound control lane is required"


def owner_wake_prompt(spec: dict[str, Any]) -> str:
    outcome, task, lane = spec.get("outcome") or {}, spec.get("task") or {}, spec.get("route") or {}
    scope = task.get("mutation_scope") or []
    if isinstance(scope, str):
        scope = [scope]
    lane_target = lane.get("target") or f"{lane.get('platform') or 'unknown'}:{lane.get('chat_id') or 'unknown'}"
    lines = [
        "[HERMES OUTCOME OWNER WAKE — bounded controller receipt]",
        f"Project: {spec.get('project_id') or 'unknown'}",
        f"Outcome: {spec.get('outcome_id') or 'unknown'} ({outcome.get('outcome_key') or 'unknown'})",
        f"Control lane: {lane_target}",
        f"Lane ID: {lane.get('lane_id') or 'unknown'} · lane_kind={lane.get('lane_kind') or 'unknown'}",
        f"Board/task/event: {spec.get('board') or 'unknown'} / {spec.get('task_id') or 'unknown'} / {spec.get('event_id') or 'unknown'} ({spec.get('event_kind') or 'unknown'})",
        f"Visible owner: {spec.get('visible_owner') or 'unassigned'}",
        f"Current Outcome revision: {spec.get('outcome_revision') or 'unknown'}",
        f"Current candidate/base/live: {outcome.get('current_candidate_ref') or 'unknown'} / {outcome.get('current_base_ref') or 'unknown'} / {outcome.get('current_live_ref') or 'unknown'}",
        f"Task parent execution: {task.get('parent_execution_id') or 'none'}",
        f"Mutation repository/scope/base: {task.get('mutation_repository') or 'unknown'} / {', '.join(str(item) for item in scope) or 'none declared'} / {task.get('mutation_base_ref') or 'unknown'}",
        f"Task title: {task.get('title') or 'unknown'}",
    ]
    if spec.get("human_gate"):
        lines.extend([
            "Human-gated boundary: persist one typed blocker/manual decision request in the current Outcome.",
            "Do not unblock, merge, deploy, write business data, or expose anything publicly.",
        ])
    else:
        lines.extend([
            "Read the current graph and consume at most one already-authorized next gate.",
            "Do not create a successor graph, retry a superseded task, infer deploy authority, or start a second execution.",
            "Deploy-complete is an owner live-readback boundary: record the actual live result and do not deploy again.",
        ])
    lines.append("Persist exactly one typed controller receipt before returning.")
    return "\n".join(lines)


def resolve_outcome_owner_wake_spec(
    board: Optional[str], task: Any, event: Any,
) -> Optional[dict[str, Any]]:
    if event is None or getattr(event, "kind", "") not in OUTCOME_OWNER_WAKE_KINDS:
        return None
    project_id = str(getattr(task, "project_id", None) or "").strip()
    outcome_id = str(getattr(task, "outcome_id", None) or "").strip()
    if not project_id or not outcome_id:
        return None
    from hermes_cli import outcomes_db as odb
    try:
        with odb.connect_closing() as conn:
            outcome = odb.get_outcome(conn, outcome_id, project_id=project_id)
            if outcome is None:
                return None
            revision = odb.outcome_owner_wake_revision(outcome)
            payload = event.payload if isinstance(event.payload, dict) else {}
            fields = _body_fields(getattr(task, "body", None))
            status, reason = "deliver", ""
            if any((
                _first(payload, "project_id", "project") not in ("", project_id),
                _first(payload, "outcome_id", "outcome") not in ("", outcome_id),
                _first(fields, "project_id", "project") not in ("", project_id),
                _first(fields, "outcome_id", "outcome") not in ("", outcome_id),
                _first(payload, "outcome_revision", "revision") not in ("", revision),
            )):
                status, reason = "stale", "terminal event identity mismatches task binding"
            event_base = _first(payload, "current_base_ref", "base_ref", "mutation_base_ref")
            event_candidate = _first(payload, "current_candidate_ref", "candidate_ref", "candidate")
            if status == "deliver" and (
                _truthy(payload.get("superseded"))
                or _first(payload, "superseded_by", "supersession_id")
                or (event_base and event_base != str(outcome.current_base_ref or ""))
                or (event_candidate and event_candidate != str(outcome.current_candidate_ref or ""))
                or outcome.archived
                or str(outcome.state).lower() in {"superseded", "obsolete", "cancelled", "archived"}
            ):
                status, reason = "stale", "terminal event candidate/base is not current"
            task_lane_id = getattr(task, "conversation_lane_id", None)
            task_topic_target = getattr(task, "topic_target", None)
            lane = None
            owner = str(outcome.visible_owner or "").strip()
            if status == "deliver" and not owner:
                status, reason = "noop", "Outcome.visible_owner is missing"
            elif status == "deliver":
                lane, route_error = _owner_lane(
                    odb,
                    conn,
                    project_id=project_id,
                    outcome_id=outcome.id,
                    lane_id=task_lane_id,
                    topic_target=task_topic_target,
                )
                if route_error:
                    status, reason = "noop", route_error
            route = {}
            if lane is not None:
                route = {
                    "lane_id": lane.id, "platform": lane.platform,
                    "chat_id": lane.chat_id, "thread_id": lane.thread_id or "",
                    "target": odb.conversation_lane_target(lane),
                    "lane_kind": lane.lane_kind, "profile": owner,
                }
            outcome_data = outcome.to_dict()
            outcome_data["outcome_revision"] = revision
            spec = {
                "status": status, "reason": reason, "board": str(board or ""),
                "task_id": str(task.id), "event_id": str(getattr(event, "id", "")),
                "event_kind": str(event.kind), "project_id": project_id,
                "outcome_id": outcome.id, "outcome_revision": revision,
                "visible_owner": owner, "route": route, "outcome": outcome_data,
                "task": {
                    "title": str(getattr(task, "title", "") or "")[:512],
                    "parent_execution_id": getattr(task, "parent_execution_id", None),
                    "mutation_repository": getattr(task, "mutation_repository", None),
                    "mutation_scope": list(getattr(task, "mutation_scope", None) or []),
                    "mutation_base_ref": getattr(task, "mutation_base_ref", None),
                    "topic_target": task_topic_target,
                    "conversation_lane_id": task_lane_id,
                },
                "human_gate": any(_truthy(payload.get(key)) or _truthy(fields.get(key)) for key in (
                    "needs_user_decision", "manual_only", "human_gate", "requires_human",
                    "manual_decision", "needs_manual_decision", "release_gate", "merge_gate",
                    "deploy_gate", "business_gate", "public_gate",
                )),
            }
            if status == "deliver":
                spec["prompt"] = owner_wake_prompt(spec)
            else:
                odb.record_outcome_owner_wake_receipt(
                    conn, board=str(board or ""), task_id=str(task.id),
                    event_id=getattr(event, "id", ""), event_kind=str(event.kind),
                    project_id=project_id, outcome_id=outcome.id,
                    outcome_revision=revision, status=status, reason=reason,
                )
            return spec
    except Exception as exc:
        logger.debug("kanban owner wake resolution failed for %s: %s", getattr(task, "id", ""), exc)
        return {"status": "retry", "reason": str(exc)}


def _claim(spec: dict[str, Any]) -> Optional[dict[str, Any]]:
    from hermes_cli import outcomes_db as odb
    with odb.connect_closing() as conn:
        return odb.claim_outcome_owner_wake(
            conn, board=spec.get("board") or "default", task_id=spec.get("task_id") or "",
            event_id=spec.get("event_id") or "", event_kind=spec.get("event_kind") or "",
            project_id=spec.get("project_id") or "", outcome_id=spec.get("outcome_id") or "",
            outcome_revision=spec.get("outcome_revision") or "", payload=dict(spec),
        )


def _is_current(spec: dict[str, Any]) -> bool:
    from hermes_cli import outcomes_db as odb
    with odb.connect_closing() as conn:
        outcome = odb.get_outcome(conn, spec.get("outcome_id") or "", project_id=spec.get("project_id") or "")
        if outcome is None or odb.outcome_owner_wake_revision(outcome) != str(spec.get("outcome_revision") or ""):
            return False
        route = spec.get("route") or {}
        task = spec.get("task") or {}
        lane, route_error = _owner_lane(
            odb,
            conn,
            project_id=outcome.project_id,
            outcome_id=outcome.id,
            lane_id=task.get("conversation_lane_id"),
            topic_target=task.get("topic_target"),
        )
        if route_error or lane is None:
            return False
        return (
            str(outcome.visible_owner or "").strip()
            == str(route.get("profile") or "").strip()
            and lane.id == str(route.get("lane_id") or "")
            and lane.platform == str(route.get("platform") or "").strip().lower()
            and lane.chat_id == str(route.get("chat_id") or "").strip()
            and (lane.thread_id or "") == str(route.get("thread_id") or "")
            and odb.conversation_lane_target(lane)
            == str(route.get("target") or "").strip()
        )


def _settle(claim_key: str, status: str, error: Optional[str] = None) -> None:
    from hermes_cli import outcomes_db as odb
    with odb.connect_closing() as conn:
        if status == "delivered":
            odb.mark_outcome_owner_wake_delivered(conn, claim_key)
        elif status == "stale":
            odb.mark_outcome_owner_wake_stale(conn, claim_key, error=error)
        else:
            odb.mark_outcome_owner_wake_failed(conn, claim_key, error=error)


async def deliver_outcome_owner_wakes(runner: Any, specs: list[dict[str, Any]]) -> None:
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.wake import deliver_wake

    for spec in specs:
        if spec.get("status") != "deliver":
            continue
        claimed = await _to_thread_process_service(_claim, spec)
        if claimed is None:
            continue
        claim_key = str(claimed.get("claim_key") or "")
        try:
            if not _is_current(spec):
                await _to_thread_process_service(_settle, claim_key, "stale", "Outcome revision or control lane is stale")
                continue
            route = claimed.get("payload", {}).get("route") or spec.get("route") or {}
            platform = Platform(str(route.get("platform") or "").strip().lower())
            profile = str(route.get("profile") or "").strip()
            adapter = runner._authorization_adapter(platform, profile or None)
            if adapter is None:
                raise RuntimeError(f"owner adapter unavailable for profile {profile or 'unassigned'}")
            source = SessionSource(
                platform=platform, chat_id=str(route.get("chat_id") or ""),
                chat_type=str(route.get("chat_type") or "group"),
                thread_id=str(route.get("thread_id") or "") or None, profile=profile or None,
            )
            resolver = getattr(runner, "_session_key_for_source", None)
            session_key = resolver(source) if callable(resolver) else ""
            await deliver_wake(
                adapter, text=str(claimed.get("payload", {}).get("prompt") or spec.get("prompt") or ""),
                session_id=session_key, source=source,
            )
            await _to_thread_process_service(_settle, claim_key, "delivered")
        except Exception as exc:
            await _to_thread_process_service(_settle, claim_key, "failed", str(exc))
            logger.warning("kanban notifier: Outcome owner wake failed; retrying: %s", exc)
