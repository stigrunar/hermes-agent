# HRI R15 descendant write-fence receipt

## Scope

R15 restores the native delegated-child environment behavior and keeps the
stronger local policy: worker descendants retain the persistent
`HERMES_DELEGATED_CHILD_CONTEXT` fence and only bounded non-secret Kanban read
locations (`HERMES_KANBAN_DB`, `HERMES_KANBAN_BOARD`, and
`HERMES_KANBAN_WORKSPACE`). Task, run, claim-lock, goal, worker-scope, and
unknown/future `HERMES_KANBAN_*` capability keys are stripped. The parent
dispatcher environment is not mutated. Execute-code uses the shared helper
while preserving scoped passthrough secret denial and local SQLite routing.

## Verification

Focused checks completed before the required gate:

- `env -i HOME=/home/hermes PATH=/usr/bin:/bin TEMP=/tmp HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/venv/bin/python bash scripts/run_tests.sh tests/tools/test_local_env_blocklist.py -k 'TestKanbanNestedSpawnScrub or TestPythonpathSelectiveStrip'`: pass, 36 passed, 1 skipped, 0 failed. The skip was a non-Linux platform case.
- `env -i HOME=/home/hermes PATH=/usr/bin:/bin TEMP=/tmp HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/venv/bin/python bash scripts/run_tests.sh tests/tools/test_delegate_kanban_isolation.py tests/tools/test_code_execution_windows_env.py -k 'test_delegate_child_execute_code_env_bridges_contextvar_and_scrubs_kanban or worker_descendant_fence_survives_task_removal_without_parent_mutation or TestKanbanDescendantRouting or test_worker_terminal_foreground_spawn_strips_kanban_env or test_worker_terminal_foreground_spawn_carries_descendant_fence or test_worker_terminal_background_spawn_strips_kanban_env'`: pass, 6 passed, 0 failed.
- `env -i HOME=/home/hermes PATH=/usr/bin:/bin TEMP=/tmp HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/venv/bin/python bash scripts/run_tests.sh tests/tools/test_kanban_descendant_scope.py -k 'test_terminal_descendants_cannot_mutate_even_after_task_is_removed or test_worker_cli_cannot_use_foreign_task_to_drop_run_scope'`: pass, 2 passed, 0 failed.

The required unfiltered gate was run exactly once:

```text
env -i HOME=/home/hermes PATH=/usr/bin:/bin TEMP=/tmp HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/venv/bin/python bash scripts/run_tests.sh -j2 --file-timeout 180 tests/tools/test_kanban_descendant_scope.py tests/tools/test_delegate_kanban_isolation.py tests/tools/test_local_env_blocklist.py tests/tools/test_code_execution_windows_env.py tests/tools/test_env_passthrough.py tests/cron/test_cron_kanban_env_isolation.py .
```

Result: incomplete; `.` expanded the run to 3,949 files (about 40,940 tests).
The run was stopped with Ctrl-C at the assigned hard-stop boundary before the
requested files were reached. No final runner exit code or final counts were
emitted. Observed progress at interruption was `0.9% | 353/~40940 | 265
passed | 15 failed`. The observed failures were the unrelated optional
dependency cases in `skills/productivity/pdf/tests/test_pdf_skill.py` (6
passed, 15 failed because `pypdf` was unavailable), plus collection failure in
`skills/productivity/docx/tests/test_docx_skill.py` because `docx` was
unavailable. Two unrelated ACP files hit the declared per-file timeout:
`tests/acp/test_mcp_e2e.py` (180s, process tree killed; 7 collected, one dot)
and `tests/acp/test_server.py` (180s, process tree killed; 30 collected, 18
dots). No final platform skip summary was emitted; focused Linux checks
reported the expected macOS/Windows skips. The mandated gate is therefore not
a release-green result.

Static checks after the source/test changes:

- `.../venv/bin/python -m py_compile agent/delegation_context.py tools/code_execution_env.py tests/tools/test_kanban_descendant_scope.py tests/tools/test_delegate_kanban_isolation.py tests/tools/test_local_env_blocklist.py tests/tools/test_code_execution_windows_env.py`: pass.
- `git diff --check`: pass.

Tested revision/state: `HEAD=a6b042436c134d09d02105adbb724df3853b40f1`, base
tree `f40bb4f04cb4797e3fdd6ca427df19452d568b22`; working tree is dirty only in
the R15 allowed paths. No commit was created.

## Independent owner verification

The worker execution finished `partial` in 1530.573s, not accepted from exit 0.
Owner matched the six dirty paths to the receipt and reviewed the actual
source and changed assertions. The real ingress test file was unchanged;
legacy prefix/no-marker checks now assert bounded read context, persistent
fencing and absent known/future capabilities. The ordinary POSIX oracle is
unchanged. Composition mocks now use bounded EOF streams and a test-only
listener seam; real process/CLI ingress remains independently exercised.
No surviving HRI pytest/run_tests processes were observed after handoff.

Owner then executed the exact mandatory command WITHOUT the erroneous `.`:
**6 files, 170 passed, 0 failed, 6 existing platform skips**, exit 0 in 16.6s
(`r15-owner-six-file.log`). This supplies the missing six-file evidence;
it does not turn the accidentally broadened worker run into a passing gate.

Independent environment falsifiers: **10 passed**, exit 0 in 0.23s,
covering ambient/mapping/ContextVar/second-hop fences, absent parent mutation,
ordinary helper/strip compatibility, scoped denial of each valid read-location
key, and invalid database-DSN rejection (`r15-owner-environment-boundary.log`).
The unfiltered owner preservation gate passes **18 files, 345 tests, 0 failed**,
exit 0 in 80.3s; separate real-registry proof passes **20 tests** in 7.66s.
All four immutable worktree/intent/lease/scope-WAL falsifiers pass. Fresh changed
Python compilation and `git diff --check` pass. Exact source blobs are in
`r15-owner-verified-blobs.json`; logs/probes are in the existing HRI exports.
Owner verdict: **source_slice_verified**, not HRI/release acceptance.

The next independent gateway baseline still reports four failed replan tests
and two collection-error files (missing owner-wake helpers), zero passed,
23.7s. These are subsequent preserved-owner notification obligations, not
R15 environment regressions; `post-r15-owner-gateway-baseline.log` retains them.

## Risks / not verified

- Native Windows execution and independent release QA remain unverified.
- No live database, model, network, service, credential, deployment, or push
  operation was used.
