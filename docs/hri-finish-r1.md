# HRI finish R1 — incomplete source checkpoint

**BLOCKED / INCOMPLETE. This is not a passing, committed source candidate. HRI is neither accepted nor installed.**

Project `p_155df2bb`; Outcome `o_cde72dc3`; owner Dolly/default; lane `cl_15e90882`; target `telegram:-1003951469776:3`.

## Identity and scope

Initial clean base: `cb49a601a02474836051d1cf55adcf6776d9f13d`, tree `5efbca4970a6e3496bbdbb8b4aaddb9a0b35ff35`. Fixed upstream `4a76e99f876494c7a848f5298ea82a9d7b9d2204` was verified as an ancestor. HEAD remains the base; candidate commit and candidate Git tree are unavailable because Git metadata is read-only. The working tree is dirty.

Actual native IDs exposed by collaboration: root `/root` (selected `gpt-6-astra/low`), child `/root/hri_implementation` (explicit `gpt-5.6-luna/xhigh`, `fork_turns=none`). One implementation leaf; no recursive delegation. Separate runtime UUIDs were not exposed. Root reviewed the diff and ran static checks; the child owned implementation and tests.

The preserved 14-source-file manifest was independently hash-checked with zero mismatches. Preserved tracked.patch SHA-256: `43a005a6840b3d41b1b6227c0952987400866ec1226132656b80ce1a9472a875`. The approved App Server repair remains in the base. No upstream fetch, reset, integration, push, deployment, real updater, service, live DB, global/profile config or credential mutation was performed.

## Changes

- `agent/conversation_loop.py`: Use completed-call count for the closeout reserve notice; focused no-extra-call test passed.
- `agent/delegation_context.py`: Preserve dispatcher task context and honor explicit delegated-child markers.
- `gateway/delivery_ledger.py`: Discover legacy deferred/dead-owner obligations alongside native failed flood rows; preserve deadlines and retry marker.
- `gateway/run.py`: Remove obsolete separate deferred-queue fields.
- `gateway/run_startup.py`: Route legacy deferred delivery through native flood timer and shield legacy claims; runtime-sweep cancellation remains incomplete.
- `hermes_cli/kanban.py`: Forward existing Outcome and mutation/resource inputs on create.
- `hermes_cli/kanban_db.py`: Restore invariant-floor context.
- `hermes_cli/kanban_diagnostics.py`: Suppress obsolete terminal/history diagnostics.
- `hermes_cli/kanban_parser.py`: Restore bounded existing create flags.
- `hermes_cli/projects_cmd.py`: Restore advertised Outcome, lane, execution, resource and Telegram command wiring to native services.
- `hermes_state_holders.py`: Exclude recognized non-file descriptor targets only; unresolved ordinary files remain fail-closed.
- `plugins/memory/hindsight/__init__.py`: Keep tools-only embedded initialization lazy and default automatic retention off.
- `tests/gateway/test_telegram_deferred_delivery.py`: Port queue fixtures to native timer; retain delivery assertions and add behavioral/legacy/ingress probes.
- `tests/hermes_cli/test_doctor.py`: Repoint stale defining-module mocks.
- `tools/browser_use_cli.py`: Restore missing contextlib/importlib imports.

- `docs/hri-dollycode-native-r6.md`: recovered historical receipt, unchanged from preserved source.
- `docs/hri-finish-r1.md`: this current receipt.

## Required proof and actual result

The frozen list was verified by code: **108 entries, 108 unique files**. One combined gate was launched after source freeze using the exact file list, existing interpreter, `scripts/run_tests.sh -j 2 --file-timeout 180 --file-retries 0`. Exact full command: `.local-only/hri-finish-r1/full-gate-command.txt`; log: `.local-only/hri-finish-r1/full-gate.log`.

Isolation used task-local HOME/TEMP/TMP/TMPDIR; the canonical runner strips unspecified environment values and conftest isolates HERMES_HOME. Repo-local temporary roots still inherit this repository's Git ancestor; this caused prompt-context discovery failures. Actual process inspection and signaling errors also occurred under the restricted environment. No guards were weakened to make those checks pass.

Last recorded partial gate: **22/108 files with terminal results; 437 passed, 7 failed, 0 reported collection/setup errors, 0 reported skips, 4 file timeouts; 86 files unfinished**. Timed-out file output contains additional partial assertion failures that have no final pytest totals and are not included in the numeric test totals. Zero reported skips is a partial observation, not proof that the full list has no platform skips. Gate terminated with SIGINT, exit 130. Timed-out delivery-ledger output records eight additional failure markers (and 24 pass markers); goal-resume output records two pass markers. These partial markers are separate from finalized pytest summaries. Per-file counts are in the handoff artifacts.

Focused logs include Project CLI (9 passed), native Telegram with the added ingress probe (13 passed, 1 failed; subsequent ingress rerun incomplete), reserve timing (1 passed), and isolated Hindsight initialization checks. These do not establish the whole preservation gate. A state-holder run that passed with a profile/path shortcut was rejected; that shortcut was removed and its result is invalid for the candidate. Some targeted logs are incomplete; no pass is inferred from them.

Root verification: changed-Python compilation (15 files), affected Ruff, scope validation and `git diff --check` all passed, exit 0; exact commands/file lists in `.local-only/hri-finish-r1/root-static-checks.json`. No broader build was required for these Python changes.

Worker pre-gate tracked-diff SHA-256 `4a7c83da64f314ef1622fa9bfa3ac95819a32b4f4087312c7ac2e044d93d5f12` was independently rechecked during the gate and matched. Per-Python byte hashes were also stable from root static verification onward. This receipt is added after source freeze; it does not change tested Python. Final source/patch hashes are in handoff.json.

## Concrete blockers

1. Local commit: `git add -- agent/delegation_context.py` exited 128: `fatal: Unable to create '/home/hermes/.hermes/hermes-agent/.worktrees/hri-finish-r1/.git/index.lock': Read-only file system`. A local exclude write also failed with Errno 30. The effective sandbox contradicts the writable-clone expectation. No commit was created and no metadata-protection bypass was attempted.
2. C1 is still incomplete: `gateway/run_startup.py::_redeliver_failed_obligations_for_platform` can discard claims returned by `sweep_failed_for_runtime` after `_ledger_call` cancellation, with no compensating release. `gateway/platforms/base.py` ingress persistence/finalization still uses raw threaded mutations. Legacy-only shielding is not the complete runtime/ingress cancellation fix.
3. The combined preservation gate failed and remained unfinished at the closeout boundary. Prompt-builder failures, delivery-flood invariant failures and timed-out files are preserved in the log. The exact per-file table is `.local-only/hri-finish-r1/root-gate-summary.json`. Required inherited safeguards and all updater states cannot be claimed green from this result.
4. Restricted `/proc` visibility produces unknown state DB holders; browser process tests also reported the live-system guard rejecting child PIDs as outside the test subtree. Those safety guards remain intact. These observations do not excuse unfinished source defects.

## Handoff

The deadline is not extended and this execution is not automatically retried. Source is checkpointed as a patch plus per-path SHA-256 manifest in `.local-only/hri-finish-r1/`; handoff.json binds the evidence. Seven immutable owner probes and old exports were not modified or rerun.

Dolly/default retains the existing Outcome and next owns recovery of this source checkpoint into a Git-writable execution, completion of runtime/ingress cancellation and a full passing preservation gate. Only then can the already-authorized independent review, private integration, backup/rollback-backed activation and fresh real-target proof proceed. No new user permission is requested here.
