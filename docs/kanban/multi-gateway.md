# Multi-gateway deployment

Hermes supports multiple gateway processes running concurrently — one per profile
(default, writer, admin, coder, researcher). Each gateway opens its own connection
to platform APIs and delivers messages for its profile's subscribers.

## Independent dispatcher and notifier ownership

Only one gateway owns the kanban dispatcher. The dispatch-owning gateway keeps
`kanban.dispatch_in_gateway: true` (the default); every other gateway sets it
to `false`.

Notification ownership is separate and enabled by
`kanban.notify_in_gateway: true` (the default). Every gateway with a connected
default or multiplex-profile adapter is eligible for a machine-global notifier
lease, regardless of `kanban.dispatch_in_gateway`. The first eligible gateway
to acquire `<kanban-home>/kanban/.notifier.lock` polls all boards. Other
gateways keep retrying the lease but do **not** enumerate or open board DBs
while they are non-owners. Lock-unavailable gateways also fail closed. If the
owner loses all adapters or exits, it releases the lease and a connected
gateway can take over.

Subscriptions remain stamped with the profile that created them
(`notifier_profile`). The elected owner routes strictly through that profile's
adapter, preserving profile isolation; it never falls back to another
profile's adapter. The designated notifier owner is therefore expected to be a
multiplex gateway hosting every profile adapter needed by the adopted boards.
New subscriptions start from the task's current event position. Activating
notifications does not backfill or deliver terminal-event history that already
exists; events recorded after activation remain eligible across notifier
restart or temporary delivery failure.

## Configuration

Choose one notifier-capable gateway that hosts the adapters for every profile
whose subscriptions it must deliver. It may be the dispatch owner, or it may
be a notifier-only multiplex gateway:

```yaml
kanban:
  dispatch_in_gateway: false
  notify_in_gateway: true
```

On other profile gateways, opt out of notifier election as well as embedded
dispatch:

```yaml
kanban:
  dispatch_in_gateway: false
  notify_in_gateway: false
```

`notify_in_gateway` defaults to `true` for single-gateway compatibility. In a
multi-profile deployment, explicitly selecting the multiplex notifier avoids a
single-profile gateway winning the lease while lacking another profile's
adapter. The lock remains the exactly-one safety boundary among all gateways
that are intentionally eligible. `HERMES_KANBAN_DISPATCH_IN_GATEWAY=false`
remains the env override for dispatcher execution only.

## What each gateway does

| Gateway role | dispatch_in_gateway | Opens per-board DBs? | Runs dispatcher? | Runs notifier? |
|---|---|---|---|---|
| dispatch owner + notifier lease owner | true | yes | yes | yes |
| dispatcher-only gateway | true | yes, for dispatch | yes | waits for lease |
| notifier-only lease owner | false | yes | no | yes |
| connected non-owner | false | no | no | waits for lease |
| notifications disabled | any | no, for notifications | unchanged | no |

The notifier lease does not enable dispatch and the dispatcher flag does not
grant the notifier lease. Set `notify_in_gateway: false` on every gateway that
must not participate in notifier election.
