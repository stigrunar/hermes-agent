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
default or multiplex-profile adapter is eligible for that profile's
machine-global notifier lease, regardless of `kanban.dispatch_in_gateway`.
Exactly one process per profile may acquire
`<kanban-home>/kanban/.notifier-<profile-digest>.lock` and poll that profile's
subscriptions across all boards. Other gateways keep retrying that profile's
lease but do **not** enumerate or open board DBs for it while they are
non-owners. Lock-unavailable gateways also fail closed. If an owner loses the
profile's adapter or exits, it releases the lease and another connected
gateway can take over.

Subscriptions remain stamped with the profile that created them
(`notifier_profile`). The elected per-profile owner routes strictly through
that profile's adapter, preserving profile isolation; it never falls back to
another profile's adapter. Legacy rows without a profile stamp are ambiguous
and are not claimed or delivered. A multiplex gateway may hold separate leases
for every profile adapter it hosts.
New subscriptions start from the task's current event position. Activating
notifications does not backfill or deliver terminal-event history that already
exists; events recorded after activation remain eligible across notifier
restart or temporary delivery failure.

## Configuration

Enable notification election on gateways that host the relevant profile
adapters. They may also own dispatch, or they may be notifier-only gateways:

```yaml
kanban:
  dispatch_in_gateway: false
  notify_in_gateway: true
```

On gateways that must not poll or deliver notifications, opt out of notifier
election as well as embedded dispatch:

```yaml
kanban:
  dispatch_in_gateway: false
  notify_in_gateway: false
```

`notify_in_gateway` defaults to `true` for single-gateway compatibility. The
per-profile lock remains the exactly-one safety boundary among all gateways
hosting that profile. `HERMES_KANBAN_DISPATCH_IN_GATEWAY=false` remains the env
override for dispatcher execution only.

## What each gateway does

| Gateway role | dispatch_in_gateway | Opens per-board DBs? | Runs dispatcher? | Runs notifier? |
|---|---|---|---|---|
| dispatch owner + profile lease owner | true | yes | yes | owned profile subscriptions |
| dispatcher-only gateway | true | yes, for dispatch | yes | waits for profile lease or disabled |
| notifier-only profile lease owner | false | yes, for subscribed boards | no | owned profile subscriptions |
| connected non-owner | false | no, for that profile | no | waits for profile lease |
| notifications disabled | any | no, for notifications | unchanged | no |

Notifier leases do not enable dispatch, and dispatcher ownership does not
grant a notifier lease or permit delivery of unstamped legacy subscriptions.
Set `notify_in_gateway: false` on every gateway that must not participate in
notifier election.
