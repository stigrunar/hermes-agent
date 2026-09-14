# HRI R13 safety slice

This source-preparation slice closes the two demonstrated R12 gaps:

- bound worktree resolution fails closed when an occupied canonical checkout has
  no safe exact-base fallback, while unbound legacy reuse/fallback behavior is
  preserved;
- owner-replan hygiene reads structured task-row `hygiene_class` and
  `superseded_by` first, falling back to body metadata for both the current task
  and apparent successors.

## Verification

All commands ran from the R13 worktree at the tested revision before commit.

1. Preservation gate: exit `0`, `47 passed, 1 deselected, 4 warnings`.

   ```text
   env -i HOME=/home/hermes PATH=/usr/bin:/bin TEMP=/home/hermes/exports/hri-direct-update-20260910/pytest-temp-isolated /home/hermes/.hermes/hermes-agent/venv/bin/python -m pytest -o addopts= -q tests/hermes_cli/test_kanban_roadmap_binding.py tests/hermes_cli/test_kanban_project_link.py tests/hermes_cli/test_kanban_outcome_mutation_lease.py tests/hermes_cli/test_direct_codex_execution.py -k 'not test_explicit_integration_ready_candidate_requires_immutable_push_clean_and_proof'
   ```

2. Immutable worktree falsifier: exit `0`; bound occupied canonical path was
   rejected (`rejected: true`).

   ```text
   env -i HOME=/home/hermes PATH=/usr/bin:/bin PYTHONPATH=/home/hermes/.hermes/hermes-agent/.worktrees/hri-semantic-port-r4 /home/hermes/.hermes/hermes-agent/venv/bin/python /home/hermes/exports/hri-direct-update-20260910/r12-owner-worktree-probe.py
   ```

3. Immutable owner-intent falsifier: exit `0`; both structured-row cases
   emitted no event (`intent_count: 0` for `hygiene_class` and
   `superseded_by`).

   ```text
   env -i HOME=/home/hermes PATH=/usr/bin:/bin PYTHONPATH=/home/hermes/.hermes/hermes-agent/.worktrees/hri-semantic-port-r4 /home/hermes/.hermes/hermes-agent/venv/bin/python /home/hermes/exports/hri-direct-update-20260910/r12-owner-intent-probe.py
   ```

4. Changed-Python compilation: exit `0`.

   ```text
   env -i HOME=/home/hermes PATH=/usr/bin:/bin /home/hermes/.hermes/hermes-agent/venv/bin/python -m py_compile hermes_cli/kanban_db.py hermes_cli/kanban_db_workspace.py tests/hermes_cli/test_kanban_roadmap_binding.py
   ```

5. Diff hygiene: `git diff --check` exit `0`.

## Independent owner verification

Owner inspected the exact four-path diff and verified all working blob IDs
against the native final receipt. The unfiltered14-file gate reports
**284 passed,1 failed**, exit1,66.5s (`r13-owner-preservation.log`). Its sole
failure is the unchanged, explicitly deferred review-tool candidate contract;
no R13 criterion failed. Both immutable R12 falsifiers now pass: occupied bound
worktree rejected and both structured suppression cases emit zero intents.
The prior independent projection-disabled lease and scope/WAL probes also
pass. All stores/repos were temporary; no live worker or service action.
Compilation and whitespace checks pass.

Owner verdict: **source_slice_verified**, not full HRI acceptance. Proof:
`r13-owner-probes.json` and exact per-probe logs in the existing exports dir.
The next tool-boundary baseline was exercised separately:6 files,55 passed,
11 failed;9 failures are missing structured conversation identity roundtrip,
while2 concern the later descendant-environment fence. These are explicit
remaining preservation work, not a waiver or new runtime action.

## Deferred proof

The excluded review-tool test remains intentionally red in the owner's
unfiltered suite until the later source port. The assigned R13 independent
14-file gate and old/new falsifiers are complete above; later source changes
require candidate-bound revalidation.
Supplemental migrations, gateway consumption, review-tool port, full candidate
independent review, and release/rollback/live gates remain deferred. This slice
is source preparation only and is not acceptance/live deployment.
