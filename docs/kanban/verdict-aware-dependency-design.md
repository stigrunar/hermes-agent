# Verdict-aware dependency links — bounded design note

Hermes v1 `task_links` are status-only parent → child edges. A child is
eligible for promotion when every linked parent is `done` or `archived`; the
link does not carry a review/QA verdict or an accepted-verdict policy.

The current diagnostics are therefore deliberately read-only. They surface a
review-required blocked parent that keeps a child in `todo`, and a terminal
parent whose structured negative run verdict has nevertheless released an
active child. This preserves v1 routing and existing data/API contracts while
making the ambiguity visible to operators.

A future verdict-aware contract should be explicit at the link boundary, for
example with `gate_type` plus an `accepted_verdicts` field on the link model,
schema, and create/link APIs. Promotion would then evaluate the parent’s
structured verdict against that policy, with a bounded migration for existing
status-only links. That contract is intentionally not implemented here.
