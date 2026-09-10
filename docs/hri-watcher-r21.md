# HRI R21 — native watcher preservation

Project `p_155df2bb`, Outcome `o_cde72dc3`; base `3cb023a591e6eb615b150356be6074beec3972c5`.

## Result

The native split watcher now uses the established hashed, profile-isolated notifier lock; automatic decomposition defaults off when unspecified. Dispatcher admission precedes singleton locking, settings side effects and board opening. One prepared admission object passes unchanged by identity through the native dispatcher, together with canonical caps and allowed worker profiles. No parallel policy or state store was added.

The orphan tests now separate real bounded maintenance (`max_new_spawns=0`) from read-only preview. Real reconciliation retains its original row/result assertions; the new preview check preserves source rows/events and forbids spawning. Split-module fixture targets were corrected without adding production test-only branches. Existing adaptive-admission and systemd-scope safety tests were unchanged.

## Evidence and execution distinction

Implementation `ex_52cd4fcb` reached its 1200-second deadline without a final receipt and remains failed/non-retryable. Its in-scope patch was retained, not blindly retried. Owner verification/checkpoint execution `ex_885b669a` inspected the exact five changed source/test files and supplied the missing proof after the writer stopped. All tracked source hashes remained unchanged throughout final verification.

Owner commands, from the bound worktree:

- `bash /home/hermes/exports/hri-direct-update-20260910/r17-owner-notify-f0aff00a82/scoped-gate.sh`: exit 0; all 19 files completed, 126 passed, no failures or timeouts, 122.7 seconds.
- `bash /home/hermes/exports/hri-direct-update-20260910/r20-test-placement/owner-29-file-gate.sh`: exit 0; 552 passed, zero failed, six platform skips, 169.3 seconds. Includes the unchanged adaptive-admission and systemd-scope safety files.
- Independent public-registry/environment-boundary files: 30 passed, exit 0.
- Six unchanged worktree, owner-intent, lease, scope/WAL, native-admission and replan-recovery probes: all exit 0.
- Changed Python compilation and `git diff --check`: pass.

Exact commands, full logs, probe hashes, source hashes and native execution snapshots are retained in `/home/hermes/exports/hri-direct-update-20260910/r21-watcher-preservation/`. The earlier owner diagnostic also passed outside the Codex sandbox; worker test/receipt failure is not silently relabelled as successful implementation execution.

## Boundary

This is a verified internal-upgrade source correction, not an installed upgrade or public product release. No integration, source push, activation, restart, service/config/auth change or live business-data write occurred. Full HRI acceptance still requires its remaining relevant preservation checks, independent exact-candidate review, native-update behavior and rollback-backed actual-target verification. The dependent auth repair remains deferred until HRI delivery is accepted. Reuse these unchanged-byte receipts; do not repeat verified work merely to create more process stages.
