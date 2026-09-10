# HRI R5 adaptive admission port receipt

## Scope and base

- Worktree: `hri-semantic-port-r4`, branch `fix/hri-semantic-port-r4`.
- Requested base: `e8e78e7bd01b295e294d532b975aa6658cc819b3` (initial tree
  `ca058be72e0f47a4500273f3eaa609fe52848c46`).
- Parents supplied for semantic reference: downstream
  `dc7c62921a31131822e4dbd59db4920b390f8fbc`, fixed upstream
  `4a76e99f876494c7a848f5298ea82a9d7b9d2204`.
- Implementation is a direct native split relocation: database capability
  persistence remains in `kanban_db.py`; adaptive admission and dispatch
  implementations live in `kanban_db_dispatch.py`; the facade exports real
  implementations.

## Proof run

All test commands used the existing interpreter and canonical runner:
`HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/venv/bin/python
./scripts/run_tests.sh ...`.

- Initial targeted missing-contract reproduction:
  `tests/hermes_cli/test_kanban_adaptive_admission.py -k test_caps_are_strict_and_cli_cannot_widen` — **exit 1**, expected base failure: `hermes_cli.kanban_db` lacked `resolve_dispatch_caps`.
- `tests/hermes_cli/test_kanban_adaptive_admission.py` — **exit 0**, 39 passed.
- `tests/hermes_cli/test_kanban_worker_spawn_toolsets.py tests/hermes_cli/test_kanban_dispatch_lock.py tests/hermes_cli/test_kanban_dispatch_tick_hook.py` — **exit 0**, 9 passed.
- Controller rerun: adaptive admission plus worker toolsets with `-j 2` — **exit 0**, 42 passed.
- Controller rerun: dispatch lock plus tick hook — **exit 0**, 6 passed.
- Combined acceptance including `tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py` — **exit 1**, 50 passed and 5 failed. The five failures are all CLI pre-gating cases that call `hermes_cli/kanban.py:kanban_command`, which initializes the DB before the admission handler. `kanban.py` is outside the R5 allowed-write set; no workaround was applied.
- `/home/hermes/.hermes/hermes-agent/venv/bin/python -m py_compile hermes_cli/kanban_db.py hermes_cli/kanban_db_dispatch.py` — **exit 0**.
- `git diff --check` — **exit 0**.

## Changed files

- `hermes_cli/kanban_db.py`: canonical required-capability vocabulary/aliases,
  task/schema/row persistence, additive legacy-column migration, and native
  dispatch facade exports.
- `hermes_cli/kanban_db_dispatch.py`: adaptive cap/profile/capability admission,
  immutable canonical policy snapshots, allocation/memory/scope admission,
  pre-claim capability rejection, and adaptive dispatch locking.
- `docs/hri-admission-port-r5.md`: this receipt.
- Tests modified: none; existing regression coverage was sufficient.

## Remaining gates and handoff

The CLI passthrough pre-gating failures remain **NOT_VERIFIED** for this slice
because their required fix is in the out-of-scope `hermes_cli/kanban.py` facade.
The requested adaptive, worker-toolset, dispatch-lock, and dispatch-hook proof
is green. Independent QA is deferred to Dolly's exact-committed candidate
review and full release-runtime gates. Deferred project/outcome lifecycle,
notifier, updater/runtime bridge, auth circuit, Outcome wake, and unrelated
prompt/toolset/runtime improvements remain out of scope.

Final receipt: useful R5 implementation diff retained; no commit, push, merge,
install, service, live-config, database, or network write was performed.
