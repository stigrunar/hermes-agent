# HRI R11 lease slice

## Source preparation result

This bounded correction restores Kanban mutation-lease ownership when
`kanban.cross_project_orchestration_v1_enabled` is false. It uses the existing
`hermes_cli.outcomes_db` mutation-lease authority directly, with
`kanban:<board>:<task>` as the owner identity. When projection is enabled, the
existing canonical execution admission remains the only acquire path.

Flag-off behavior is now:

- overlapping repository/path scopes across separate board databases serialize;
  the loser stays `ready` and receives one deduplicated conflict event;
- disjoint scopes may claim concurrently;
- heartbeats renew the standalone lease;
- run completion, block/requeue, reclaim, and other `_end_run` failure paths
  release the same board-scoped owner lease;
- lease authority errors fail closed before `ready -> running`, and a failed
  claim CAS rolls back the lease.

## Required proof

All commands below were run from the R11 worktree.

```text
env -i HOME=/home/hermes PATH=/usr/bin:/bin TEMP=/home/hermes/exports/hri-direct-update-20260910/pytest-temp-isolated HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/venv/bin/python timeout --signal=TERM --kill-after=10s 180 bash scripts/run_tests.sh -j2 --file-timeout 120 tests/hermes_cli/test_kanban_project_link.py tests/hermes_cli/test_kanban_outcome_mutation_lease.py tests/hermes_cli/test_direct_codex_execution.py
```

Exit `0`; 3 files and 22 tests passed.

```text
env -i HOME=/home/hermes PATH=/usr/bin:/bin PYTHONPATH=/home/hermes/.hermes/hermes-agent/.worktrees/hri-semantic-port-r4 /home/hermes/.hermes/hermes-agent/venv/bin/python /home/hermes/exports/hri-direct-update-20260910/r10-owner-lease-probe.py
```

Exit `0`; probe output reported `first_claimed=true`, `second_claimed=false`,
and `active_lease_count=1` with projection disabled.

```text
/home/hermes/.hermes/hermes-agent/venv/bin/python -m py_compile hermes_cli/kanban_db.py
git diff --check
```

Both exited `0`.

## Independent owner proof

Dolly inspected the exact three-path diff and verified that the worker's two
Python blob IDs matched the independently tested working files. The full
13-file preservation gate passed: **259 passed, 0 failed**, exit 0, 77.8s
(`r11-owner-preservation.log`). The immutable projection-disabled overlap
probe passed independently: first claimed, second rejected, one lease. The
scope/WAL falsifier passed: unavailable required scope rejected and foreign
WAL occupancy remained fail-closed unknown, never false zero. Compilation
and whitespace checks passed. These probes used isolated stores only.

Owner verdict: **source_slice_verified**, not complete HRI acceptance. The
separate four-file remaining-preservation baseline is still red: 13 passed,
13 failed plus two gateway collection errors. It identifies the already
pending roadmap/worktree-base, review-contract tool entry and owner-wake
ports, not an acceptance waiver (`post-r11-remaining-preservation.log`).

## Remaining gates

This is source preparation only. It does not establish the exact roadmap/base
gate, owner wake/continuation, full relevant release regressions, independent
complete-candidate review or live runtime acceptance. The R11 owner
preservation gate above is complete; later source changes invalidate it as
proof of that later candidate.
No deployment, push, integration, credential, or live service action was run.
