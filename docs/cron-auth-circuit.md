# Cron primary-auth circuit — current owner revision

## Current revision: CRON-FRICTION-AUTH-CIRCUIT-R2

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
