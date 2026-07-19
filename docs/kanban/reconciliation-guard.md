# Kanban reconciliation guard

The reconciliation guard prevents a dispatcher from starting work whose
recorded repository, exact ref, candidate commit, workspace, assignee, or
replacement identity is no longer true. It is an opt-in contract carried in
`task_runs.metadata.reconciliation`; unrelated claim, failure, and retry runs
do not erase the most recent valid receipt.

## Receipt contract

A candidate receipt uses an exact repository identity, full ref, and full
commit SHA:

```json
{
  "reconciliation": {
    "repo": "/absolute/repo/or-exact-remote",
    "ref": "refs/heads/feature",
    "candidate_head": "0123456789abcdef0123456789abcdef01234567",
    "review": {
      "head": "0123456789abcdef0123456789abcdef01234567",
      "verdict": "approved"
    }
  }
}
```

Short branch names are interpreted only as `refs/heads/<name>`. Git is never
allowed to DWIM a tag, remote-tracking ref, or similarly named object. A
definitive absent exact ref is `branch_missing`; timeout, permission failure,
budget exhaustion, or an unreadable repository is `branch_unknown` and must
not be treated as absence.

Persistent workspaces must exist, be Git checkouts, belong to the declared
repository, and be at `candidate_head`. A `worktree` task additionally requires
a linked worktree. Retired `/home/openclaw` paths and persistent `/tmp` paths
are rejected.

Review evidence is current only when its full `head` equals both the candidate
receipt and the observed exact-ref head. A stale review is retained as history
and routes a task already in `review` to a reviewer for a fresh exact-head
decision; it does not prevent that reviewer from running.

## Replacement trust

A source task cannot declare itself replaced. Suppression requires all of the
following target-owned evidence:

- a distinct replacement task exists and is `done` or `archived`;
- the replacement's own receipt names `supersedes_task_id`;
- its canonical identity resolves to itself;
- its terminal receipt names the replacement task, contains a full SHA, and
  has state `merged`, `landed`, or `archived`;
- terminal and candidate heads agree when both are present.

Only then is the source removed from ordinary dashboard columns. The complete
source card remains in the board response's `suppressed` audit collection and
is still available through its detail, run, event, and comment endpoints.

## Sensor and claim boundary

One request-scoped Git probe session serves the entire controller tick. Exact
ref and workspace observations are cached, individual Git commands are capped
at two seconds, the session is capped at twelve commands and six seconds, and
budget exhaustion fails closed as unknown.

Git/network work never runs under SQLite's writer lock. The sensor records a
DB-backed fingerprint plus `PRAGMA data_version`; the claim transaction checks
both and recomputes the fingerprint before its first mutation. A raced task,
run, receipt, assignee, workspace, claim, or replacement change therefore
creates no claim, run, or event.

`dispatch --dry-run` backs the open database into memory and executes the
preview there. It does not acquire the dispatch lockfile, signal/reap a
process, create a workspace, spawn a worker, or write any byte to the source
database.

## Bounded continuation routing

`hermes kanban dispatch --continue-blockers` records a bounded controller pass
in `kanban_continuations`. Ordering is deterministic: board hygiene, repair,
review, ops, transient retry, proof-needed, then human-required; priority,
block age, and task id break ties. A pass scans at most 250 tasks and emits at
most 25 decisions by default (hard maximum 100).

The idempotency lock is derived from the task, route action, and stable failure
fingerprint. Duplicate ticks insert no duplicate row. Changed proof supersedes
the prior pending row, transient observations use `next_retry_at` with bounded
exponential backoff, proof-needed state remains explicit, and human-required
work is reported but left untouched. The non-human blocker SLA remains 900
seconds.

The controller is restricted to private, reversible routing records. It cannot
create Kanban cards, merge, deploy, restart services, notify external parties,
or declare product work complete.
