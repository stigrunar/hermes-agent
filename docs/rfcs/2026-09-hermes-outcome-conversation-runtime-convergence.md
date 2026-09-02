# Hermes downstream convergence and outcome execution model

Status: implementation in progress
Date: 2026-09-02
Owner: downstream Hermes maintainers
Pilot: Hovewest Prosjektstyring

## Decision

Hermes runtime is a deployed artifact, never a development branch or source of unique application code. The canonical code authority for this installation is the maintained downstream repository line. Upstream changes are synchronized into that line; Hove West-specific changes do not wait for upstream acceptance.

Normal software work is organized as Project -> Outcome -> Execution. Conversation surfaces are lanes bound to a project and optionally an outcome. A Telegram topic, Hermes Group Chat, or other conversation lane carries context and projection only; it does not implicitly acquire code authority.

A material outcome has at most one active mutating execution for an overlapping repository/path scope. Other projects, topics, bots, QA actors, and dependency owners may observe, request, review, or depend on the outcome without creating a competing mutation.

Direct Codex is the default implementation inner loop for a bounded feature. Kanban remains available for durable scheduling, cross-authority handoff, long-lived waiting work, or genuinely independent execution, but a normal feature must not automatically expand into owner -> architect -> controller -> code -> QA graphs.

## Canonical convergence baseline

Observed before this change:

- effective dispatcher runtime: `stig/release/hri-r5-8daae18a` at `8daae18adf37b866453fdda566cc7d4fc5365b86`;
- that runtime is one downstream overlay commit on upstream snapshot `ca9952cbb3087564fb19b5e26a0bb3564a1a3693` from 2026-08-31;
- upstream `origin/main` at implementation start: `254158f4530cada634c4ef8f4cff93257c5b4f77`;
- the effective runtime overlay merged with that upstream tip with no unresolved file conflicts;
- legacy `stig/main` at `e7d2f72280cfa72eaba4a16ac2ce678b6be085f9` is not a safe cutover target because it has its own older divergence;
- the older local release line `release/v0206-local-runtime-notifier-r1-20260828` is also not canonical and must become historical evidence after cutover.

The implementation branch `codex/outcome-conversation-lanes-r1` is based on the effective `hri-r5` overlay and merged current upstream before feature work. No live runtime is mutated by development on this branch.

## Runtime delta classification

Every downstream-only change needed after upstream synchronization is classified as one of:

- `LOCAL_PERMANENT`: installation/product behavior intentionally maintained downstream;
- `UPSTREAM_CANDIDATE`: generic Hermes improvement used downstream immediately and optionally proposed upstream;
- `TEMPORARY_COMPAT`: bounded workaround with an explicit removal condition;
- `DROP`: historical runtime behavior that must not survive canonical convergence.

No class may exist only as an unversioned runtime patch.

### Current downstream delta classification

The current maintained downstream line is `origin/main` plus a small downstream commit series. The legacy effective HRI behavior is preserved by `8daae18adf` and the Outcome/project changes are separate commits after it. Classification is by behavior family rather than pretending the historical squashed overlay has one semantic purpose:

| Behavior family | Class | Current handling / removal condition |
| --- | --- | --- |
| HRI/Kanban task lifecycle, owner-replan, iteration-exhaustion semantics, review lifecycle, project execution policy, remote Codex host routing, worker scope/resource fencing | `LOCAL_PERMANENT` | Maintained as downstream operating behavior until an upstream equivalent is deliberately adopted and parity-tested. |
| Project -> Outcome -> Execution model, conversation lanes, mutation leases, cross-project dependencies, current-state projection, outcome-first prompt/routing | `LOCAL_PERMANENT` | Hove West downstream product/operating-model capability. No upstream acceptance dependency. |
| Telegram deferred-delivery/error normalization, delivery-ledger reliability, doctor bounded-state probes, worker env scrub, lazy memory/browser workers and related generic robustness fixes inside the HRI overlay | `UPSTREAM_CANDIDATE` | Used downstream immediately. May be proposed upstream independently; remove the local delta only after an upstream revision is proven behaviorally equivalent. |
| Hindsight/provider and local operator integration adjustments carried by the HRI overlay | `LOCAL_PERMANENT` | Retained while this installation uses the local integration contract; upstreamable pieces may be split later without blocking runtime convergence. |
| Compatibility-only source patches | `TEMPORARY_COMPAT` | **None currently required as unique runtime source.** Any future compat patch must name an explicit upstream/version removal condition and still live in downstream Git. |
| `release/v0206-local-runtime-notifier-r1-20260828`, older topic/release/runtime fix branches, direct runtime-only source variants | `DROP` | Historical/rollback evidence only after cutover. Never eligible as a new development base or source-selection candidate. |

This classification deliberately allows downstream divergence. The failure mode being removed is not "different from upstream"; it is "different from our own canonical downstream source".

### Convergence verification evidence

As of 2026-09-02:

- current upstream `origin/main` is an ancestor of maintained `stig/downstream/main`;
- the maintained downstream line is 15 commits ahead of the inspected upstream tip before the final documentation-only adjustment;
- the effective HRI overlay merged onto the 2026-09-02 upstream tip without unresolved source conflicts;
- targeted Outcome/Project/Kanban/Group Chat/gateway suites passed 99/99 tests;
- the broader Projects/Kanban regression selection passed 131 tests with 1 skip and exactly two `projects.tree` failures;
- those same two failures reproduce unchanged on pre-Outcome baseline `a73e7834cf`, so they are accepted unrelated baseline regressions rather than migration blockers.

## Data model

### Project

Existing first-class Project remains the durable product/workspace identity.

### Outcome

An Outcome is a material user result inside a Project. It has stable identity, state, visible owner, current source/candidate/live references, frozen acceptance, and next action. Outcome state is current-state coordination data, not a replacement for Git, runtime, or source-system truth.

### Conversation lane

A Conversation Lane binds a conversation coordinate to a Project and optionally an Outcome. Supported coordinates are generic: platform + chat/room + optional thread. A project may have many lanes; an outcome may be projected into multiple lanes.

Conversation binding never grants mutation authority.

### Mutation lease

An active mutation lease contains:

- project/outcome identity;
- repository identity/path;
- conservative path-scope list;
- execution owner identity;
- source base reference;
- acquisition/release timestamps.

Acquiring an overlapping active lease for another execution fails closed. Scope comparison is deliberately conservative: uncertain wildcard overlap is treated as overlap.

## Outcome packet

For material software outcomes, the human-readable projection lives at:

`docs/outcomes/<outcome-key>/`

with:

- `00-status.md` — materialized current state;
- `01-outcome.md` — user result, frozen acceptance and non-goals;
- `02-system-fit.md` — repos, data/source ownership, runtime and side-effect boundaries;
- `03-execution.md` — current tracer bullet/slices, mutation ownership, tests and release path;
- `receipts/` — exact candidate/review/deploy/actual-target evidence.

`00-status.md` is a projection of authoritative data and must not become an independent planning database.

## Execution policy

Default bounded feature flow:

Outcome -> one coherent Codex edit/test loop -> immutable candidate -> proportional independent acceptance when material -> guarded deploy/live proof when material.

Architect is triggered by unresolved structural/system-boundary uncertainty, not by feature existence. Controller/replan is triggered by a real authority, dependency, source-freshness or collision boundary, not as a standard stage.

Before mutation, execution must bind a fetched canonical source base. Before candidate freeze, relevant source freshness is checked again. Material movement on overlapping paths invalidates or rebases the same outcome execution; it does not create a new competing graph by default.

## Cross-project dependency rule

A project may request or depend on another project's outcome without acquiring its mutation scope.

Pilot invariant:

- HWStaffing may require `STAFFING-TEST-ENABLER-R1` and project its status in the HWStaffing conversation;
- Prosjektstyring owns the `/bemanning` composition/product surface;
- HWStaffing requirement or conversation activity must not create a second Prosjektstyring `/bemanning` mutator while the outcome lease is active.

## Conversation topology

Small projects may remain topics in a shared `Dolly Projects` forum/group.

A project with multiple simultaneously material workstreams may be promoted to a dedicated project group/forum. The Prosjektstyring pilot topology is:

- Control / status
- Salg
- Bemanning
- Utleie
- Equipment
- Plattform

The same project/outcome identities are also usable by Hermes.app hosted Group Chats so project bots can collaborate without inventing a second project model.

## Related systems that must converge

Cutover is incomplete until the following stop forcing the legacy topology:

- first-class project DB/RPC/API;
- Kanban task identity and create paths;
- dispatcher admission/collision logic;
- Codex routing and source preflight;
- hosted Group Chat room identity;
- Telegram/topic routing and notifier projection;
- project/outcome status projection;
- root/profile AGENTS and relevant bundled/local skills;
- project/roadmap/grill/spec/review skills that create execution graphs;
- observers/cron jobs that infer current source from local release branches or stale task graphs;
- update/conformance checks that currently tolerate unique runtime code.

Migration must search these surfaces explicitly. A code change is not complete while an active skill/config can automatically reconstruct the legacy path.

## Phases

### Phase 1 — canonical downstream convergence — source convergence implemented; runtime proof pending cutover

1. Rebase/merge the effective `hri-r5` overlay onto current upstream.
2. Run focused and broad tests for overlay-owned systems.
3. Classify remaining downstream delta.
4. Publish one maintained downstream branch/ref and make it the source for future releases.
5. Verify runtime can be reproduced from exact repository commit + documented config, with no unique application source in runtime.

### Phase 2 — Outcome core and mutation ownership — implemented

1. Add Outcome and Conversation Lane tables/APIs to Projects.
2. Add mutation lease storage and conservative overlap enforcement.
3. Add `outcome_id` to task identity and validate Project/Outcome consistency.
4. Allow a bounded execution to acquire/release a lease and surface collisions before worker startup.
5. Add tests for same-outcome, cross-outcome and cross-project collisions.

### Phase 3 — execution simplification and skills migration — implemented in canonical source/skills

1. Make direct bounded execution the normal feature route.
2. Change automatic task-graph creation to trigger-based behavior.
3. Update AGENTS, bundled skills and installation-local skills that encode the old owner/controller/review topology.
4. Route cross-project work as dependency/request by default.
5. Make current source base mandatory for mutating executions and fail early on stale source.

### Phase 4 — project conversations and projection — implemented except Telegram group provisioning rollout

1. Bind Hermes Group Chats to project/outcome identity.
2. Add generic conversation-lane APIs usable by Telegram/forum topics and app rooms.
3. Materialize a one-screen project Control projection from Outcomes.
4. Pilot Prosjektstyring workstreams and HWStaffing dependency projection.
5. Preserve existing topic coordinates as aliases/projections during migration rather than treating them as execution owners.

### Phase 5 — release cutover — pending

1. Run release doctor/full targeted suites from canonical downstream source.
2. Build one versioned release from an exact downstream commit.
3. Activate through the existing rollback-safe release path.
4. Verify every running Hermes process reports the expected code SHA.
5. Archive old runtime/release branches as evidence; do not delete rollback evidence needed for recovery.

## Frozen acceptance

The migration is accepted only when all of these hold:

1. An exact canonical downstream Git commit reproduces the application code running in Hermes; runtime contains no unique source patch.
2. Upstream acceptance is not a prerequisite for Hove West-specific functionality.
3. Projects may have multiple concurrent outcomes and conversation lanes.
4. Telegram topic or app room identity does not implicitly decide source, outcome owner, or mutation authority.
5. Overlapping repository/path mutations cannot run concurrently under different active execution leases.
6. HWStaffing can depend on and discuss Prosjektstyring `/bemanning` without spawning a competing Prosjektstyring implementation.
7. A normal feature can execute as one coherent Codex inner loop and does not require a generated multi-agent graph.
8. Related skills/config/dispatch surfaces have been migrated or explicitly disabled; no active rule silently recreates the legacy flow.
9. Existing guarded deploy, rollback, credential, public-exposure and destructive-action boundaries remain intact.
10. Prosjektstyring can expose separate Salg and Bemanning conversation lanes plus a coherent project Control view.

## Non-goals for this migration

- deleting historical Kanban rows;
- mass-deleting old Git branches/worktrees unless required for safe cutover;
- weakening release rollback or actual-target verification;
- forcing every small project into a dedicated chat group;
- requiring upstream PR acceptance before using downstream fixes;
- replacing Git, source systems, runtime health or release receipts with an Outcome document.
