# HRI tools R14 source-slice receipt

Status: `source_slice_verified`

This slice restores the accepted Kanban creation contract preparation and
forwards the existing persisted Project/Outcome, conversation-lane, mutation,
capability, execution, and roadmap-binding fields through the registered
`kanban_create` handler. `kanban_show` and `kanban_list` now project the native
identity/preflight fields. Bound task subscriptions use the persisted lane
target and retain an existing passive subscription policy.

## Verification

Exact required focused proof (exit 0):

```text
env -i HOME=/home/hermes PATH=/usr/bin:/bin TEMP=/home/hermes/exports/hri-direct-update-20260910/pytest-temp-isolated HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/venv/bin/python bash scripts/run_tests.sh -j2 --file-timeout 180 tests/tools/test_kanban_tools.py tests/tools/test_kanban_create_conversation_roundtrip.py tests/tools/test_kanban_provenance.py tests/tools/test_kanban_redaction.py tests/hermes_cli/test_kanban_roadmap_binding.py
```

Result: exit `0`; 5 files, `86 passed`, `0 failed`, no deselection.

Changed-Python compile (exit `0`):

```text
/home/hermes/.hermes/hermes-agent/venv/bin/python -m py_compile tools/kanban_tools.py tests/tools/test_kanban_tools.py tests/tools/test_kanban_create_conversation_roundtrip.py
```

Diff hygiene (exit `0`):

```text
git diff --check
```

Tested revision/state: `46fddcd392231233091c3239c6b7dfcc5f37e75a`; working tree
dirty with the four scoped R14 paths uncommitted. The shared Git common-dir is
read-only for this route, so no commit was created.

## Independent owner verification

Owner matched all four dirty blobs to the receipt, inspected the actual
handler/schema/subscription/serialization delta and compared the compact
contract validators with the exact accepted reference. Role aliases and
required execution-list constants are unchanged. No existing test assertion
was removed or weakened.

The combined unfiltered owner 14-file gate plus four public-tool test files
passes: **18 files, 345 passed, 0 failed**, exit 0 in 78.2s
(`r14-owner-preservation.log`). The previously deferred review-contract test
is now green. All four immutable worktree/intent/lease/scope-WAL falsifiers
pass. Compile and whitespace checks pass.

Additional independent real-registry proof in
`test_r14_owner_public_boundary.py`: **20 passed**, exit 0 in 7.58s.
It covers all four valid role packets, all missing-role gates before
persistence, capability/resource and exact creation/show/list readback,
malformed capability/execution and conflicting bindings, mismatched/dirty
review proof, and top-level/nested roadmap and execution preflight roundtrip.
All source systems are temporary fixtures, with no live network/model/board.
Exact logs and probe JSON are in the existing HRI exports directory.

Owner verdict: **source_slice_verified**, not full HRI acceptance.
A fresh separate descendant baseline still reports 6 passed, 2 failed across
2 files in 23.7s. These are the explicitly deferred descendant authority and
read-context preservation cases; native consumer calls already converge on
`agent/delegation_context.py`, while its helper currently fences delegated
ContextVars only and strips the required read-context keys. That same helper
is present in the accepted reference, so the failure is not claimed to be
introduced by R14. The next bounded correction must reuse that shared helper.

## Separate gates

R14's assigned independent diff/public-boundary and preservation gates are
complete above. Later source changes require candidate-bound revalidation.
Descendant-environment, migration, gateway, lifecycle/goal, full release,
live, rollback, commit, and integration gates remain separate; this is not a
full HRI release claim.
