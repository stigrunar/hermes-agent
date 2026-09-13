"""Kanban board watcher methods for GatewayRunner.

Background loops that subscribe to kanban boards, deliver notifications and
artifacts, and drive the multi-agent dispatcher. They use only ``self`` state,
so they live on a mixin ``GatewayRunner`` inherits. Per-tick work lives in
``kanban_watchers_notifier`` / ``kanban_watchers_dispatcher``; shared plumbing
in ``kanban_watchers_common``.
"""

from __future__ import annotations

import asyncio
import os
import time
from functools import partial
from pathlib import Path
from typing import Any, Optional

from gateway.kanban_watchers_common import (
    _acquire_singleton_lock,
    _kanban_dispatch_allowed,
    _profile_notifier_lock_path,
    _release_singleton_lock,
    _resolve_auto_decompose_settings,
    _gc_retention_days,
    _to_thread_process_service,
    logger,
)
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.kanban_watchers_owner import (
    GatewayKanbanOwnerMixin,
    _resolve_outcome_owner_wake_spec,
    _owner_wake_prompt,
)
from gateway.kanban_watchers_dispatcher import (
    _KanbanDispatcher,
    _log_spawn_results,
    _resolve_dispatcher_settings,
)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
_GC_INTERVAL_SECONDS = 3600.0
_HEALTH_WINDOW = 6


class _NotificationReceiptError(RuntimeError):
    """Telegram proof could not be validated or durably stored."""


def _verified_telegram_delivery(
    send_result: Any, requested_thread_id: Any,
) -> tuple[str, str]:
    """Validate bounded Telegram Message evidence for the requested target."""
    message_id = str(getattr(send_result, "message_id", "") or "").strip()
    if not message_id:
        raise RuntimeError("Telegram send succeeded without a message_id")
    raw = getattr(send_result, "raw_response", None)
    if not isinstance(raw, dict) or "message_thread_id" not in raw:
        raise RuntimeError("Telegram send returned no concrete thread evidence")
    returned_thread_ids = [raw["message_thread_id"]]
    per_message = raw.get("message_receipts")
    if per_message is not None:
        if not isinstance(per_message, list) or not per_message:
            raise RuntimeError("Telegram send returned invalid per-message evidence")
        if any(
            not isinstance(item, dict)
            or not str(item.get("message_id") or "").strip()
            or "message_thread_id" not in item
            for item in per_message
        ):
            raise RuntimeError("Telegram send returned invalid per-message evidence")
        if str(per_message[0]["message_id"]).strip() != message_id:
            raise RuntimeError("Telegram primary message evidence is inconsistent")
        returned_thread_ids = [item["message_thread_id"] for item in per_message]

    requested = str(requested_thread_id or "").strip()
    if requested in {"", "1"}:
        if any(value is not None for value in returned_thread_ids):
            raise RuntimeError(
                "Telegram General/root delivery returned unexpected topic evidence"
            )
        return message_id, "general_root"
    try:
        matches = all(int(value) == int(requested) for value in returned_thread_ids)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Telegram send returned invalid thread evidence") from exc
    if not matches:
        raise RuntimeError("Telegram send returned mismatched thread evidence")
    return message_id, "matched"


class _ReceiptAwareKanbanNotification(_KanbanNotification):
    """Live notifier delivery with durable Telegram target receipts."""

    async def _receipt_exists(self, event_id: int) -> bool:
        return bool(await _to_thread_process_service(
            self.runner._kanban_has_notification_receipt,
            self.sub,
            event_id,
            self.board_slug,
        ))

    async def _record_receipt(
        self, event_id: int, message_id: str, confirmation: str,
    ) -> None:
        await _to_thread_process_service(
            self.runner._kanban_record_notification_receipt,
            self.sub,
            event_id,
            message_id,
            confirmation,
            self.board_slug,
        )

    async def _send_pings(self) -> bool:
        """Persist Telegram proof before settling any claimed event cursor."""
        from gateway.platforms.base import SendResult

        for ev in self.d["events"]:
            msg = self.format_event(ev)
            if msg is None:
                continue
            if not self.is_push_adapter and self.wake_agent:
                logger.debug(
                    "kanban notifier: adapter %s has no push channel; skipping text ping for %s, "
                    "relying on wake self-post instead",
                    self.platform_str,
                    self.task_id,
                )
                continue
            if not self.send_passive:
                continue
            try:
                if self.platform_str == "telegram":
                    try:
                        already_receipted = await self._receipt_exists(ev.id)
                    except Exception as exc:
                        raise _NotificationReceiptError(
                            "Telegram notification receipt lookup failed"
                        ) from exc
                    if already_receipted:
                        logger.debug(
                            "kanban notifier: skipping already-receipted %s event for %s to "
                            "%s/%s on board %s",
                            ev.kind,
                            self.task_id,
                            self.platform_str,
                            self.sub["chat_id"],
                            self.board_slug,
                        )
                        self.clear_failures()
                        continue
                if ev.id <= self.sub.get("last_ping_event_id", 0):
                    continue

                delivery_metadata = self.sub.get("delivery_metadata")
                metadata: dict[str, Any] = (
                    dict(delivery_metadata)
                    if isinstance(delivery_metadata, dict)
                    else {}
                )
                if self.sub.get("thread_id") and not metadata.get("thread_id"):
                    metadata["thread_id"] = self.sub["thread_id"]
                send_result = await self.adapter.send(
                    self.sub["chat_id"], msg, metadata=metadata,
                )
                if getattr(send_result, "success", True) is False:
                    raise RuntimeError(
                        "adapter send() reported failure: "
                        f"{getattr(send_result, 'error', None) or 'unknown error'}"
                    )
                if self.platform_str == "telegram" and isinstance(send_result, SendResult):
                    try:
                        message_id, confirmation = _verified_telegram_delivery(
                            send_result, self.sub.get("thread_id") or "",
                        )
                        await self._record_receipt(ev.id, message_id, confirmation)
                    except Exception as exc:
                        raise _NotificationReceiptError(
                            "Telegram notification receipt validation or persistence failed"
                        ) from exc

                await _to_thread_process_service(partial(
                    self.runner._kanban_sub_op,
                    self.board_slug,
                    "record_notify_ping",
                    self.sub,
                    event_id=ev.id,
                ))
                logger.debug(
                    "kanban notifier: delivered %s event for %s to %s/%s on board %s",
                    ev.kind,
                    self.task_id,
                    self.platform_str,
                    self.sub["chat_id"],
                    self.board_slug,
                )
                if ev.kind == "completed":
                    try:
                        await self.runner._deliver_kanban_artifacts(
                            adapter=self.adapter,
                            chat_id=self.sub["chat_id"],
                            metadata=metadata,
                            event_payload=getattr(ev, "payload", None),
                            task=self.task,
                        )
                    except Exception as exc:
                        logger.debug(
                            "kanban notifier: artifact delivery for %s failed: %s",
                            self.task_id,
                            exc,
                        )
                self.clear_failures()
            except _NotificationReceiptError as exc:
                logger.warning(
                    "kanban notifier: receipt proof failed for %s on %s; rewinding claim: %s",
                    self.task_id,
                    self.platform_str,
                    exc.__cause__ or exc,
                )
                await self.rewind()
                return False
            except Exception as exc:
                await self.delivery_failed(
                    "kanban notifier: send failed for %s on %s (attempt %d/%d): %s",
                    (self.task_id, self.platform_str),
                    "kanban notifier: dropping subscription %s on %s after %d consecutive send failures",
                    exc,
                    False,
                )
                return False
        return True


# Preserve the live split-module notifier while specializing only this facade's
# delivery construction point.
_KanbanNotification = _ReceiptAwareKanbanNotification


class GatewayKanbanWatchersMixin(GatewayKanbanOwnerMixin):
    """Kanban watcher / notifier / dispatcher loops for GatewayRunner."""

    def _owns_kanban_dispatcher_lock(self) -> bool:
        return getattr(self, "_kanban_dispatcher_lock_handle", None) is not None

    def _release_kanban_dispatcher_lock(self) -> None:
        """Clear notifier-visible ownership before releasing the OS lock."""
        handle = getattr(self, "_kanban_dispatcher_lock_handle", None)
        self._kanban_dispatcher_lock_handle = None
        _release_singleton_lock(handle)

    async def _sleep_between_ticks(self, interval: float) -> None:
        """Sleep *interval* (floored to 1s) in 1s slices so stop() never waits a full interval."""
        interval = max(interval, 1.0)
        slept = 0.0
        while slept < interval and self._running:
            await asyncio.sleep(min(1.0, interval - slept))
            slept += 1.0

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Elect one profile-owned notifier loop and keep one polling flow."""
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return
        profile = self._active_profile_name()
        lock_path = _profile_notifier_lock_path(_kb.kanban_home(), profile)
        retry_delay = min(1.0, max(0.1, float(interval)))
        while getattr(self, "_running", False):
            handle, state = _acquire_singleton_lock(lock_path)
            if state == "held":
                try:
                    await self._kanban_notifier_owner_loop(interval=interval)
                finally:
                    _release_singleton_lock(handle)
                return
            if state == "unavailable":
                logger.warning(
                    "kanban notifier: profile %s lock unavailable; falling back to config-only ownership",
                    profile,
                )
                await self._kanban_notifier_owner_loop(interval=interval)
                return
            await asyncio.sleep(retry_delay)

    async def _kanban_notifier_owner_loop(self, interval: float = 5.0) -> None:
        """Poll ``kanban_notify_subs`` and deliver terminal events to users.

        Per subscription, claims ``task_events`` newer than the stored cursor
        (kinds in TERMINAL_KINDS), sends one message per event, then advances
        the cursor. The subscription is removed only when the task is
        ``archived``: ``done`` is reversible, so the cursor — not unsubscribing
        — is the dedup mechanism (unsub-on-terminal dropped users when the
        dispatcher respawned a crashed task). All SQLite work runs in a thread;
        one tick's failure never stops the next.
        """
        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        sub_fail_counts: dict[tuple, int] = getattr(self, "_kanban_sub_fail_counts", {})
        self._kanban_sub_fail_counts = sub_fail_counts
        notifier_profile = getattr(self, "_kanban_notifier_profile", None) or self._active_profile_name()
        self._kanban_notifier_profile = notifier_profile

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        # Stale done-sub GC: subs survive ``done``, so boards that never
        # archive would accumulate rows scanned every tick. One DELETE per
        # board, at startup (0 → first tick) and at most hourly.
        _gc_next_at = 0.0

        while self._running:
            try:
                _gc_due = time.monotonic() >= _gc_next_at
                _retention = 30
                if _gc_due:
                    _gc_next_at = time.monotonic() + _GC_INTERVAL_SECONDS
                    _retention = _gc_retention_days()

                deliveries = await asyncio.to_thread(partial(
                    _notifier_collect, self, _kb,
                    notifier_profile=notifier_profile, gc_due=_gc_due, gc_retention_days=_retention,
                ))
                # Resolve bound Outcome events after Kanban collection (the
                # root Outcomes store must not be opened from the collector's
                # SQLite worker).  Owner delivery is independent of the origin
                # adapter and therefore precedes the generic notification.
                for d in deliveries:
                    task = d.get("task")
                    owner_wakes = list(d.get("owner_wakes") or [])
                    if task is not None and getattr(task, "project_id", None) and getattr(task, "outcome_id", None):
                        for event in d.get("events") or []:
                            spec = await _to_thread_process_service(
                                _resolve_outcome_owner_wake_spec, d.get("board"), task, event,
                            )
                            if spec is not None:
                                owner_wakes.append(spec)
                    d["owner_wakes"] = owner_wakes
                    if any(spec.get("status") == "retry" for spec in owner_wakes):
                        sub = d.get("sub")
                        if sub is not None:
                            await _to_thread_process_service(
                                self._kanban_rewind, sub, d.get("cursor", 0), d.get("old_cursor", 0), d.get("board"),
                            )
                        continue
                    if owner_wakes:
                        await self._deliver_outcome_owner_wakes(owner_wakes)
                    await _KanbanNotification(
                        self, d, platform_cls=_Platform, sub_fail_counts=sub_fail_counts,
                    ).deliver()
                    if d.get("owner_replan") and d.get("sub") is not None:
                        await self._deliver_owner_replan(
                            d.get("board"), d["sub"], d.get("task"), d["owner_replan"],
                        )
                pending = await _to_thread_process_service(self._pending_outcome_owner_wakes, _kb)
                if pending:
                    await self._deliver_outcome_owner_wakes(pending)
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            await self._sleep_between_ticks(interval)

    def _kanban_sub_op(self, board: Optional[str], op: str, sub: dict, **extra: Any) -> None:
        """Sync helper (runs in to_thread): call ``kanban_db_notify.<op>`` for one subscription on its board."""
        from hermes_cli import kanban_db_connect as _kbc
        from hermes_cli import kanban_db_notify as _kbn
        conn = _kbc.connect(board=board)
        try:
            getattr(_kbn, op)(
                conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "", **extra,
            )
        finally:
            conn.close()

    def _kanban_advance(self, sub: dict, cursor: int, board: Optional[str] = None) -> None:
        self._kanban_sub_op(board, "advance_notify_cursor", sub, new_cursor=cursor)

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        self._kanban_sub_op(board, "remove_notify_sub", sub)

    def _kanban_has_notification_receipt(
        self, sub: dict, event_id: int, board: Optional[str] = None,
    ) -> bool:
        from hermes_cli import kanban_db as _kb

        conn = _kb.connect(board=board)
        try:
            return bool(_kb.list_notification_receipts(
                conn,
                task_id=sub["task_id"],
                event_id=event_id,
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
            ))
        finally:
            conn.close()

    def _kanban_record_notification_receipt(
        self,
        sub: dict,
        event_id: int,
        message_id: str,
        thread_confirmation: str,
        board: Optional[str] = None,
    ) -> None:
        from hermes_cli import kanban_db as _kb

        conn = _kb.connect(board=board)
        try:
            _kb.record_notification_receipt(
                conn,
                task_id=sub["task_id"],
                event_id=event_id,
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                message_id=message_id,
                thread_confirmation=thread_confirmation,
            )
        finally:
            conn.close()

    def _kanban_rewind(self, sub: dict, claimed_cursor: int, old_cursor: int, board: Optional[str] = None) -> None:
        """Undo a claimed notification cursor after send failure."""
        self._kanban_sub_op(board, "rewind_notify_cursor", sub, claimed_cursor=claimed_cursor, old_cursor=old_cursor)

    async def _deliver_kanban_artifacts(self, *, adapter, chat_id: str, metadata: dict, event_payload: Optional[dict], task) -> None:
        """Upload artifact files referenced by a completed kanban task.

        Sources, in priority order: ``event_payload['artifacts']``,
        ``event_payload['summary']``, then ``task.result`` (legacy). Paths are
        deduplicated, missing files are skipped (may be mentioned for
        reference only), and upload errors are logged, never raised.
        """
        raw_paths: list[str] = []
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                raw_paths += [item for item in raw if isinstance(item, str)]
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                raw_paths += adapter.extract_local_files(summary)[0]
        if task is not None and getattr(task, "result", None):
            raw_paths += adapter.extract_local_files(str(task.result))[0]
        candidates: list[str] = []
        for path in raw_paths:
            expanded = os.path.expanduser(path) if path else ""
            if expanded and expanded not in candidates and os.path.isfile(expanded):
                candidates.append(expanded)
        if not candidates:
            return

        from gateway.platforms.base import BasePlatformAdapter
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        if not candidates:
            return

        from urllib.parse import quote as _quote

        # Images ride one send_multiple_images call (batch uploads on Signal/Slack).
        image_paths = [p for p in candidates if Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if Path(p).suffix.lower() not in _IMAGE_EXTS]
        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(chat_id=chat_id, images=batch, metadata=metadata)
            except Exception as exc:
                logger.warning("kanban notifier: image batch upload failed: %s", exc)
        for path in other_paths:
            try:
                if Path(path).suffix.lower() in _VIDEO_EXTS:
                    await adapter.send_video(chat_id=chat_id, video_path=path, metadata=metadata)
                else:
                    await adapter.send_document(chat_id=chat_id, file_path=path, metadata=metadata)
            except Exception as exc:
                logger.warning("kanban notifier: artifact upload (%s) failed: %s", path, exc)

    def _kanban_dispatcher_boot(self) -> Optional[tuple]:
        """Resolve config, kanban_db and the singleton lock; None when the dispatcher must not run.

        Config is read once at boot (restart to apply), except the auto-decompose
        toggle which is re-read every tick. The env var is an escape hatch to
        disable without editing YAML.
        """
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return None
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return None
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return None
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not isinstance(kanban_cfg, dict):
            logger.error("kanban dispatcher: kanban config must be a mapping")
            return None
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info("kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false")
            return None
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return None

        # Resolve the immutable shared-root admission snapshot before the
        # singleton lock, settings side effects, or any board DB is opened.
        # Every board in this watcher receives the same prepared object.
        try:
            requested_spawn, requested_progress = _kb.resolve_dispatch_caps(cfg)
            requested_progress = _kb.resolve_max_in_progress(requested_progress)
            effective_config = _kb.prepare_dispatch_admission(
                cfg,
                max_spawn=requested_spawn,
                max_in_progress=requested_progress,
                max_in_progress_per_profile=kanban_cfg.get("max_in_progress_per_profile"),
            )
            max_spawn, max_in_progress = _kb.resolve_dispatch_caps(
                effective_config,
                max_spawn=requested_spawn,
                max_in_progress=requested_progress,
            )
            allowed_worker_profiles = _kb.resolve_worker_profile_admission(
                effective_config,
                max_spawn=max_spawn,
                max_in_progress=max_in_progress,
            )
        except (TypeError, ValueError) as exc:
            logger.error("kanban dispatcher: admission policy invalid: %s", exc)
            return None

        # Single-dispatcher backstop (see _acquire_singleton_lock). The lock
        # lives at the machine-global kanban root, so it serialises ALL gateways.
        self._kanban_dispatcher_lock_handle = None
        _lock_path = _kb.kanban_home() / "kanban" / ".dispatcher.lock"
        _lock_handle, _lock_state = _acquire_singleton_lock(_lock_path)
        if _lock_state == "contended":
            logger.info("kanban dispatcher: another gateway already holds the dispatcher "
                        "lock (%s); this gateway will NOT dispatch.", _lock_path)
            return None
        if _lock_state == "held":
            self._kanban_dispatcher_lock_handle = _lock_handle  # hold for process lifetime
            logger.info("kanban dispatcher: holding singleton dispatcher lock (%s)", _lock_path)
        else:
            logger.warning("kanban dispatcher: advisory lock unavailable at %s; proceeding "
                           "on config control alone.", _lock_path)
        return (
            _load_config,
            _kb,
            kanban_cfg,
            max_spawn,
            max_in_progress,
            allowed_worker_profiles,
            effective_config,
        )

    async def _kanban_dispatcher_watcher(self) -> None:
        """Embedded kanban dispatcher — one tick every `dispatch_interval_seconds`.

        Gated by `kanban.dispatch_in_gateway` (default True); when false the
        loop exits and an external `hermes kanban daemon` is expected. Each
        tick runs :func:`kanban_db_dispatch.dispatch_once` in a thread; one tick's
        failure never stops the next. Shutdown: ``self._running`` is checked
        between ticks and the in-flight ``to_thread`` returns on its own.
        """
        boot = self._kanban_dispatcher_boot()
        if boot is None:
            return
        (
            _load_config,
            _kb,
            kanban_cfg,
            max_spawn,
            max_in_progress,
            allowed_worker_profiles,
            effective_config,
        ) = boot
        settings = _resolve_dispatcher_settings(
            kanban_cfg,
            _kb,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            allowed_worker_profiles=allowed_worker_profiles,
            effective_config=effective_config,
        )
        interval = settings.interval

        # Initial delay so adapters are wired before workers spawn (matches the notifier).
        await asyncio.sleep(5)

        # Health telemetry (mirrors `_cmd_daemon`): warn when the ready queue
        # is non-empty but spawns are 0 for N consecutive ticks — usually a
        # broken PATH, missing venv, or credential loss.
        bad_ticks = 0
        last_warn_at = 0
        dispatcher = _KanbanDispatcher(_kb, settings)

        logger.info("kanban dispatcher: embedded in gateway (interval=%.1fs)", interval)
        while self._running:
            try:
                # Reap zombies before per-board work so a board DB failure
                # cannot block cleanup of unrelated workers.
                from hermes_cli import kanban_db_dispatch as _kbd
                pids = await _to_thread_process_service(_kbd.reap_worker_zombies)
                if pids:
                    logger.info("kanban dispatcher: reaped %d zombie worker(s), pids=%s", len(pids), pids)
            except Exception:
                logger.exception("kanban dispatcher: zombie reaper failed")

            try:
                # Emergency stop (`hermes pause`): no auto-decompose or
                # dispatch while paused; running workers finish naturally.
                if not _kanban_dispatch_allowed():
                    bad_ticks = 0
                else:
                    # Re-read the auto-decompose toggle live so disabling it
                    # takes effect on the next tick, not on restart.
                    _ad_enabled, _ad_per_tick = _resolve_auto_decompose_settings(_load_config)
                    # See #49638.
                    if _ad_enabled:
                        await _to_thread_process_service(dispatcher.auto_decompose_tick, _ad_per_tick)
                    results = await _to_thread_process_service(dispatcher.tick_once)
                    any_spawned = _log_spawn_results(results)
                    ready_pending = await _to_thread_process_service(dispatcher.ready_nonempty)
                    bad_ticks = bad_ticks + 1 if ready_pending and not any_spawned else 0
                now = int(time.time())
                if bad_ticks >= _HEALTH_WINDOW and now - last_warn_at >= 300:
                    logger.warning(
                        "kanban dispatcher stuck: ready queue non-empty for "
                        "%d consecutive ticks but 0 workers spawned. Check "
                        "profile health (venv, PATH, credentials) and "
                        "`hermes kanban list --status ready`.",
                        bad_ticks,
                    )
                    last_warn_at = now
            except asyncio.CancelledError:
                logger.debug("kanban dispatcher: cancelled")
                self._release_kanban_dispatcher_lock()
                raise
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            await self._sleep_between_ticks(interval)

        self._release_kanban_dispatcher_lock()


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Callable  # noqa: F401,E402
from contextvars import Context  # noqa: F401,E402
import logging  # noqa: F401,E402
import re  # noqa: F401,E402
import sqlite3  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    't': ('agent.i18n', 't'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
