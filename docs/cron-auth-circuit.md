# Cron primary-auth circuit — current owner revision

## Current revision: CRON-FRICTION-AUTH-CIRCUIT-R2-REVIEW2

- Classification: `useful_incomplete_patch` — the useful inherited R2 implementation remains, but run 863 produced **no new correction patch**.
- State: `prepared_not_dispatched`; owner replan only, not execution or release admission.
- Project `p_155df2bb`; Outcome `o_3b0a9003`; owner Dolly/default; board `hermes`.
- `continuation_of=t_d8d4dd7f`; terminal run `863`, prior contract `CRON-FRICTION-AUTH-CIRCUIT-R2-REVIEW1` / `r1`, reason `iteration_exhausted`, resume policy `never`.
- Fingerprint: `07647454531713e76e7ffe828384dd1b4d45d22807b811a7731170e683c95407`.
- Wake field remains `topic_target=unknown` as supplied. Native task creation and needs_owner_replan events resolve `telegram:-1003951469776:3`, lane `cl_15e90882`; this verified resolution must be used by a future admission.

### Preserved run 863 evidence

Worktree `/home/hermes/.hermes/hermes-agent/.worktrees/t_d8d4dd7f`, branch `hermes-agent/t_d8d4dd7f-cron-auth-review1`, is clean at inherited commit `3780a1e88e37aa48d3641e0bafb3487f61e209a1`, tree `75d738712c3bbc123f82a4d6cac03a4e62f9e4ac`. There is no diff, no new implementation commit, and `git ls-remote origin refs/heads/hermes-agent/t_d8d4dd7f-cron-auth-review1` returned no ref. This is the previously rejected R2 candidate, not a newly completed one.

The worker log at `/home/hermes/.hermes/kanban/boards/hermes/logs/t_d8d4dd7f.log` records repeated discovery, an ambiguous duplicate scheduler hunk, atomic patch rejection with no files changed, then 60/60 iteration exhaustion. No implemented repair or passing correction proof exists. Log text proposing a session resume is historical tool output, not authority to resume the terminal revision.

Owner re-ran the fake-only regression in a temporary HERMES_HOME with canonical venv Python, PYTHONPATH set to the preserved worktree, and `python -m pytest -o addopts= -q /tmp/test_cron_auth_owner_review.py`: **4 failed in 6.26s**. The enclosing diagnostic shell later exited zero because hashes were printed; the pytest result is failing, not green. The three excluded quota/auxiliary/delivery results still open the primary circuit; late automatic failure still steals the manual probe. Prior broad cron evidence remains 1150 passed, 1 failed, 1 skipped; not rerun by this replan.

Preserved file SHA-256 values:
- `cron/auth_circuit.py`: `0ad312ee637f4f1554fe436ac2d5b7fc33ba8b376d36bac993a79c3e06f6046d`
- `cron/scheduler.py`: `849b5f38b4da8bd3960105c9bedb4877d811a37e8ed4932297597ef93673faa8`
- `tests/cron/test_cron_auth_circuit.py`: `c7434c3a5e10ec1689d2379502170982f93c1af2585b7a8e8e5e1741c7635c64`
- `tests/cron/test_preflight_config.py`: `8d1fff440b38929b2a6a716eae8923cd58e3f2c616f41dee81ca3a2d266de856`

### Frozen continuation and one next action

**One next action:** Dolly/default must prepare and admit one bounded correction execution under this same Outcome from the preserved exact candidate, with the four failing regressions as the first implementation checkpoint and unique-context edits rather than a large ambiguous patch. Before that admission, place the regression fixture in a durable shared evidence path (the worker's separate PrivateTmp namespace could not directly read the owner's `/tmp` path), verify fresh base/ownership and exact route, and supply the settled existing-native credential/provenance evidence instead of sending a leaf back through broad discovery. This one-shot does not dispatch that execution.

All A1–A5 criteria from REVIEW1 remain binding: same-scope suppression across reload and OAuth refresh; correct primary provenance and exclusions; one alert per generation; exclusive manual recovery closed only by genuine primary inference; safe durable state without credentials or secret logging. The first checkpoint does not waive stable OAuth/callable/pool identity or primary-versus-fallback success. Empty output alone is not proof no primary inference occurred. Remaining scope is `cron/`, `tests/cron/`, `docs/cron-auth-circuit.md`; any demonstrated need for provider-core mutation requires a new owner scope decision before edits.

Dependencies remain unchanged: no task parents or children. Source proof, exact committed candidate, independent review, and separate private activation/rollback/actual-target gates remain required. Stig's standing activation approval is retained but does not approve this rejected candidate. No terminal task is unblocked/retried, no same worker/Architect/detached QA/new graph is spawned, and no merge, push, deployment, config/auth/runtime mutation occurs in this replan.

Canonical documentation base was clean `7b23500d33b2d8d1cf673895c8e67573d2f8733e`. The only active same-repo execution observed, `ex_9029812e`, owns four AGENTS/reference files, not this document. This local documentation-only receipt does not alter the preserved candidate or establish remote publication.

## Historical revision: CRON-FRICTION-AUTH-CIRCUIT-R2 (superseded)

The following sections preserve the earlier replan; their next-action wording is historical, not a second current instruction.

- Classification: `useful_incomplete_patch`.
- State: `prepared_not_dispatched`; this document is a continuation packet, not execution admission.
- Owner: Dolly/default. Project: `p_155df2bb`. Outcome: `o_3b0a9003` (`CRON-FRICTION-REPAIR-R1`).
- `continuation_of=t_dd85f25b`; terminal run `861`, contract `task:t_dd85f25b/r1`, reason `iteration_exhausted`, resume policy `never`.
- Replan fingerprint: `8a32ba26aabcae5a7291aafbe8fb8851461c5ae0810c2e4a1f5d4a1d5048725f`.
- Wake supplied `topic_target=unknown`. Native task creation and needs_owner_replan events resolve the actual target to `telegram:-1003951469776:3`, lane `cl_15e90882`. Preserve both the original unknown field and this verified resolution; do not dispatch with an unknown target.

## Preserved artifact and proof

Preserved worktree: `/home/hermes/.hermes/hermes-agent/.worktrees/t_dd85f25b`.
Branch: `hermes-agent/t_dd85f25b-cron-reliability-scoped-terminal-auth-ci`.
HEAD/source base: `af22785ea73d909e81c3e16aa7a38fa27b45671d`.
HEAD tree: `bdcf8990229f5ea668309a6ab628395238d17596` (base tree only, NOT a patch candidate).

The worker made no implementation commit. `git ls-remote origin refs/heads/hermes-agent/t_dd85f25b-cron-reliability-scoped-terminal-auth-ci` returned no ref. No pushed candidate exists on that branch. Fresh fetch observed origin/main `7de89b6d8dcae89dbf591f3f918944854ecfc9a6`; the preserved base is not silently upgraded or deployed.

Preserved files (SHA-256 before owner documentation):

- Modified `cron/scheduler.py`: `6243bf795432745501f4704cef5d0dc335c6158628539407cf8d354181690cf3` (74 added lines).
- Untracked `cron/auth_circuit.py`: `234111750b3857e1f98478e74ddfccbbd7160ae44643a75186ac364cf8523336`.
- Untracked `.repro_auth.py`: `01369fce27d98da35b4799cff4c705275410282b463fcb6316736eb54985efa9` (preserved evidence, outside intended production mutation scope).

Owner verification on 2026-09-09:

- AST parsing of all three files: PASS. `git diff --check`: PASS.
- Ran `.repro_auth.py` through `/home/hermes/.hermes/hermes-agent/venv/bin/python` with temporary HOME/HERMES_HOME and PYTHONDONTWRITEBYTECODE=1. It uses a fake AIAgent, fake runtime provider, and fake session database. No real inference or credential recovery was performed.
- Process exit 0, but acceptance FAIL: both first_error and second_error were `RuntimeError: HTTP 401 unauthorized: invalid api key`; `primary_agent_calls: 2`. Exit 0 is only script completion, not a passing regression.
- Worker log `/home/hermes/.hermes/kanban/boards/hermes/logs/t_dd85f25b.log` independently records unfinished primary-result wiring and missing tests/commit at lines 447–463.
- No new regression tests under tests/cron, no complete focused-suite receipt, and no committed candidate. Worker reported four Windows msvcrt type diagnostics; not independently rerun here.

## Bounded remaining package

Retain the useful durable state, locking, admission and sanitized-state work, but complete and verify it before calling it a candidate:

1. Wire terminal PRIMARY inference failures after provider resolution into opening the circuit, and genuine successful primary recovery into closing the admitted probe. Provider-resolution success alone is not inference success.
2. Complete manual-run plumbing and quiet suppression: automatic skips must not repeatedly enter failure delivery; one incident notification per open generation.
3. Prove effective credential-scope isolation rather than assuming requested provider/endpoint equals resolved credential identity. Preserve unrelated scopes and no_agent sensors.
4. Prove persistence, classification exclusions, cross-process concurrent manual recovery and stale-probe behavior. The existing TTL alone does not prove an active long-running probe cannot be stolen.
5. Turn the isolated reproduction into committed regression coverage under tests/cron; keep raw scratch evidence preserved until an admitted worker explicitly relocates it. Resolve scoped type/lint issues, run affected tests, and freeze an exact local commit/tree with actual outputs.

## One next action

Dolly/default must admit this R2 completion packet under the SAME Outcome after revalidating current canonical base and non-overlapping ownership; then complete the bounded missing wiring/test/proof package from the preserved artifact. This one-shot replan does not launch a worker, retry/resume R1, or create a successor/root graph. R2 is prepared, not runnable. Fresh admission must carry continuation identity, resolved lane/target, the original required capabilities (local_file_hash, local_file_read, terminal, workspace_access), and the original mutation boundary.

Frozen scope remains `cron/`, `tests/cron/`, `docs/cron-auth-circuit.md`, `ROADMAP.md`, `TASKS.md`. Frozen exclusions remain quota/transient/auxiliary/delivery failures, unrelated provider/profile scopes, credentials, fallback, secret logging, live cron/config/runtime changes, and external Git push.

Dependencies and gates are unchanged: the source task has no parent or child tasks. Existing owner-delivered no-change monitor/config repair is not duplicated. Source implementation proof must precede the retained owner independent control-plane review and separate exact release/runtime proof; no review or activation approval is supplied here. HRI owner-wake activation and other Outcomes remain untouched.

## Documentation publication boundary

This current revision is recorded locally in the canonical source checkout, separate from the preserved dirty implementation worktree. External push, merge and deployment are not authorized by this replan. Local documentation is not a published implementation candidate; later source admission must reconcile the current remote base and carry this receipt forward without overwriting sibling work.
