"""Owner-bound Kanban wake helpers.

The normal notifier keeps ownership of polling, cursor settlement and passive
messages.  This module contains only the exact owner-route resolution and the
durable owner delivery helpers composed by :mod:`gateway.kanban_watchers`.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from gateway.kanban_watchers_common import _list_boards, _to_thread_process_service, logger


_OUTCOME_OWNER_WAKE_KINDS = frozenset({
    "completed", "blocked", "gave_up", "crashed", "timed_out",
    "iteration_exhausted", "review_requested", "changes_requested",
    "block_loop_detected",
})

_OWNER_WAKE_PROJECT_ID_RE = re.compile(r"^p_[0-9a-f]{8}$")
_OWNER_WAKE_OUTCOME_ID_RE = re.compile(r"^o_[0-9a-f]{8}$")
_OWNER_WAKE_REF_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/@+-]*$")


def _owner_wake_body_fields(body: Any) -> dict[str, str]:
    if not isinstance(body, str):
        return {}
    fields: dict[str, str] = {}
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace("-", "_")
        value = value.strip().strip('`"')
        if key and value and len(key) <= 80 and len(value) <= 1024:
            fields[key] = value
    return fields


def _owner_wake_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _owner_wake_first(mapping: Any, *keys: str) -> str:
    if not isinstance(mapping, dict):
        return ""
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _owner_wake_body_alias(mapping: Any, key: str, pattern: re.Pattern[str]) -> str:
    """Return a rendered-label value only when it has canonical machine shape."""
    value = _owner_wake_first(mapping, key)
    return value if pattern.fullmatch(value) else ""


def _owner_wake_explicit_lane(
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


def _owner_wake_lane(
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
            conn, project_id, outcome_id=outcome_id
        )
        if item.lane_kind == "control"
    ]
    if len(controls) == 1:
        return controls[0], ""
    if len(controls) > 1:
        return None, "exactly one bound control lane is required"
    if str(lane_id or "").strip() or str(topic_target or "").strip():
        return _owner_wake_explicit_lane(
            odb,
            conn,
            project_id=project_id,
            outcome_id=outcome_id,
            lane_id=lane_id,
            topic_target=topic_target,
        )
    return None, "exactly one bound control lane is required"


def _owner_replan_prompt(task: Any, replan: dict[str, Any]) -> str:
    """Build one bounded, artifact-grounded continuation for Dolly/default."""
    task_id = str(replan.get("task_id") or getattr(task, "id", ""))
    fingerprint = str(replan.get("fingerprint") or "")
    project = replan.get("project_id") or replan.get("project") or "unknown"
    outcome = replan.get("semantic_outcome") or replan.get("end_reason") or "unknown"
    return (
        "[HERMES OWNER REPLAN — one shot]\n"
        f"Project: {project} · board: {replan.get('board') or 'unknown'}\n"
        f"Terminal task: {task_id} · run: {replan.get('terminal_run_id')} · reason: {replan.get('end_reason')}\n"
        f"Contract/revision: {replan.get('contract_id')} / {replan.get('revision')}\n"
        f"Semantic outcome: {outcome} · action: {replan.get('action') or 'inspect preserved artifact'}\n"
        f"Source topic target: {replan.get('topic_target') or 'unknown'}\n"
        f"Required successor identity: continuation_of={task_id} · project_id={project} · topic_target={replan.get('topic_target') or 'unknown'} · fingerprint={fingerprint}\n"
        f"Preserved artifact: {replan.get('worktree') or 'unknown'} · branch: {replan.get('branch') or 'unknown'} · state: {replan.get('artifact_state') or 'unknown'}\n\n"
        "Inspect the terminal run, preserved worktree/diff, tests, commit and push evidence. "
        "Classify exactly one of: complete_candidate, useful_incomplete_patch, "
        "new_blocker_or_unusable. Materialize exactly one current revision and one next "
        "action or manual blocker in repo canon, preserving current dependencies and review gates. "
        "Do not unblock or retry the terminal revision; do not spawn the same worker, Architect, "
        "detached QA, a new root graph, merge, or deploy.\n"
        f"After the current-revision/next-action receipt is durable, add one Kanban comment to {task_id} "
        f"with this exact line: owner_replan_ack: {fingerprint}"
    )


async def _owner_deliver_wake(adapter: Any, *, text: str, session_id: str, source: Any = None) -> None:
    """Deliver through the native wake path and require adapter admission."""
    from gateway.wake import deliver_wake
    await deliver_wake(adapter, text=text, session_id=session_id, source=source)


def _owner_wake_prompt(spec: dict[str, Any]) -> str:
    """Build the bounded controller handoff for one terminal task event."""
    outcome = spec.get("outcome") or {}
    task = spec.get("task") or {}
    lane = spec.get("route") or {}
    scope = task.get("mutation_scope") or []
    if isinstance(scope, str):
        scope = [scope]
    scope_text = ", ".join(str(item) for item in scope) or "none declared"
    human_gate = bool(spec.get("human_gate"))
    lane_target = lane.get("target") or (
        f"{lane.get('platform') or 'unknown'}:{lane.get('chat_id') or 'unknown'}"
        + (f":{lane.get('thread_id')}" if lane.get("thread_id") else "")
    )
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
        f"Mutation repository/scope/base: {task.get('mutation_repository') or 'unknown'} / {scope_text} / {task.get('mutation_base_ref') or 'unknown'}",
        f"Task title: {task.get('title') or 'unknown'}",
    ]
    if task.get("topic_target"):
        lines.append(f"Task topic target: {task['topic_target']}")
    if human_gate:
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


def _resolve_outcome_owner_wake_spec(
    board: Optional[str], task: Any, event: Any,
) -> Optional[dict[str, Any]]:
    """Resolve a terminal event to its exact current Outcome control lane."""
    if event is None or getattr(event, "kind", "") not in _OUTCOME_OWNER_WAKE_KINDS:
        return None
    project_id = str(getattr(task, "project_id", None) or "").strip()
    outcome_id = str(getattr(task, "outcome_id", None) or "").strip()
    if not project_id or not outcome_id:
        return None
    try:
        from hermes_cli import outcomes_db as odb

        with odb.connect_closing() as conn:
            outcome = odb.get_outcome(conn, outcome_id, project_id=project_id)
            if outcome is None:
                spec = {
                    "status": "noop", "reason": "outcome_identity_unresolved",
                    "board": str(board or ""), "task_id": str(task.id),
                    "event_id": str(getattr(event, "id", "")), "event_kind": str(event.kind),
                    "project_id": project_id, "outcome_id": outcome_id, "outcome_revision": "unresolved",
                }
                odb.record_outcome_owner_wake_receipt(
                    conn, board=str(board or ""), task_id=str(task.id),
                    event_id=getattr(event, "id", ""), event_kind=str(event.kind),
                    project_id=project_id, outcome_id=outcome_id,
                    outcome_revision="unresolved", status="noop",
                    reason="outcome_identity_unresolved",
                )
                return spec

            revision = odb.outcome_owner_wake_revision(outcome)
            outcome_data = outcome.to_dict()
            outcome_data["outcome_revision"] = revision
            event_payload = event.payload if isinstance(event.payload, dict) else {}
            body_fields = _owner_wake_body_fields(getattr(task, "body", None))
            explicit_project = _owner_wake_first(event_payload, "project_id", "project")
            explicit_outcome = _owner_wake_first(event_payload, "outcome_id", "outcome")
            body_project = _owner_wake_first(body_fields, "project_id") or _owner_wake_body_alias(
                body_fields, "project", _OWNER_WAKE_PROJECT_ID_RE
            )
            body_outcome = _owner_wake_first(body_fields, "outcome_id") or _owner_wake_body_alias(
                body_fields, "outcome", _OWNER_WAKE_OUTCOME_ID_RE
            )
            explicit_revision = _owner_wake_first(event_payload, "outcome_revision", "revision")
            if (
                (explicit_project and explicit_project != project_id)
                or (explicit_outcome and explicit_outcome != outcome_id)
                or (body_project and body_project != project_id)
                or (body_outcome and body_outcome != outcome_id)
                or (explicit_revision and explicit_revision != revision)
            ):
                status, reason = "stale", "terminal event identity mismatches task binding"
            else:
                event_base = _owner_wake_first(event_payload, "current_base_ref", "base_ref", "mutation_base_ref")
                event_candidate = _owner_wake_first(event_payload, "current_candidate_ref", "candidate_ref", "candidate")
                body_base = _owner_wake_first(body_fields, "current_base_ref", "base_ref", "mutation_base_ref")
                body_candidate = _owner_wake_first(
                    body_fields, "current_candidate_ref", "candidate_ref"
                ) or _owner_wake_body_alias(body_fields, "candidate", _OWNER_WAKE_REF_RE)
                status, reason = "deliver", ""
                if _owner_wake_truthy(event_payload.get("superseded")) or _owner_wake_first(
                    event_payload, "superseded_by", "supersession_id"
                ) or _owner_wake_first(body_fields, "superseded_by", "supersession_id"):
                    status, reason = "stale", "terminal event is explicitly superseded"
                elif any((
                    event_base and event_base != str(outcome.current_base_ref or ""),
                    event_candidate and event_candidate != str(outcome.current_candidate_ref or ""),
                    body_base and body_base != str(outcome.current_base_ref or ""),
                    body_candidate and body_candidate != str(outcome.current_candidate_ref or ""),
                )):
                    status, reason = "stale", "terminal event candidate/base is not current"
                elif outcome.archived or str(outcome.state).lower() in {"superseded", "obsolete", "cancelled", "archived"}:
                    status, reason = "stale", "Outcome is archived or superseded"

            task_lane_id = getattr(task, "conversation_lane_id", None)
            task_topic_target = getattr(task, "topic_target", None)
            lane = None
            owner = str(outcome.visible_owner or "").strip()
            if status == "deliver" and not owner:
                status, reason = "noop", "Outcome.visible_owner is missing"
            elif status == "deliver":
                lane, route_error = _owner_wake_lane(
                    odb,
                    conn,
                    project_id=project_id,
                    outcome_id=outcome.id,
                    lane_id=task_lane_id,
                    topic_target=task_topic_target,
                )
                if route_error:
                    status, reason = "noop", route_error
            route: dict[str, Any] = {}
            if lane is not None:
                route = {
                    "lane_id": lane.id, "platform": lane.platform,
                    "chat_id": lane.chat_id, "thread_id": lane.thread_id or "",
                    "target": odb.conversation_lane_target(lane),
                    "lane_kind": lane.lane_kind, "profile": owner or "",
                }
            task_data = {
                "title": str(getattr(task, "title", "") or "")[:512],
                "parent_execution_id": getattr(task, "parent_execution_id", None),
                "mutation_repository": getattr(task, "mutation_repository", None),
                "mutation_scope": list(getattr(task, "mutation_scope", None) or []),
                "mutation_base_ref": getattr(task, "mutation_base_ref", None),
                "topic_target": task_topic_target,
                "conversation_lane_id": task_lane_id,
            }
            human_gate = any(
                _owner_wake_truthy(event_payload.get(key)) or _owner_wake_truthy(body_fields.get(key))
                for key in (
                    "needs_user_decision", "manual_only", "human_gate", "requires_human",
                    "manual_decision", "needs_manual_decision", "release_gate", "merge_gate",
                    "deploy_gate", "business_gate", "public_gate",
                )
            )
            spec = {
                "status": status, "reason": reason,
                "board": str(board or ""), "task_id": str(task.id),
                "event_id": str(getattr(event, "id", "")), "event_kind": str(event.kind),
                "project_id": project_id, "outcome_id": outcome.id, "outcome_revision": revision,
                "visible_owner": owner, "route": route, "outcome": outcome_data,
                "task": task_data, "human_gate": human_gate,
            }
            if status == "deliver":
                spec["prompt"] = _owner_wake_prompt(spec)
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
        return {
            "status": "retry",
            "reason": "Outcome identity could not be read; retry before advancing the event cursor",
            "board": str(board or ""), "task_id": str(getattr(task, "id", "")),
            "event_id": str(getattr(event, "id", "")), "event_kind": str(getattr(event, "kind", "")),
            "project_id": project_id, "outcome_id": outcome_id, "outcome_revision": "unresolved",
        }


class GatewayKanbanOwnerMixin:
    """Durable owner-wake and terminal owner-replan delivery methods."""

    def _kanban_claim_outcome_owner_wake(self, spec: dict[str, Any]) -> Optional[dict[str, Any]]:
        from hermes_cli import outcomes_db as odb
        conn = odb.connect()
        try:
            return odb.claim_outcome_owner_wake(
                conn, board=spec.get("board") or "default", task_id=spec.get("task_id") or "",
                event_id=spec.get("event_id") or "", event_kind=spec.get("event_kind") or "",
                project_id=spec.get("project_id") or "", outcome_id=spec.get("outcome_id") or "",
                outcome_revision=spec.get("outcome_revision") or "", payload=spec,
            )
        finally:
            conn.close()

    def _kanban_outcome_owner_wake_is_current(self, spec: dict[str, Any]) -> bool:
        from hermes_cli import outcomes_db as odb
        conn = odb.connect()
        try:
            outcome = odb.get_outcome(conn, spec.get("outcome_id") or "", project_id=spec.get("project_id") or "")
            if outcome is None or odb.outcome_owner_wake_revision(outcome) != str(spec.get("outcome_revision") or ""):
                return False
            route = spec.get("route") or {}
            task = spec.get("task") or {}
            lane_id = task.get("conversation_lane_id")
            topic_target = task.get("topic_target")
            lane, route_error = _owner_wake_lane(
                odb,
                conn,
                project_id=outcome.project_id,
                outcome_id=outcome.id,
                lane_id=lane_id,
                topic_target=topic_target,
            )
            if route_error:
                return False
            if lane is None:
                return False
            return (
                str(outcome.visible_owner or "").strip() == str(route.get("profile") or "").strip()
                and lane.id == str(route.get("lane_id") or "")
                and lane.platform == str(route.get("platform") or "").strip().lower()
                and lane.chat_id == str(route.get("chat_id") or "").strip()
                and (lane.thread_id or "") == str(route.get("thread_id") or "")
                and odb.conversation_lane_target(lane) == str(route.get("target") or "").strip()
            )
        finally:
            conn.close()

    def _kanban_outcome_owner_wake_receipt(self, claim_key: str, delivered: bool, error: Optional[str] = None) -> None:
        from hermes_cli import outcomes_db as odb
        conn = odb.connect()
        try:
            if delivered:
                odb.mark_outcome_owner_wake_delivered(conn, claim_key)
            else:
                odb.mark_outcome_owner_wake_failed(conn, claim_key, error=error)
        finally:
            conn.close()

    def _kanban_outcome_owner_wake_stale(self, claim_key: str) -> None:
        from hermes_cli import outcomes_db as odb
        conn = odb.connect()
        try:
            odb.mark_outcome_owner_wake_stale(conn, claim_key, error="Outcome revision or control lane is stale")
        finally:
            conn.close()

    async def _deliver_outcome_owner_wakes(self, specs: list[dict[str, Any]]) -> None:
        from gateway.config import Platform
        from gateway.wake import adapter_supports_push

        for spec in specs:
            if spec.get("status") != "deliver":
                continue
            claimed = await _to_thread_process_service(self._kanban_claim_outcome_owner_wake, spec)
            if claimed is None:
                continue
            claim_key = str(claimed.get("claim_key") or "")
            try:
                if not self._kanban_outcome_owner_wake_is_current(spec):
                    await _to_thread_process_service(self._kanban_outcome_owner_wake_stale, claim_key)
                    continue
                payload = claimed.get("payload") or {}
                route = payload.get("route") or spec.get("route") or {}
                platform = Platform(str(route.get("platform") or "").strip().lower())
                profile = str(route.get("profile") or payload.get("visible_owner") or "").strip()
                adapter = self._authorization_adapter(platform, profile or None)
                if adapter is None:
                    raise RuntimeError(f"owner adapter unavailable for profile {profile or 'unassigned'}")
                text = str(payload.get("prompt") or spec.get("prompt") or "").strip()
                if not text:
                    raise RuntimeError("owner wake prompt missing")
                chat_id = str(route.get("chat_id") or "").strip()
                thread_id = str(route.get("thread_id") or "").strip() or None
                if adapter_supports_push(adapter):
                    from gateway.session import SessionSource
                    source = SessionSource(
                        platform=platform, chat_id=chat_id,
                        chat_type=str(route.get("chat_type") or "group"), thread_id=thread_id,
                        user_id=route.get("user_id"), profile=profile or None,
                    )
                    resolver = getattr(self, "_session_key_for_source", None)
                    session_key = resolver(source) if callable(resolver) else ""
                    await _owner_deliver_wake(adapter, text=text, session_id=session_key, source=source)
                else:
                    await _owner_deliver_wake(adapter, text=text, session_id=chat_id)
                await _to_thread_process_service(self._kanban_outcome_owner_wake_receipt, claim_key, True)
            except Exception as exc:
                await _to_thread_process_service(self._kanban_outcome_owner_wake_receipt, claim_key, False, str(exc))
                logger.warning("kanban notifier: Outcome owner wake failed task=%s event=%s: %s", spec.get("task_id"), spec.get("event_id"), exc)

    def _kanban_claim_owner_replan(self, board: Optional[str], sub: dict) -> Optional[dict[str, Any]]:
        from hermes_cli import kanban_db as kb
        conn = kb.connect(board=board)
        try:
            return kb.claim_owner_replan_for_route(
                conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "", claim=True,
            )
        finally:
            conn.close()

    def _kanban_owner_replan_outcome(self, board: Optional[str], task_id: str, fingerprint: str, replan_event_id: int, error: Optional[str]) -> None:
        from hermes_cli import kanban_db as kb
        conn = kb.connect(board=board)
        try:
            if error is None:
                kb.mark_owner_replan_delivered(conn, task_id, fingerprint=fingerprint, replan_event_id=replan_event_id)
            else:
                kb.mark_owner_replan_failed(conn, task_id, fingerprint=fingerprint, replan_event_id=replan_event_id, error=error)
        finally:
            conn.close()

    async def _deliver_owner_replan(self, board: Optional[str], sub: dict, task: Any, replan: dict[str, Any]) -> None:
        from gateway.config import Platform
        from gateway.wake import adapter_supports_push

        fingerprint = str(replan.get("fingerprint") or "")
        event_id = int(replan.get("replan_event_id") or 0)
        if replan.get("interrupted_claim"):
            await _to_thread_process_service(
                self._kanban_owner_replan_outcome, board, sub["task_id"], fingerprint, event_id,
                "owner wake claim was interrupted before a delivery receipt",
            )
            return
        claimed = await _to_thread_process_service(self._kanban_claim_owner_replan, board, sub)
        if claimed is None:
            return
        fingerprint = str(claimed.get("fingerprint") or fingerprint)
        event_id = int(claimed.get("replan_event_id") or event_id)
        route = claimed.get("route") or {}
        try:
            platform = Platform(str(route.get("platform") or "").strip().lower())
            profile = str(route.get("notifier_profile") or "default").strip() or "default"
            adapter = self._authorization_adapter(platform, profile)
            if adapter is None:
                raise RuntimeError(f"owner adapter unavailable for profile {profile}")
            prompt = _owner_replan_prompt(task, claimed)
            chat_id = str(route.get("chat_id") or sub.get("chat_id") or "").strip()
            thread_id = str(route.get("thread_id") or sub.get("thread_id") or "").strip() or None
            if adapter_supports_push(adapter):
                from gateway.session import SessionSource
                source = SessionSource(
                    platform=platform, chat_id=chat_id, chat_type=str(route.get("chat_type") or sub.get("chat_type") or "group"),
                    thread_id=thread_id, user_id=route.get("user_id") or sub.get("user_id"), profile=profile,
                )
                resolver = getattr(self, "_session_key_for_source", None)
                session_key = resolver(source) if callable(resolver) else str(route.get("session_key") or chat_id)
                await _owner_deliver_wake(adapter, text=prompt, session_id=session_key, source=source)
            else:
                await _owner_deliver_wake(adapter, text=prompt, session_id=str(route.get("session_key") or chat_id))
            await _to_thread_process_service(self._kanban_owner_replan_outcome, board, sub["task_id"], fingerprint, event_id, None)
        except Exception as exc:
            await _to_thread_process_service(self._kanban_owner_replan_outcome, board, sub["task_id"], fingerprint, event_id, str(exc))

    def _pending_outcome_owner_wakes(self, kb: Any) -> list[dict[str, Any]]:
        """Read retryable owner rows for restart recovery without touching Kanban state."""
        try:
            from hermes_constants import get_default_hermes_root
            if not (get_default_hermes_root() / "outcomes.db").exists():
                return []
            from hermes_cli import outcomes_db as odb
            specs: list[dict[str, Any]] = []
            for board_meta in _list_boards(kb):
                board = board_meta.get("slug") or kb.DEFAULT_BOARD
                with odb.connect_closing() as conn:
                    rows = odb.list_outcome_owner_wakes(conn, board=board, statuses=odb.OWNER_WAKE_RETRYABLE_STATES)
                for row in rows:
                    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                    if not payload.get("route") or not payload.get("prompt"):
                        continue
                    spec = dict(payload)
                    spec.update({
                        "status": "deliver", "claim_key": row["claim_key"], "board": row["board"],
                        "task_id": row["task_id"], "event_id": row["event_id"], "event_kind": row["event_kind"],
                        "project_id": row["project_id"], "outcome_id": row["outcome_id"], "outcome_revision": row["outcome_revision"],
                    })
                    specs.append(spec)
            return specs
        except Exception as exc:
            logger.debug("kanban owner wake pending scan failed: %s", exc)
            return []
