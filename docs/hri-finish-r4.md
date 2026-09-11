# HRI finish R4 handoff

Project `p_155df2bb`; Outcome `o_cde72dc3`; owner `default`; lane `cl_15e90882`; target `telegram:-1003951469776:3`; parent owner execution `ex_6848ea00`.

## Candidate

Base remains `25e15c327e972b3f68949a645999fab3f7b2a73c`, tree `fe2541e6d9f306109b3cb2086b8d9f41de5a1535`. Initially clean, now dirty uncommitted patch. No commit, push, merge, deployment or activation. `hri_accepted=false`; `installed=false`.

Native route: Astra/low root delegated once to native V2 `/root/r4_worker`, role worker, explicit gpt-5.6-luna/xhigh, fork_turns=none; no recursion. Only canonical task name was exposed, not a native UUID. Root inspected the scoped diff and required reversal of an unauthorized process-start parsing hunk; that hunk is absent from the final candidate.

## Criterion disposition

1. Implemented: `_db_opens_cleanly` accepts keyword-only `skip_integrity_check=False`. Only the integrity PRAGMA is conditional; existing journal, read, FTS and transactional probes remain. Full repair-validation default retained.
2. Implemented, proof required: Doctor uses defining modules and defers the scan only for known logical size strictly above the warning threshold without fix; required notice emitted. Threshold, unknown size and fix use the full default. Doctor file did not finish and its initial failure was not diagnosed within this budget.
3. Implemented, proof required: `browser_exec` invokes the existing group-safe runner; duplicate Windows options removed. Existing real execution/death assertions remain unchanged. Native signal guard prevented group-death proof.

No test assertions or fixtures changed. Descendant, root, cancellation and liveness changes are absent. R3 preserved WIP remains unaccepted.

## Verification and evidence limits

Worker reports compile, affected Ruff and diff check PASS; exact commands are in `.local-only/hri-finish-r4/worker-verification-command.txt`. One canonical three-file invocation used `-j 2 --file-timeout 90 --file-retries 0`, the specified interpreter and fixture-owned HOME/TMP/TEMP/TMPDIR. Worker reports exit 1, 142 passed and 5 failed from completed files; Doctor (66 collected) timed out at 90 seconds after `.F......................................`. These are not a complete suite pass count. State repair: 23 pass/2 fail; browser: 119 pass/3 fail. Reported duration 90.1 seconds.

Two state cleanup and two browser group-kill failures hit the test live-system signal guard. A browser lease assertion saw `owner_start_ticks=None`; no liveness fix retained. Doctor's initial failure is unresolved, not classified as environmental. Raw canonical stdout/stderr was not supplied; saved worker result text is a reconstructed report, not an independently checked raw log. Static results are worker-reported. The final candidate includes reversal of the out-of-scope hunk and has no subsequent test run; exact final-byte execution proof is missing.

Coding/testing stopped before the 480-second cutoff. No retry or broader gate ran. Normal worker exit and static checks do not establish acceptance.

## Owner next action

Owner must inspect the exact candidate, perform host-side affected and 108-file combined verification plus unchanged safety probes, resolve observed failures, and checkpoint Git after worker exit. None of those gates is waived. Candidate patch and SHA-256 path mapping are in `.local-only/hri-finish-r4/candidate.patch` and `handoff.json`.
