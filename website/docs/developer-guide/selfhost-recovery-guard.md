---
sidebar_position: 9
---

# Self-hosted recovery guard

`hermes gateway recovery-guard --plan <plan.json>` pre-arms a gateway-independent activation transaction for a Linux systemd-user installation. It is intended for high-risk self-hosted runtime replacement, not routine gateway restart.

The command does not stop or replace the live gateway itself. It:

1. validates and copies the candidate activation artifact, rollback artifact, plan, and supervisor source into `$HERMES_HOME/recovery-guard/<run_id>/`;
2. verifies SHA-256 hashes and makes the copies read/execute-only;
3. launches the copied supervisor as a separate transient systemd-user service;
4. returns only after the supervisor has written a durable `armed` event.

The transient service then owns the whole disruptive sequence. It requests drain for the exact old gateway PID, rejects stale or counter-only drain state, activates the candidate, and either disarms after all mandatory proofs or restores the prior runtime.

Before writing `armed`, the supervisor validates both sealed artifacts, the fresh
live incumbent identity, its service cgroup, and the independent legacy-incumbent
proof. A failure in this pre-arm phase records `prearm_failed` and exits without
requesting drain, invoking candidate activation, or running rollback recovery. The
mandatory legacy-incumbent proof may execute the sealed rollback artifact in proof
mode before arming. The durable `armed` event is written before the drain request;
once that disruptive sequence begins, any later failure retains the fail-closed
verified rollback path.

## Safety contract

The guard has two explicit evidence phases and fails closed unless the applicable
phase contract is satisfied.

During the legacy-incumbent pre-mutation phase, a missing active-session registry
is tolerated only for the built-in audited baseline commit
`150ab8ca4dfecae838119cbba9488c27550dd5f5` and tree
`2b728fa1c71fda2ef4c885284ceda0db25f760ac`. The sealed rollback artifact must
independently inspect the observed live PID/start-time pair and return that exact
source identity together with the same pair. A static source-identity response is
not sufficient. All other evidence remains mandatory:

- `gateway_state.json` is fresh and identifies the live systemd `MainPID` with matching process start time;
- `gateway_state=draining`, `drain_quiesced=true`, and all persisted work counters are zero;
- the atomic split work counters (`active_agents`, `active_cron_jobs`, and
  `active_api_runs`) are valid, sum exactly to `active_work`, and are all zero;
- `state.db` has no non-expired compression lock;
- every process in the gateway service cgroup has the same PID/start-time identity
  captured before the drain request; a new process, a reused PID, or an
  uninspectable identity fails closed. Unchanged persistent infrastructure is
  allowed only after the independent session, compression-lock, and explicit
  drain-quiescence proofs above are all idle. The runtime identity and zero-work
  state are read again after the independent proofs so PID/MainPID drift or work
  reopening fails closed.

Immediately before the candidate artifact is invoked, the supervisor durably
persists and receipts a monotonic phase transition in the sealed run directory.
A reconstructed supervisor reloads that state; a transition receipt whose state
file is absent fails closed. From that point through final disarm, the
cross-process active-session registry is mandatory, structurally valid, and
empty. Missing or malformed evidence, live leases, and stale, reused, foreign,
or uninspectable lease identities fail closed. The registry is checked before
and after candidate proofs and once more immediately before the drain marker is
removed.

A successful candidate must have a new live PID/start-time identity whose command line contains every configured `candidate_runtime_argv_contains` token. The plan must also contain proof roles for:

- `health`
- `candidate_runtime`
- `notifier_owner`
- `dispatcher_owner`

Rollback requires proof roles for:

- `health`
- `prior_runtime`
- `gateway_service`
- `dashboard_service` when `dashboard_unit` is set

`systemctl is-active` is therefore never sufficient by itself.

The gateway materializes this registry at startup, and CLI, TUI, and gateway
turns maintain leases even when `max_concurrent_sessions` is unset or unlimited.
That setting controls admission only; it does not disable recovery evidence.

## Artifact contract

Candidate and rollback artifacts are single, self-contained executables. Their source path and SHA-256 are declared in the plan; the guard copies and re-hashes them before activation. The rollback artifact must restore and restart the prior gateway, plus the dashboard when the plan declares one. It must be safe to invoke after a partial candidate activation.

An artifact's `argv` can use `{artifact}` for its sealed per-run copy. Command proofs can use `{candidate_artifact}` or `{rollback_artifact}` so ownership/service probes are executed by a hash-pinned artifact rather than a mutable helper elsewhere on disk. The legacy-incumbent proof must also contain exactly one `{runtime_pid}` and `{runtime_start_time}` argument. The audited rollback-artifact command must use those values to inspect the current process provenance and print four exact lines: `commit`, `tree`, `pid`, and `start_time`.

Example structure (replace every path, hash, token, and expected value with the exact frozen candidate):

```json
{
  "version": 1,
  "run_id": "hri-r2-20260729-01",
  "state_path": "/home/user/.hermes/gateway_state.json",
  "drain_path": "/home/user/.hermes/.drain_request.json",
  "state_db_path": "/home/user/.hermes/state.db",
  "active_sessions_path": "/home/user/.hermes/runtime/active_sessions.json",
  "gateway_unit": "hermes-gateway.service",
  "dashboard_unit": "hermes-dashboard.service",
  "freshness_seconds": 15,
  "drain_timeout_seconds": 180,
  "readiness_deadline_seconds": 120,
  "rollback_deadline_seconds": 120,
  "poll_seconds": 1,
  "candidate_runtime_argv_contains": ["hermes_cli.main", "gateway", "run"],
  "legacy_incumbent_identity": {
    "commit": "150ab8ca4dfecae838119cbba9488c27550dd5f5",
    "tree": "2b728fa1c71fda2ef4c885284ceda0db25f760ac"
  },
  "candidate_identity": {
    "commit": "<exact-candidate-commit>",
    "tree": "<exact-candidate-tree>"
  },
  "legacy_incumbent_proof": {
    "type": "command_text",
    "role": "legacy_incumbent",
    "argv": [
      "{rollback_artifact}",
      "prove-legacy-incumbent",
      "{runtime_pid}",
      "{runtime_start_time}"
    ],
    "expected_stdout": "commit=150ab8ca4dfecae838119cbba9488c27550dd5f5\ntree=2b728fa1c71fda2ef4c885284ceda0db25f760ac"
  },
  "candidate": {
    "artifact": "/absolute/path/activate-candidate",
    "sha256": "<64 lowercase hex characters>",
    "argv": ["{artifact}", "activate"],
    "timeout_seconds": 120
  },
  "rollback": {
    "artifact": "/absolute/path/restore-prior-runtime",
    "sha256": "<64 lowercase hex characters>",
    "argv": ["{artifact}", "restore"],
    "timeout_seconds": 120
  },
  "success_proofs": [
    {
      "type": "http_json",
      "role": "health",
      "url": "http://127.0.0.1:8642/health",
      "expected": {"status": "ok"}
    },
    {
      "type": "json_file",
      "role": "candidate_runtime",
      "path": "/home/user/.hermes/recovery-identity.json",
      "expected": {
        "release_id": "<exact-candidate-release-id>",
        "source_commit": "<exact-candidate-commit>",
        "source_tree": "<exact-candidate-tree>"
      },
      "freshness_field": "updated_at",
      "max_age_seconds": 15
    },
    {
      "type": "command_text",
      "role": "notifier_owner",
      "argv": ["{candidate_artifact}", "prove-notifier-owner"],
      "expected_stdout": "external-default"
    },
    {
      "type": "command_text",
      "role": "dispatcher_owner",
      "argv": ["{candidate_artifact}", "prove-dispatcher-owner"],
      "expected_stdout": "external-dispatcher"
    }
  ],
  "rollback_proofs": [
    {
      "type": "http_json",
      "role": "health",
      "url": "http://127.0.0.1:8642/health",
      "expected": {"status": "ok"}
    },
    {
      "type": "json_file",
      "role": "prior_runtime",
      "path": "/home/user/.hermes/recovery-identity.json",
      "expected": {"release_id": "<exact-prior-release-id>"},
      "freshness_field": "updated_at",
      "max_age_seconds": 15
    },
    {
      "type": "command_text",
      "role": "gateway_service",
      "argv": ["{rollback_artifact}", "prove-gateway-service"],
      "expected_stdout": "active"
    },
    {
      "type": "command_text",
      "role": "dashboard_service",
      "argv": ["{rollback_artifact}", "prove-dashboard-service"],
      "expected_stdout": "active"
    }
  ]
}
```

## Receipts and outcomes

The run directory is durable and independent of Telegram, the gateway agent loop, and Kanban notifier delivery:

- `plan.json` — sealed plan with per-run artifact paths and owner token
- `events.jsonl` — append-only phase evidence (`armed`, `drain_proved`,
  `evidence_phase_transition`, command results, `candidate_disarm_proved`,
  rollback start, final). The armed event includes both exact source identities,
  the sealed legacy proof, and all drain/readiness/rollback deadlines.
- `result.json` — atomic final receipt

Final outcomes:

- `activated` (exit 0): candidate identity, endpoint health, and ownership proofs passed
- `rolled_back` (exit 1): candidate failed or timed out; prior runtime and service proofs passed
- `prearm_failed` (exit 2): pre-arm validation failed; no drain was requested, candidate activation was not invoked, and rollback recovery did not run. Validation may already have executed the sealed rollback artifact solely for the mandatory legacy-incumbent proof
- `rollback_failed` / `rollback_unavailable` (exit 2): local operator intervention is required

Inspect the durable receipt directly; do not infer success from the gateway process being active or from notifier delivery.
