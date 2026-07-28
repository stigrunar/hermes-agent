# DollyArchitect hardening candidate

Status: inactive candidate. The repository now contains a separately reviewed
generic runtime-policy seam, but this candidate is still inert until DollyOps
copies the immutable overlay into one named profile and adds the explicit
profile-local activation config below. No live profile is changed by this
candidate.

The candidate accepts only explicit architecture work tags: ontology,
scenario grammar, code-versus-data boundaries, shared harness/adapter/UI
primitives, cross-repository contracts, migration seams, and reuse proven by
a materially different second scenario. Implementation, evaluation,
benchmarking, routine QA/review, visual design, PR, release, and deployment
tags are deterministically rerouted to the named specialist role.

Scratch is the default workspace. Writes require a resolved
`HERMES_KANBAN_WORKSPACE` and explicit, existing artifact roots that resolve
strictly beneath it. Worktree mode is the narrow exception: a valid contract
must name exactly one architecture-document path, and the guard permits only
that resolved document while rejecting source suffixes and obvious source
directories.

The staged capability set is read/search, knowledge/code intelligence,
Kanban, session search, and guarded assigned-artifact writing. Terminal is
disabled because this overlay cannot enforce command, working-directory, and
resolved-path allowlisting together. Normal cron, delegation, computer use,
media/image, GitHub write, and deploy/release capability remain disabled.

Hindsight remains disabled because `HINDSIGHT_API_KEY` and
`HINDSIGHT_LLM_API_KEY` are unavailable. Knowledge and session search provide
candidate continuity. The profile remains `gpt-5.6-sol`, reasoning `high`,
with normal `max_turns` 60. The concrete staged exclusions are
`contract-driven-frontend-implementation`, `mobile-ui-verification`,
`release-candidate-evidence`, and `external-upstream-pr-recuts`. They are
profile-local only and never inspect or mutate shared skills.

An accepted architecture decision packet is data distinct from its emitted
DollyCode handoff contract. The pure transformation always returns one
handoff and an empty implementation-action collection.

## Dispatch and handoff formats

A DollyArchitect Kanban task body must contain exactly one block with these
literal delimiters (ordinary explanatory text may appear outside the block):

```text
<!-- HERMES_ARCHITECT_DISPATCH_V1
{"contract_id":"...","work_kind":"cross_repo_contract","workspace_kind":"scratch","writable_artifact_roots":["/absolute/workspace/artifacts"],"architecture_document_paths":[],"requested_actions":["architecture_decision"],"implementation_owner":null,"operations_owner":null}
HERMES_ARCHITECT_DISPATCH_V1 -->
```

The dispatcher validates the JSON before `Popen`, including work-kind fit,
task/contract workspace agreement, execution-action contradictions, model
override, and the reviewed pure contract invariants. An implementation work
kind names `DollyCode` in its rejection; release, PR, and deploy work name
`DollyOps`.

The one permitted `kanban_create` call accepts exactly the fields `title`,
`assignee`, and `body`; `assignee` must be `dollycode`, and `body` must contain
exactly one decision packet:

```text
<!-- HERMES_ARCHITECTURE_DECISION_V1
{"packet_id":"...","decision":"...","rationale":"...","constraints":["..."],"acceptance_criteria":["..."],"dollycode_owner":"DollyCode"}
HERMES_ARCHITECTURE_DECISION_V1 -->
```

The runtime replaces that body with canonical
`HERMES_DOLLYCODE_HANDOFF_V1` JSON containing
`implementation_actions: []`, binds the child to the architect task, denies a
second handoff, and denies `kanban_complete` until the first create succeeds.
`kanban_block` remains available for a real blocker.

## Reviewed activation (DollyOps only, later)

The reviewed overlay files are `__init__.py`, `hardening.py`,
`measurement_schema.json`, and `profile.json`. After substituting the actual
Hermes data root for `/srv/hermes`, DollyOps must:

1. Confirm `HERMES_PROFILE=dollyarchitect` will resolve to exactly
   `/srv/hermes/profiles/dollyarchitect`.
2. Create `/srv/hermes/profiles/dollyarchitect/runtime_policy/dollyarchitect`
   and copy only the four reviewed files there, preserving their reviewed
   bytes. Do not copy the writable install manifest as an integrity source.
3. Add this profile-local configuration to
   `/srv/hermes/profiles/dollyarchitect/config.yaml`, preserving any existing
   provider credentials unchanged:

```yaml
model:
  default: gpt-5.6-sol
agent:
  max_turns: 60
  reasoning_effort: high
  runtime_policy:
    id: dollyarchitect.v1
    enabled: true
telegram:
  dm_policy: allowlist
  allow_from:
    - "<one-or-more-private-human-user-ids>"
  group_policy: disabled
```

4. Review the installed bytes against the code-owned SHA-256 pins in
   `agent/profile_runtime_policy.py`. A missing file, extra file, symlink,
   edited manifest, config drift, identity mismatch, or hash mismatch fails
   closed. Then run a profile-load/schema smoke and a non-spawning rejected
   dispatch smoke before allowing a real architect card.

The loader also rejects activation when the inherited, existing
`TELEGRAM_ALLOW_BOTS` setting normalizes to `mentions` or `all`. Safe states
are unset or `none`. This is an activation check on Telegram's existing
runtime control, not a new environment variable or gateway behavior.

The internal `HERMES_INTERNAL_RUNTIME_POLICY` child payload is produced only
by the dispatcher. It is not user configuration and must never be written to
`.env` or `config.yaml`.

Rollback order is exact: set `agent.runtime_policy.enabled` to `false` (or
remove the two-field mapping), stop scheduling new DollyArchitect work, verify
the profile no longer loads the policy, then remove only
`runtime_policy/dollyarchitect`. Restore the prior profile-local config/overlay
snapshot if one existed. No shared skills, default profile, board tasks, or
credentials are changed during rollback.
