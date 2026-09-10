# HRI R6 native boundary preservation receipt

## Current state

This worktree contains an uncommitted R6 safety slice on top of the requested
split base. Native worker-scope identity, required-scope fail-closed probing,
launch receipts, private read-only DB/WAL snapshots, strict foreign-board
occupancy observation, scoped terminal requests/reaping, and CLI dispatch
admission-before-init are wired through the split facades. No source WAL/SHM,
inode, or mtime is rewritten by read-only preview paths.

## Proof

All focused checks used the canonical runner and project interpreter:

- `PYTHONPATH=$PWD /home/hermes/.hermes/hermes-agent/venv/bin/python /home/hermes/exports/hri-direct-update-20260910/r5-owner-safety-probe.py` — exit 0; required scope rejected, foreign WAL occupancy remained unknown, no live DB access.
- `HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/venv/bin/python ./scripts/run_tests.sh -j2 tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py tests/hermes_cli/test_kanban_worker_scope_resources.py` — exit 0; 23 passed.
- Scoped worker terminal/reaping subset of `test_kanban_worker_systemd_scope.py` — exit 0; 10 passed across the focused runs (one runner retry was flaky before the final release-fence fix).
- `python -m py_compile hermes_cli/kanban.py hermes_cli/kanban_db.py hermes_cli/kanban_db_connect.py hermes_cli/kanban_db_dispatch.py hermes_cli/kanban_db_worker_scope.py` — exit 0.
- `git diff --check` — exit 0.

The full `test_kanban_worker_systemd_scope.py` file was not accepted as a
green proof in this handoff: the canonical runner exceeded the bounded 50–55s
probe window while exercising its broad live-systemd matrix. Parent review
must rerun that required file and the remaining R6 acceptance set.

## Handoff

Working tree is intentionally dirty and uncommitted; parent owns integration
and final acceptance. No push, merge, deploy, credential, or live-service
operation was performed.
