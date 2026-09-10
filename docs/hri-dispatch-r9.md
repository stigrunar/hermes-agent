# HRI R9 — dashboard dispatch admission owner verification

Project p_155df2bb; Outcome o_cde72dc3; owner default. Source base c8b4b91b2578ee8f4950c8f445a2fb90d6243982, tree cd0f206ea44c6d1f851faaa411a520843c35ad59. Frozen upstream cutoff 4a76e99f876494c7a848f5298ea82a9d7b9d2204.

Execution ex_651c8769 terminalized failed at 600.044 seconds without a native final receipt. It is not retried or relabeled successful. Owner independently reviewed its sole production diff and completed the missing verification on the preserved bytes. The source repair passes its assigned gates; the complete HRI candidate remains unaccepted.

The existing dispatch endpoint restores accepted dc7c62921a31131822e4dbd59db4920b390f8fbc semantics: absent max override does not widen canonical capacity; admission precedes all board connections/schema initialization; preview uses read-only connection; dispatch receives the exact prepared snapshot; invalid admission is refused. No new helper, test changes, global route changes or unrelated source edits.

## Actual owner proof

Working directory: /home/hermes/.hermes/hermes-agent/.worktrees/hri-semantic-port-r4

```sh
env -i HOME=/home/hermes PATH=/usr/bin:/bin TEMP=/home/hermes/exports/hri-direct-update-20260910/pytest-temp-isolated HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/venv/bin/python bash scripts/run_tests.sh -j2 --file-timeout 240 tests/hermes_cli/test_kanban_worker_systemd_scope.py tests/hermes_cli/test_kanban_adaptive_admission.py tests/hermes_cli/test_kanban_worker_scope_resources.py tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py tests/hermes_cli/test_kanban_worker_spawn_toolsets.py tests/hermes_cli/test_kanban_dispatch_lock.py tests/hermes_cli/test_kanban_dispatch_tick_hook.py tests/plugins/test_kanban_dashboard_plugin.py tests/plugins/test_kanban_dashboard_task_updated_hook.py
```

Exit 0: nine files, **210 passed, 0 failed**, 54.7 seconds. Evidence: /home/hermes/exports/hri-direct-update-20260910/r9-owner-final-nine-files.log.

```sh
env -i HOME=/home/hermes PATH=/usr/bin:/bin PYTHONPATH=/home/hermes/.hermes/hermes-agent/.worktrees/hri-semantic-port-r4 /home/hermes/.hermes/hermes-agent/venv/bin/python /home/hermes/exports/hri-direct-update-20260910/r5-owner-safety-probe.py
/home/hermes/.hermes/hermes-agent/venv/bin/python -m py_compile plugins/kanban/dashboard/plugin_api.py
git diff --check
```

All exit 0. Safety evidence: /home/hermes/exports/hri-direct-update-20260910/r9-owner-safety-probe.log. Tests use isolated stores and fake systemd/process interfaces, not live DB/config/service mutation.

This closes the assigned worker-scope/dashboard safety test package, not all HRI preservation. Next: verify remaining previously identified Project/Outcome/owner-wake and non-Kanban preservation against this checkpoint, freeze only concrete remaining repairs, then independently review the immutable complete candidate. No source push/integration, dependency install, runtime activation or service restart occurred. Existing 04:00 release window and rollback/review gates remain; o_3b0a9003 waits for accepted HRI delivery.
