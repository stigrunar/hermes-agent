# HRI R10 binding receipt

R10 preserves Project/Outcome task identity and root admission across the
Kanban board boundary. Task rows now carry the outcome, conversation lane and
exact topic target, parent execution identity, repository/base/scope mutation
binding, and resource requirements. The migration is additive for existing
board databases. Child creation inherits the parent identity (including the
project fallback), while invalid project/outcome/scope combinations fail
closed.

Claims project a board-derived `kanban:<board>:<task>` execution into the
existing `outcomes_db` admission path. Mutation/resource conflicts leave the
board task ready and emit a deduplicated conflict event; successful claims,
heartbeats, requeues, rollback, and completion update or release the same root
execution and its leases. Lane-bound notification subscriptions are rewritten
to the exact structured target, and rebinding updates the task, subscription,
and existing execution projection.

## Verification

Required scoped proof (exit 0):

```text
env -i HOME=/home/hermes PATH=/usr/bin:/bin TEMP=/home/hermes/exports/hri-direct-update-20260910/pytest-temp-isolated HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/venv/bin/python timeout --signal=TERM --kill-after=10s 180 bash scripts/run_tests.sh -j2 --file-timeout 120 tests/hermes_cli/test_kanban_project_link.py tests/hermes_cli/test_kanban_outcome_mutation_lease.py tests/hermes_cli/test_direct_codex_execution.py
```

Result: 3 files, 18 tests passed, 0 failed.

Changed-source compile (exit 0):

```text
/home/hermes/.hermes/hermes-agent/venv/bin/python -m py_compile hermes_cli/kanban_db.py hermes_cli/kanban_db_connect.py hermes_cli/kanban_db_notify.py
```

`git diff --check` passed (exit 0). Tested revision is
`d7e6c4a18a0cdf8726003df068d92c369937ad2f`, with the working tree dirty by
the scoped R10 changes. Full release acceptance, live service/runtime proof,
and integration/landing remain with the owner; this receipt makes no
complete-HRI or deployment claim.

## Owner validation — source changes required

Owner independently ran 13 relevant files: **255 passed, 0 failed**, exit 0,
87.7 seconds (`r10-owner-preservation.log`). This is not full acceptance:
`r10-owner-lease-probe.py` proved a missing preserved safety path with
`cross_project_orchestration_enabled=False`. R10 admitted both overlapping
board tasks and held zero mutation leases (exit 1); the exact accepted
`dc7c62921a31131822e4dbd59db4920b390f8fbc` source admitted only the first and
held one lease (exit 0). All stores were temporary; no worker was spawned.

The accepted `_acquire_task_mutation_lease` / renewal / release path must
operate independently of optional execution projection. R10 restored only
the projection-enabled path. Native execution `ex_49503ed9` completed its
process normally, but owner verdict is **CHANGES_REQUIRED**, not an accepted
source slice. Preserve the partial patch; a new bounded owner revision must
restore the missing lease path and prove both feature-flag modes before
roadmap/base and owner-wake preservation continue. No live change occurred.

