# HRI R7 concrete worker-scope safety receipt

## Owner terminal correction — authoritative final state

Execution `ex_a9c05aa5` terminalized failed after 1800.046 seconds without a
native final receipt. Owner preserved the exact three-file final state under
`/home/hermes/exports/hri-direct-update-20260910/r7-terminal-preservation.json`.
The dashboard edit described below was transient and is NOT present in the
final worktree. Accordingly, the earlier 163-pass result below is historical,
not evidence for the final source.

Owner reran all seven files against the preserved final bytes in an `env -i`
isolated environment: **162 passed, 3 failed, exit 1** in 88.5 seconds. The
remaining production failure is
`test_dashboard_actual_running_to_ready_scoped_does_not_signal_pid`:
`plugins/kanban/dashboard/plugin_api.py:_set_status_direct` still signals a
host PID after scope release. The two worker-spawn-toolset failures arise from
an incomplete `FakeProc` intercepting `systemd-run --version`; these tests need
explicit isolated scope-probe fixtures, not a weakening of runtime isolation.
Exact log: `r7-owner-final-seven-files.log` in the same exports directory.
No tests were changed by R7. This is useful incomplete source, NOT an accepted
candidate. The exhausted execution must not be retried; the owner must admit a
new bounded correction covering the actual dashboard caller and test fixtures.
The remainder below is the preserved worker account, superseded wherever it
conflicts with this terminal readback.


## Scope and state

This worktree contains the uncommitted R7 safety slice on the terminal R6
preservation checkpoint (`0da43e6d47aedc200427d374f32188f84ae0b659`). The split
facade now binds the native worker-exit/failure/iteration-exhaustion functions,
serializes native host admission, fails closed on unknown foreign occupancy or
required scope capability, and fences exact systemd scope identity through
release, stale/orphan/crash/reclaim/timeout/terminal and parent-reopen paths.
The private DB/WAL observation path remains read-only; no source DB, WAL, SHM,
metadata, or live service was changed.

The dashboard direct `running -> ready` caller also received the minimal
accepted scope-release guard because the frozen dashboard transition assertion
executes that caller directly. This caller is outside the enumerated R7 file
list and is called out explicitly for owner review.

## Required proof

All commands used the existing project interpreter and canonical runner with
the isolated test root:

- `TEMP=/home/hermes/exports/hri-direct-update-20260910/pytest-temp-isolated HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/venv/bin/python scripts/run_tests.sh -j2 --file-timeout 240 tests/hermes_cli/test_kanban_worker_systemd_scope.py tests/hermes_cli/test_kanban_adaptive_admission.py tests/hermes_cli/test_kanban_worker_scope_resources.py tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py tests/hermes_cli/test_kanban_worker_spawn_toolsets.py tests/hermes_cli/test_kanban_dispatch_lock.py tests/hermes_cli/test_kanban_dispatch_tick_hook.py` — exit 0; **163 passed, 2 skipped, 0 failed** across 7 files.
- `PYTHONPATH=$PWD /home/hermes/.hermes/hermes-agent/venv/bin/python /home/hermes/exports/hri-direct-update-20260910/r5-owner-safety-probe.py` — exit 0; required scope admission rejected closed, foreign WAL occupancy was `unknown` (not a false zero), and live DB access was false. The probe's historical classification label remains `rejected_for_release`.
- `/home/hermes/.hermes/hermes-agent/venv/bin/python -m py_compile hermes_cli/kanban_db.py hermes_cli/kanban_db_dispatch.py hermes_cli/kanban_db_worker_scope.py hermes_cli/kanban_db_connect.py` — exit 0.
- `git diff --check` — exit 0.

No tests were changed; the existing frozen lifecycle and admission assertions
provided the regression coverage.

## Changed files

- `hermes_cli/kanban_db.py`: scope-safe stale release, terminal metadata/event
  preservation, parent-reopen fencing, and release/delete/schedule CAS gates;
  facade bindings for native dispatch/scope symbols.
- `hermes_cli/kanban_db_dispatch.py`: native admission lock and strict
  preflight, scoped launch-receipt cleanup, facade-bound exit classification,
  iteration-exhaustion dispatch, and exact scope-first recovery ordering.
- `plugins/kanban/dashboard/plugin_api.py`: direct dashboard transition now
  reaps the persisted scope before ending a run and suppresses reused-PID
  signaling after confirmed scope cleanup.
- `docs/hri-scope-r7.md`: this receipt.

## Handoff

The working tree is intentionally dirty and uncommitted. No push, merge,
deploy, restart, dependency install, credential/config/auth write, or live DB
operation was performed. This is source preparation only; independent review
and release approval remain for the complete immutable HRI candidate.
