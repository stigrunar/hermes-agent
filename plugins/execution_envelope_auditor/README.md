# Execution-envelope auditor

This bundled plugin provides a deterministic, read-only companion to the
execution-envelope policy. When enabled, it registers:

```text
hermes execution-envelope-audit [--mode shadow|strict-fixture] [input.json|-]
```

It is also directly runnable without plugin activation:

```text
python -m plugins.execution_envelope_auditor envelope.json
```

`shadow` always exits zero after a valid JSON input, even when findings exist.
It therefore cannot block dispatch. `strict-fixture` exits one when a synthetic
or test receipt has findings. Neither mode writes files, calls an LLM, reads
Kanban, or changes runtime state.

## Input

The JSON object accepts `execution_envelope` and optional `task_metadata`.
Envelope fields follow the execution-envelope contract. Package collision
checks use an optional `active_package` plus `independent_packages`; package
resource lists may include `files`, `contracts`, `schemas`, `runtimes`,
`ports`, `providers`, `side_effects`, `approvals`, and `merge_order`.
Additional packages need:

```json
{
  "independence_evidence": {
    "useful_artifact": true,
    "reduces_lead_time": true,
    "disjoint": true
  }
}
```

Task metadata may name `planned_gates`, `review_trigger`,
`broad_proof_trigger`, `bootstrap_count`, `bootstrap_actions`,
`relevant_toolsets`, `relevant_skills`, `default_model`, `requested_model`,
`model_escalation_reason`, `fan_out_layers`, `contract_id`, and a
`completion_receipt.contract_id`.

## Output and privacy

Output is stable, sorted JSON with a schema version, structural normalized
fields, findings, severities, and counts. Arbitrary outcome, acceptance, scope,
proof, package-resource, and metadata payload text is never echoed. Toolset and
skill identifiers are retained because they are contract field names used to
explain over-broad surface findings; callers must not place private data in
identifier fields.

## Shadow limitations and likely false positives

The auditor intentionally uses deterministic structural and keyword rules.
Before any future enforcement proposal, evaluate these shadow cases:

- An R2 source-only candidate may truthfully exclude deploy proof; a keyword
  check can still flag that exclusion until the actual hard-gate distinction is
  encoded in metadata.
- A broad suite or detached review may be justified by a newly observed blocker;
  provide `broad_proof_trigger`, `review_trigger`, or a per-gate `trigger` so it
  is not classified as speculative.
- Shared labels for separate runtime instances can look like package collisions;
  use instance-qualified resource identifiers.
- A non-default model can be justified by a measured capability blocker; include
  `model_escalation_reason` rather than relying on model naming conventions.
- Relevant tool/skill allowlists are optional. Without them, only wildcard and
  count heuristics are available, so uncommon but legitimate broad tasks may
  receive warnings.

These are report-only findings, not dispatch decisions.
