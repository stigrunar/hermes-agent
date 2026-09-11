# HRI DollyCode native R6 history — R9 source-only continuation

## Result

**INCOMPLETE WIP — not a PASS candidate. HRI is neither accepted nor installed.**

Project `p_155df2bb`; Outcome `o_cde72dc3`; lane `cl_15e90882`; target `telegram:-1003951469776:3`. Dolly/default owns independent inspection and all later release decisions. No push, merge, integration, deployment, installation, service, credential, configuration, live-database or model/network operation was performed.

R9 used `/home/hermes/.hermes/hermes-agent/.worktrees/hri-owner-recovery-r9`. Initial clean HEAD was `2c67c9739cb5fb3b8c5fe3f6061cdc9948652c03`. The read-only preserved patch was verified at SHA-256 `c373c00f906630025502555bc4a5998794cc3b5169ae1ffd3180e0ae46e4b1a8` and applied before implementation. All final changed paths are within selection.json.

The controller identity is `/root`, user-selected route `gpt-6-astra/low`. The one native worker is `/root/native_repair`, spawned with explicit `gpt-5.6-luna/xhigh`, `fork_turns=none`, reused for corrections and then frozen. No recursive delegation occurred. Canonical agent IDs and dispatch parameters are observable; separate runtime UUIDs/model telemetry were not exposed by the collaboration tool.

## Changed files

- `agent/delegation_context.py`: Recovered dispatcher transport environment change; explicit-map descendant fencing remains unverified.
- `gateway/delivery_ledger.py`: Incomplete native timer visibility for current-owner deferred rows and retry markers.
- `gateway/run.py`: Recovered removal of obsolete deferred-queue instance fields.
- `gateway/run_startup.py`: Incomplete native deferred delivery and cancellation/claim cleanup work; not accepted.
- `hermes_cli/kanban.py`: Forward outcome/mutation/resource inputs and include them in create JSON.
- `hermes_cli/kanban_db.py`: Restore invariant-floor and escalation context.
- `hermes_cli/kanban_diagnostics.py`: Suppress terminal-task and obsolete failure history.
- `hermes_cli/kanban_parser.py`: Restore bounded outcome/mutation/resource create flags.
- `hermes_cli/projects_cmd.py`: Restore Telegram projection command wiring; broader Project command acceptance incomplete.
- `hermes_state_holders.py`: Recovered descriptor-holder filtering change; full repair safety gate incomplete.
- `plugins/memory/hindsight/__init__.py`: Recovered tools-only automatic retention default.
- `tests/hermes_cli/test_doctor.py`: Repoint repair fixtures to defining modules; assertions retained.
- `tools/browser_use_cli.py`: Recovered missing contextlib/importlib imports; fixture repair incomplete.
- `docs/hri-dollycode-native-r6.md`: this R9 receipt.

An accidental worker edit to out-of-scope `hermes_cli/kanban_output.py` was detected and fully reverted. The worker's Telegram test rewrite weakened acceptance and was rejected; `tests/gateway/test_telegram_deferred_delivery.py` is restored byte-for-byte to HEAD. No new skips, xfails, or weakened assertions remain.

## Verification

Evidence directory: `/tmp/hri-dollycode-r7-3pslhusy/` (all R9 artifacts use `r9-` except `handoff.json`). Exports and the old worktree were not modified.

- Startup/base/patch and initial post-patch source hashes: `r9-startup.json`.
- Exact combined command: `bash /tmp/hri-dollycode-r7-3pslhusy/r9-combined-gate.sh`, launched through `python3 /tmp/hri-dollycode-r7-3pslhusy/r9-run-gate.py`. The script contains the required clean environment, `scripts/run_tests.sh -j2 --file-timeout 180`, the exact frozen 98-file list, and `tests/gateway/test_delivery_flood_invariants.py`.
- Combined gate: **interrupted, exit 130**, 14/99 files with terminal results; **297 passed, 2 assertion failures, 2 timed-out files**, zero observed collection/setup errors and zero reported skips in those completed results. **85 files have no terminal result**. The two timeouts each exhausted 180 seconds plus the default retry (360.2 seconds per file). Counts are partial and do not represent preservation success.
- Assertion failures: two prompt-builder tests discover read-only `/tmp/.git` as a Git root. That environment path was observed, not changed.
- Timeouts: `tests/gateway/test_delivery_ledger.py`, `tests/gateway/test_goal_resume_restart.py`.
- Full output and before/after hashes: `r9-combined-gate.log`, `r9-gate-result.json`; source stable across the gate. The receipt document was added afterward; tested Python source was not changed.
- Root affected CLI command: `env -i HOME=/home/hermes PATH=/usr/bin:/bin TEMP=/var/tmp TMPDIR=/var/tmp HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/venv/bin/python bash scripts/run_tests.sh -j2 --file-timeout 180 tests/hermes_cli/test_kanban_cli.py`: **6 passed, exit 0**, `r9-scope-blocker-test.log`.
- Worker early Telegram baseline: 12 failed. Worker CLI batch: 37 passed. Other worker runs hung or were interrupted; a focused cancellation diagnostic used a 30-second timeout and completed zero tests. Worker pipelines used `tee` without `pipefail`; exact exit codes were not retained. See `r9-worker-run-inventory.json` for exact retained commands and explicit missing evidence.
- Changed Python compilation: **pass, exit 0**. `git diff --check`: **pass, exit 0**. Exact command/file list in `r9-static-checks.json`.
- Local commit **blocked**: `git add` exited 128 because `/home/hermes/.hermes/hermes-agent/.git/worktrees/hri-owner-recovery-r9/index.lock` is read-only. No commit was created; all 14 changed files remain uncommitted. Exact error: `r9-git-result.json`.
- Local Git commit/state and final per-path base/post-patch/final hashes: `handoff.json`. Any commit is explicitly an incomplete WIP candidate, not acceptance.

## Blocking gaps

- Cancellation during the native runtime sweep can discard returned claims, leaving rows attempting; send/finalization cancellation cleanup remains incomplete.
- Native timer discovery filters to current-process-owned rows. Unowned legacy deferred rows are also excluded by startup sweeping, leaving recovery incomplete.
- Ingress persistence/finalization still uses raw threaded calls. No completed equivalent ingress → persisted refusal → native timer → confirmed ACK proof exists.
- The original 12 Telegram assertions remain in place and were not successfully ported. Several other original failure groups were not repaired or verified before freeze.
- Recovered explicit-environment descendant fencing and all inherited safety requirements lack a complete preservation gate.
- Seven immutable owner probes were hash-verified, not executed in R9. `r9-immutable-probe-manifest.json` references the original owner scripts. One immutable safety probe hardcodes the old worktree path; its current-workspace binding must be resolved by Dolly/default without pretending it passed here.

## Next owner action

Dolly/default should inspect this exact incomplete WIP, its patch/hashes and terminal evidence, resolve the remaining native safety defects and probe binding in an explicitly authorized continuation, and require a complete preservation gate before any candidate acceptance. Independent review and later release/updater/idempotence/rollback/actual-target gates remain outstanding.
