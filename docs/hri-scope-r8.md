# HRI R8 bounded repair

This slice restores exact-scope cleanup fencing for dashboard direct status
moves and isolates worker-spawn argv/parser tests from ambient systemd probes.

## Scope

- _set_status_direct releases only the exact current run after deterministic
  refusal guards, preserves status/run identity when scope cleanup is unknown
  or fails, and uses compare-and-set status/current-run fencing.
- Confirmed scoped cleanup does not signal a historical host PID.
- Spawn argv/toolset/parser tests fake the dispatcher’s scope-argv seam; the
  dedicated systemd scope tests retain their own lifecycle fakes.
- Added isolated dashboard coverage for unknown/failed cleanup refusal and no
  PID signalling.

## Proof state

Owner terminal verification supersedes the provisional text below. Execution
`ex_7aaf6c66` reached its 900.047-second deadline without a native final receipt.
Owner reviewed the exact four-file diff and reran all nine required files on
those bytes: **207 passed, 3 failed, exit 1**, in 52.5 seconds. The dashboard
scope-release regression and both spawn-test fixture failures are fixed, and
the added unknown/failed-cleanup cases pass. The seven-file safety subset has
no failures. Scope/WAL owner probe, compilation and `git diff --check` pass.

The three remaining failures are in dashboard dispatch admission: absent
query override widens the canonical cap; DB initialization precedes admission;
and the prepared snapshot is not forwarded by identity. All three also fail
on the immutable R8 base `fc0e3953f23894d74296fe9c8473bdc74a187685`
(40 passed, 3 failed across the two dashboard files, 32.6 seconds). They predate
this R8 edit but remain HRI integration blockers, NOT accepted-runtime behavior.
No green nine-file gate or HRI acceptance is claimed. Owner will admit one
bounded correction of that endpoint rather than retrying terminal R8.

Evidence under `/home/hermes/exports/hri-direct-update-20260910/`:
`r8-owner-final-nine-files.log`, `r8-dashboard-baseline-fc0.log`,
`r8-owner-safety-probe.log`. No live service, database or configuration was
changed. The text below is retained as the original worker proof plan.


The required nine-file canonical test run, the isolated owner-safety probe,
affected-file compilation, and git diff --check are the acceptance checks
for this bounded slice. Results are recorded in the execution receipt; this
document does not claim HRI release acceptance.
