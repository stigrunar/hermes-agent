# HRI finish R2 — cancellation correction

Project `p_155df2bb`; Outcome `o_cde72dc3`; owner Dolly/default; lane `cl_15e90882`; target `telegram:-1003951469776:3`.

This same-Outcome owner replan addresses the runtime claim and final-response ingress cancellation gaps. It does not change R3-C1–C4 or claim complete HRI acceptance. The owner retains the final combined 108-file gate, unchanged safety probes, exact independent review, and subsequent integration/activation decisions.

## Source identity and authority

Starting HEAD: `c51c5e4d5367f7c4703eea52549ca860dd30b58a`; tree: `c1ff08c2c14d4be75d4ec0f75069b34f8b8d7017`. The initial working tree was clean. The existing App Server liveness change and all paths outside this package are preserved. The output is deliberately uncommitted: owner Git handling occurs after this process exits.

Native root `/root` uses the selected `gpt-6-astra/low`; one native leaf `/root/cancellation` was explicitly dispatched as `worker`, `gpt-5.6-luna/xhigh`, `fork_turns=none`. These are the native IDs exposed by the collaboration API; no additional runtime UUID is asserted. The worker owns the five allowed implementation/test paths; the root owns this document and the local handoff. No recursive delegation or Kanban mutation occurred.

## Evidence

The final local receipt is `.local-only/hri-finish-r2/handoff.json`; it binds changed-file SHA-256 hashes, exact commands, results and logs. This document will be finalized with the observed implementation and proof before handoff.

## Implementation and limits

The candidate replaces polling/`uncancel()` in the shared ledger wait with a shielded result wait that retains cancellation until the in-flight operation completes. Runtime recovery retains the result of a threaded claim, waits for resume clearing, and compensates unsent claims. Network sends remain cancellable; an in-flight send with no observed ACK remains ambiguous rather than being falsely acknowledged or immediately retried. A received ACK is persisted before cancellation propagates. Final-response ingress uses the same ledger seam and marks cancelled unsent persistence as retryable; flood finalization retains native timer scheduling.

These are source changes, **not verified closure of the two cancellation gaps**. New regressions were authored but could not be executed under the required fixture preflight. The requested deterministic red-before-repair sequence was therefore not achieved. Static inspection and compilation are not substitutes for those proofs.

## Fixture blocker

The supplied writable root `/var/tmp/hri-finish-r2-auiwa8et` exposes an empty read-only `.git` directory inside the native sandbox, alongside `.codex` and `.agents`. `/var/tmp`, `/var`, and `/` have no `.git`, but the root itself fails the explicit no-ancestor-`.git` requirement. `.local-only/hri-finish-r2/environment-preflight.json` records the check. The owner was asked for a compliant writable root during this run; no permission widening, configuration change, directory removal, prompt-builder change, or assertion weakening was attempted.

The canonical race/affected delivery commands remain NOT_RUN pending that fixture correction. The owner’s recovered 155/0 host result predates this patch and is not attributed to this candidate. The 108-file gate and immutable safety probes were not run in this execution.

## Changed paths and static verification

- `gateway/run_startup.py`: shielded ledger result handling, runtime claim compensation, and unsent batch cleanup.
- `gateway/platforms/base.py`: shared ledger seam for ingress persistence/finalization, unsent-write compensation, and flood scheduling after cancelled finalization.
- `tests/gateway/test_delivery_ledger.py`: authored runtime claim/repeated-cancel, resume-clear, and active-send/unsent-tail regressions.
- `tests/gateway/test_telegram_deferred_delivery.py`: authored threaded ingress record, ACK, and flood-finalization regressions.
- `docs/hri-finish-r2.md`: this receipt.

`tests/gateway/test_delivery_flood_invariants.py` is unchanged and remains in the planned affected gate. The resume-clear regression uses an async event-controlled clear seam, not a real threaded session-store write; it does not independently prove that full I/O path. None of the six new tests has an execution receipt.

Root checks on the frozen Python source all exited 0:

```text
/home/hermes/.hermes/hermes-agent/venv/bin/python -m py_compile gateway/run_startup.py gateway/platforms/base.py tests/gateway/test_delivery_ledger.py tests/gateway/test_telegram_deferred_delivery.py
/home/hermes/.hermes/hermes-agent/venv/bin/ruff check gateway/run_startup.py gateway/platforms/base.py tests/gateway/test_delivery_ledger.py tests/gateway/test_telegram_deferred_delivery.py tests/gateway/test_delivery_flood_invariants.py
git diff --check
```

Exact output is in `.local-only/hri-finish-r2/root-static-checks.json`. The final manifest and patch are bound by `handoff.json`. Working tree: dirty/uncommitted on the starting HEAD. No commit, push, integration, activation, service action, live database access, network send, or configuration/credential change occurred.

## Remaining owner action

Resolve the writable Git-free fixture boundary, then establish deterministic base failures and candidate passes for the new regressions, including the full threaded resume-clear path, and execute the specified three-file affected gate. Inspect/fix any failures before accepting the patch. The owner can then perform its intentional Git phase and the already-required combined gate and exact independent review. This handoff preserves a candidate for review; it does not certify the two gaps closed.
