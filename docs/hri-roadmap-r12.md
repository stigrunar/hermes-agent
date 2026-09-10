# HRI R12 roadmap binding

R12 keeps roadmap admission as an additive Kanban task preflight. A task may
persist a canonical `execution_preflight` snapshot; mutation tasks linked to a
schema-v2 workstream register must carry its validated roadmap binding.

Before a warm `ready` claim, Kanban re-reads the linked project register,
fetches the declared canonical remote/ref, resolves the tracking commit, and
runs the repository-owned admission validator with a mode-0600 temporary
binding file. The temporary file is removed on every path. Validator
acceptance, rather than a moved remote ref alone, determines admissibility.

Any required-binding drift blocks the task atomically with
`block_kind=roadmap_binding_drift`, disables retry, and emits one durable
`needs_owner_replan` intent when the existing route is eligible. Explicit
owner unblock is the resume seam and repeats warm validation. Legacy unbound
read-only work remains compatible.

Bound worktrees start at the binding's exact 40-character `base_commit` and
existing worktrees/branches must descend from that commit. Unbound tasks retain
the existing `HEAD` behavior and absolute-path protections.

## Owner validation — changes required

Native R12 `ex_9c33e678` finished normally in 990.284s. The worker reported
43 passed / 1 explicitly deferred review-tool test and green lease probe,
compilation, Ruff and whitespace checks. Owner verified the working Python
blobs against the receipt and inspected all three production diffs.

The owner unfiltered 14-file preservation run reports **280 passed, 1 failed**,
exit 1, 68.6s (`r12-owner-preservation.log`). Its only failed pytest case is the
already deferred review-tool candidate contract. This is NOT slice acceptance:
two independent temporary-store/worktree falsifiers also failed:

- B4: canonical task path occupied by an unrelated other branch reaches the
  legacy fallback and returns that unrelated history. The exact accepted local
  reference also fails this case; it is a pre-existing gap against the explicit
  R12 all-path exact-base criterion, not a newly introduced regression.
- B3: row-level `hygiene_class=obsolete` and `superseded_by` are ignored by the
  ported owner-intent helper; both emit an intent. Exact accepted reference
  suppresses both. The related successor helper also dropped row-field reads.

Evidence in the existing HRI exports directory: `r12-owner-falsifiers.json`,
`r12-owner-worktree-probe.py`, `r12-owner-intent-probe.py`, and corresponding
candidate/accepted logs. Neither probe touches live state or spawns workers.
Owner verdict: **CHANGES_REQUIRED**. Preserve the partial patch and correct
these two frozen criteria under a new bounded revision, not a terminal retry.
The review-tool/gateway ports, migration checks, independent full candidate
review and release/rollback/live acceptance still remain. No source push,
integration, restart, activation, credential or runtime change occurred.
