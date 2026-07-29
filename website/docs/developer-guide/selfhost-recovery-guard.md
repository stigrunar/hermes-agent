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

## Safety contract

The guard fails closed unless all of these are true before candidate activation:

- `gateway_state.json` is fresh and identifies the live systemd `MainPID` with matching process start time;
- `gateway_state=draining`, `drain_quiesced=true`, and all persisted work counters are zero;
- the cross-process active-session registry exists, is structurally valid, and
  has no live leases; every lease must carry a PID plus matching `/proc` start
  ticks, while stale, reused, foreign, or uninspectable identities fail closed;
- `state.db` has no non-expired compression lock;
- every process in the gateway service cgroup has the same PID/start-time identity
  captured before the drain request; a new process, a reused PID, or an
  uninspectable identity fails closed. Unchanged persistent infrastructure is
  allowed only after the independent session, compression-lock, and explicit
  drain-quiescence proofs above are all idle.

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

An artifact's `argv` can use `{artifact}` for its sealed per-run copy. Command proofs can use `{candidate_artifact}` or `{rollback_artifact}` so ownership/service probes are executed by a hash-pinned artifact rather than a mutable helper elsewhere on disk.

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
      "expected": {"release_id": "<exact-candidate-release-id>"},
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
- `events.jsonl` — append-only phase evidence (`armed`, `drain_proved`, command results, rollback start, final)
- `result.json` — atomic final receipt

Final outcomes:

- `activated` (exit 0): candidate identity, endpoint health, and ownership proofs passed
- `rolled_back` (exit 1): candidate failed or timed out; prior runtime and service proofs passed
- `rollback_failed` / `rollback_unavailable` (exit 2): local operator intervention is required

Inspect the durable receipt directly; do not infer success from the gateway process being active or from notifier delivery.
