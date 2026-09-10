# HRI R17 owner-notification preservation

R17 restores the native owner-notification boundary on the decomposed Kanban
watcher. `gateway/kanban_watchers_owner.py` resolves exact Outcome control
lanes, claims and fences durable owner wakes, and delivers one-shot terminal
owner replans. The existing notifier remains the sole polling/collector flow;
passive origin messages, cursor settlement, adapter rechecks, profile
singleton locking, and artifact delivery stay in their native modules.

`hermes_cli/kanban_db.py` emits semantic completion intents and preserves the
existing `task_events` claim/control ledger. Terminal iteration exhaustion is
non-runnable and requires the default owner to materialize any continuation.

Proof is local-only: temp Kanban/Outcome stores, recording adapters, focused
gateway and terminal-replan tests, changed-file compilation, diff hygiene, and
the owner-provided exact scoped gate. No live gateway, network, model, service,
or release action is part of this packet.
