# HRI finish R3 — preservation correction

Project `p_155df2bb`; Outcome `o_cde72dc3`; owner Dolly/default; lane `cl_15e90882`; target `telegram:-1003951469776:3`.

## Authority and source

This is the owner-replanned successor to implementation `ex_55178d41`, owner verification `ex_b18a9ef9`, and timed-out review `ex_879b2cca`. The review exited without a verdict and is not source approval. R3-C1–C4 and fixed upstream cutoff `4a76e99f876494c7a848f5298ea82a9d7b9d2204` remain unchanged.

The initial clean HEAD is `e8b95858fc0a6a5875a564f13e027b65afb7abcb`, tree `cbc0b7d54c4244f8a166a6f3a869d5771962df05`. This execution returns an uncommitted patch. Git metadata is protected; no commit, integration, activation, or runtime acceptance is claimed. `hri_accepted=false`; `installed=false`.

Native route: root `/root`, selected `gpt-6-astra/low`; one native V2 implementation leaf `/root/preservation`, explicitly dispatched as `worker`, `gpt-5.6-luna/xhigh`, `fork_turns=none`. These are the identifiers exposed by the collaboration API; no unexposed runtime UUID is asserted. The child owns the bounded implementation and directly affected tests; the root owns final inspection and this handoff. No recursive delegation or Kanban mutation is authorized.

## Evidence boundary

Owner evidence at `/home/hermes/exports/hri-finish-r2-owner/combined-summary.json` records 108 completed files, 2445 passed, 29 failed, 9 skipped, with no unfinished files or timeouts. Those counts describe the base, not this patch. The supplied owner cancellation evidence is 72 passing tests, six regressions failing on preceding source, and seven passing safety probes; those checks are preserved and not rerun here.

Implementation evidence and exact commands are recorded under `.local-only/hri-finish-r3/`. The final `handoff.json` binds changed paths to SHA-256 hashes and `candidate.patch`. Fixture HOME/TMP/TEMP/TMPDIR use the granted `/var/tmp/hri-finish-r3-m9bw3zlu` root. The seven failing files do not require Git-free ancestry; prompt-discovery tests are outside this package.

## Candidate results

Four Python paths changed:

- `agent/delegation_context.py`: restores ambient and explicit TASK triggers for the existing descendant fence. Parent mappings remain unmodified; scrubbing remains prefix-based with the existing read-location allowlist.
- `plugins/memory/hindsight/__init__.py`: rejects root during local-embedded availability and initialization without starting the daemon; the daemon entry point retains the same safety check. Tools mode remains lazy and default auto-retain remains off.
- `tests/test_state_db_malformed_repair.py`: synthetic repair fixtures declare a known-empty holder view, including the fixture-owned repair subprocess. The integrity-probe test imports the defining module. Production holder scans, WAL policy, occupied/unknown/permission denial, and signal-based fixture cleanup remain unchanged.
- `tests/tools/test_browser_use_cli.py`: death assertion distinguishes a missing process or Linux zombie from a live process. Permission and malformed PID-state reads fail closed; non-Linux keeps a process-existence check. The timeout and process-group assertions remain. No production browser edit or unrelated process kill was made.

The worker reports a passing three-file F1 gate: 103 passed, 3 skipped, exit 0. Its malformed-repair diagnostic used `-j 1` on an intermediate fixture with cooperative cleanup that was subsequently reverted, so it is not proof of final bytes; it reported 24 passed and one missing-keyword failure. The browser diagnostic also used `-j 1` and timed out at 90.1 seconds, with no selected tests completed. These diagnostic results are not full acceptance, and intermediate-source results must not be attributed to the final patch.

Root compilation, affected Ruff and `git diff --check` passed, exit 0. Exact expanded commands and tested Python hashes are in `root-static-checks.json`. The final seven-file canonical command and raw log are `seven-command.json` and `seven.log`: exit 1, 116.30 seconds; **248 passed, 2 failed, 3 skipped, 2 file timeouts**. All seven files reached terminal runner results; five completed tests and two timed out with no tests run. Doctor and Hindsight each timed out at 90.1 seconds. No passing Hindsight/root-guard runtime claim is made.

Final per-file results: doctor TIMEOUT; Hindsight TIMEOUT; malformed repair 24 passed / 1 failed; browser 121 passed / 1 failed; delegate isolation 7 passed; descendant scope 2 passed; local environment blocklist 94 passed / 3 skipped. The malformed failure is the absent `skip_integrity_check` parameter. The browser failure retains `assert self._pid_terminated(pid)`; the reported PID was 1324, and the helper returned false. This result does not prove a leaked fixture-owned child or a sandbox root cause; no action was taken against that PID. Host-side diagnosis remains required. The exact failures are preserved in the raw final log.

The four Python hashes still match the root static-check snapshot. Protected cancellation files and the App Server liveness test match preflight hashes; final diff inspection shows no edits outside the four Python files and this document.

## Concrete owner boundaries

1. `hermes_cli/doctor_state.py` calls `_db_opens_cleanly(state_db_path)` unconditionally; it does not select the large-DB threshold behavior required by the existing test. The fixture already patches the defining modules. Production doctor source is outside this package, so the assertion is retained.
2. `hermes_state_repair.py::_db_opens_cleanly` accepts only `db_path`; the existing test's `skip_integrity_check=True` contract is absent. Production repair source is outside this package. The defining-module correction exposes this mismatch rather than removing the assertion.
3. Browser actual process-group/death proof is NOT_VERIFIED where the sandbox blocks signaling or the file times out. No permission workaround, timeout increase, skip or production accommodation was added. Owner must run the exact affected files on the host.

This is a partial source handoff with remaining failing/unverified gates, not preservation acceptance.

## Remaining owner gates

Dolly/default performs clean host-side affected and combined checks, unchanged safety probes, and independent exact-candidate source review. The owner commits unchanged verified bytes after this execution exits. Integration, activation, rollback and real-target live proof remain separate unwaived gates.
