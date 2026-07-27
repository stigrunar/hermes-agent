"""Deterministic, read-only execution-envelope auditing.

The auditor deliberately emits only structural facts and policy findings. It
never echoes outcome, acceptance, scope, proof, package-resource, or metadata
payload text, so receipts can be retained without copying private task data.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any

QUALITY_MODES = ("SPIKE", "FEATURE", "RELEASE")
RISK_TIERS = ("R0", "R1", "R2", "R3")
REVIEW_POLICIES = ("owner_closeout", "one_exact_candidate", "release_gate")
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

_HARD_GATE_TERMS = (
    "auth",
    "credential",
    "destructive",
    "deploy",
    "external",
    "human approval",
    "migration",
    "money",
    "payment",
    "privacy",
    "public",
    "rollback",
    "r2",
    "r3",
)
_RELEASE_FULL_PROOF_TERMS = ("full", "suite", "integration", "end-to-end", "e2e")
_RELEASE_REVIEW_TERMS = ("exact", "candidate", "independent review", "release review")
_RELEASE_RUNTIME_TERMS = ("deploy", "rollback", "live", "actual target", "release artifact")
_ACCEPTANCE_STOP_TERMS = ("acceptance", "accepted", "proof passes", "checks pass")
_BLOCKER_STOP_TERMS = ("blocker", "authority", "resource", "safety")
_BROAD_NAMES = {"*", "all", "everything", "full", "default", "core"}
_PACKAGE_DIMENSIONS = (
    "files",
    "contracts",
    "schemas",
    "runtimes",
    "ports",
    "providers",
    "side_effects",
    "approvals",
    "merge_order",
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return sorted({_text(item) for item in value if _text(item)}, key=str.casefold)


def _lower_names(value: Any) -> list[str]:
    return sorted({item.casefold() for item in _string_list(value)})


def _contains_any(values: Iterable[str], terms: Iterable[str]) -> bool:
    text = "\n".join(values).casefold()
    return any(term.casefold() in text for term in terms)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finding(code: str, severity: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "path": path, "message": message}


def _findings_sorted(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(
        findings,
        key=lambda item: (
            SEVERITY_ORDER[item["severity"]],
            item["code"],
            item["path"],
            item["message"],
        ),
    )


def _resource_values(package: Mapping[str, Any], dimension: str) -> list[str]:
    value = package.get(dimension, [])
    if isinstance(value, str):
        value = [value]
    return _lower_names(value)


def _normal_path(value: str) -> str:
    normalized = str(PurePosixPath(value.replace("\\", "/")))
    return normalized.rstrip("/") or "."


def _paths_overlap(left: list[str], right: list[str]) -> bool:
    for raw_left in left:
        left_path = _normal_path(raw_left)
        for raw_right in right:
            right_path = _normal_path(raw_right)
            if left_path == right_path:
                return True
            if left_path != "." and right_path.startswith(left_path + "/"):
                return True
            if right_path != "." and left_path.startswith(right_path + "/"):
                return True
    return False


def _package_collisions(
    packages: list[Mapping[str, Any]],
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for left_index, left in enumerate(packages):
        for right_index in range(left_index + 1, len(packages)):
            right = packages[right_index]
            dimensions: list[str] = []
            for dimension in _PACKAGE_DIMENSIONS:
                left_values = _resource_values(left, dimension)
                right_values = _resource_values(right, dimension)
                overlap = (
                    _paths_overlap(left_values, right_values)
                    if dimension == "files"
                    else bool(set(left_values) & set(right_values))
                )
                if overlap:
                    dimensions.append(dimension)
            if dimensions:
                findings.append(
                    _finding(
                        "package_independence_collision",
                        "error",
                        f"execution_envelope.packages[{left_index},{right_index}]",
                        "Packages share independence-sensitive resources: "
                        + ", ".join(dimensions)
                        + ".",
                    )
                )
    return findings


def _independence_is_explicit(package: Mapping[str, Any]) -> bool:
    evidence = package.get("independence_evidence")
    if isinstance(evidence, bool):
        return evidence
    if not isinstance(evidence, Mapping):
        return False
    return all(
        evidence.get(key) is True
        for key in ("useful_artifact", "reduces_lead_time", "disjoint")
    )


def _model_fields(metadata: Mapping[str, Any]) -> tuple[str, str, str]:
    model = _as_mapping(metadata.get("model"))
    requested = _text(model.get("requested") or metadata.get("requested_model"))
    default = _text(model.get("default") or metadata.get("default_model"))
    reason = _text(model.get("escalation_reason") or metadata.get("model_escalation_reason"))
    return requested, default, reason


def _gate_entries(metadata: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    entries: list[Mapping[str, Any]] = []
    for item in metadata.get("planned_gates", []) if isinstance(metadata.get("planned_gates"), list) else []:
        if isinstance(item, str):
            entries.append({"type": item})
        elif isinstance(item, Mapping):
            entries.append(item)
    return entries


def audit_execution_envelope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic shadow report for one proposed envelope.

    This function is pure: it performs no I/O, does not call an LLM, and cannot
    block or mutate dispatch/runtime state.
    """

    root = _as_mapping(payload)
    envelope = _as_mapping(root.get("execution_envelope", root.get("envelope", root)))
    metadata = _as_mapping(root.get("task_metadata", root.get("metadata", {})))
    findings: list[dict[str, str]] = []

    quality_mode = _text(envelope.get("quality_mode")).upper()
    risk_tier = _text(envelope.get("risk_tier")).upper()
    review_policy = _text(envelope.get("review_policy")).casefold()
    acceptance = _string_list(envelope.get("acceptance"))
    scope_in = _string_list(envelope.get("scope_in"))
    scope_out = _string_list(envelope.get("scope_out"))
    proof_required = _string_list(envelope.get("proof_required"))
    proof_not_required_present = "proof_not_required" in envelope
    proof_not_required = _string_list(envelope.get("proof_not_required"))
    stop_when = _string_list(envelope.get("stop_when"))
    tools = _lower_names(envelope.get("tools_required"))
    skills = _lower_names(envelope.get("skills_required"))
    independent_raw = envelope.get("independent_packages", [])
    independent = (
        [item for item in independent_raw if isinstance(item, Mapping)]
        if isinstance(independent_raw, list)
        else []
    )
    active_package = _as_mapping(envelope.get("active_package"))
    packages = ([active_package] if active_package else []) + independent

    if quality_mode not in QUALITY_MODES:
        findings.append(_finding("invalid_quality_mode", "error", "execution_envelope.quality_mode", "quality_mode must be SPIKE, FEATURE, or RELEASE."))
    if risk_tier not in RISK_TIERS:
        findings.append(_finding("invalid_risk_tier", "error", "execution_envelope.risk_tier", "risk_tier must be R0, R1, R2, or R3."))
    if not _text(envelope.get("outcome")):
        findings.append(_finding("missing_outcome", "error", "execution_envelope.outcome", "A non-empty observable outcome is required."))
    if not acceptance:
        findings.append(_finding("missing_acceptance", "error", "execution_envelope.acceptance", "At least one falsifiable acceptance condition is required."))
    if not scope_in:
        findings.append(_finding("missing_scope_in", "error", "execution_envelope.scope_in", "scope_in must be explicit."))
    if not scope_out:
        findings.append(_finding("missing_scope_out", "error", "execution_envelope.scope_out", "scope_out must be explicit."))
    if not proof_required:
        findings.append(_finding("missing_proof", "error", "execution_envelope.proof_required", "At least one acceptance- or risk-linked proof is required."))
    if not proof_not_required_present:
        findings.append(_finding("missing_proof_exclusion", "error", "execution_envelope.proof_not_required", "proof_not_required must be present, even when empty."))
    if review_policy not in REVIEW_POLICIES:
        findings.append(_finding("invalid_review_policy", "error", "execution_envelope.review_policy", "review_policy must be owner_closeout, one_exact_candidate, or release_gate."))

    has_acceptance_stop = _contains_any(stop_when, _ACCEPTANCE_STOP_TERMS)
    has_blocker_stop = _contains_any(stop_when, _BLOCKER_STOP_TERMS)
    if not has_acceptance_stop or not has_blocker_stop:
        findings.append(_finding("missing_stop_condition", "error", "execution_envelope.stop_when", "stop_when must include successful acceptance and genuine blocker semantics."))

    if quality_mode == "SPIKE" and risk_tier in {"R2", "R3"}:
        findings.append(_finding("mode_risk_mismatch", "warning", "execution_envelope", "SPIKE with R2/R3 risk requires an explicit higher-risk envelope justification."))
    if risk_tier in {"R2", "R3"} and review_policy == "owner_closeout":
        findings.append(_finding("mode_risk_mismatch", "error", "execution_envelope.review_policy", "R2/R3 work cannot rely on owner_closeout alone."))
    if quality_mode == "SPIKE" and review_policy == "release_gate":
        findings.append(_finding("mode_risk_mismatch", "warning", "execution_envelope.review_policy", "A SPIKE should not carry a release gate without an explicit trigger."))

    release_requirements = (
        ("release_full_verification_missing", _RELEASE_FULL_PROOF_TERMS, "RELEASE requires relevant full or integration verification."),
        ("release_exact_review_missing", _RELEASE_REVIEW_TERMS, "RELEASE requires exact-candidate independent review."),
        ("release_runtime_proof_missing", _RELEASE_RUNTIME_TERMS, "RELEASE requires deploy, rollback, live, actual-target, or release-artifact proof."),
    )
    if quality_mode == "RELEASE":
        for code, terms, message in release_requirements:
            if not _contains_any(proof_required, terms):
                findings.append(_finding(code, "error", "execution_envelope.proof_required", message))
        if review_policy != "release_gate":
            findings.append(_finding("release_review_policy_mismatch", "error", "execution_envelope.review_policy", "RELEASE requires review_policy=release_gate."))

    hard_bypass = risk_tier in {"R2", "R3"} and _contains_any(proof_not_required, _HARD_GATE_TERMS)
    if hard_bypass:
        findings.append(_finding("hard_gate_bypass", "error", "execution_envelope.proof_not_required", "proof_not_required cannot bypass an R2/R3 or hard authority boundary."))

    review_trigger = _text(metadata.get("review_trigger"))
    broad_proof_trigger = _text(metadata.get("broad_proof_trigger"))
    if quality_mode == "FEATURE" and risk_tier in {"R0", "R1"}:
        if review_policy != "owner_closeout" and not review_trigger:
            findings.append(_finding("speculative_review_gate", "warning", "execution_envelope.review_policy", "Routine FEATURE review escalation lacks a named trigger."))
        if _contains_any(proof_required, ("full suite", "all tests", "all viewport", "security review", "design review")) and not broad_proof_trigger:
            findings.append(_finding("speculative_broad_proof", "warning", "execution_envelope.proof_required", "Routine FEATURE broad proof lacks a named trigger."))

    for gate_index, gate in enumerate(_gate_entries(metadata)):
        gate_type = _text(gate.get("type")).casefold().replace("-", "_").replace(" ", "_")
        trigger = _text(gate.get("trigger"))
        if gate_type in {"detached_review", "qa", "review", "design_review", "security_review", "full_suite"} and quality_mode == "FEATURE" and not trigger and not review_trigger:
            findings.append(_finding("speculative_review_gate", "warning", f"task_metadata.planned_gates[{gate_index}]", "Planned review/proof gate lacks a named FEATURE trigger."))
        if gate_type in {"deploy", "deployment", "live_proof", "release"} and quality_mode != "RELEASE" and not trigger:
            findings.append(_finding("speculative_deploy_gate", "warning", f"task_metadata.planned_gates[{gate_index}]", "Planned deploy/release gate lacks a named promotion trigger."))

    if len(independent) > 2 or len(packages) > 3:
        findings.append(_finding("too_many_packages", "error", "execution_envelope.independent_packages", "An envelope may contain one active package and at most two additional packages."))
    for index, package in enumerate(independent):
        if not _independence_is_explicit(package):
            findings.append(_finding("missing_independence_evidence", "error", f"execution_envelope.independent_packages[{index}]", "Additional packages require useful-artifact, lead-time, and disjointness evidence."))
    findings.extend(_package_collisions(packages))

    fan_out_layers = metadata.get("fan_out_layers", envelope.get("fan_out_layers", 0))
    if quality_mode == "FEATURE" and isinstance(fan_out_layers, int) and fan_out_layers > 1:
        findings.append(_finding("stacked_fan_out", "warning", "task_metadata.fan_out_layers", "Routine FEATURE work should use no more than one fan-out layer."))

    bootstrap_count = metadata.get("bootstrap_count", 0)
    raw_bootstrap_actions = metadata.get("bootstrap_actions", [])
    bootstrap_actions = (
        [_text(item).casefold() for item in raw_bootstrap_actions if _text(item)]
        if isinstance(raw_bootstrap_actions, list)
        else []
    )
    repeated_action = any(count > 1 for count in Counter(bootstrap_actions).values())
    if (isinstance(bootstrap_count, int) and not isinstance(bootstrap_count, bool) and bootstrap_count > 1) or repeated_action:
        findings.append(_finding("repeated_bootstrap", "warning", "task_metadata.bootstrap", "Repository/context bootstrap is repeated without a declared identity or contract change."))

    relevant_tools = set(_lower_names(metadata.get("relevant_toolsets")))
    relevant_skills = set(_lower_names(metadata.get("relevant_skills")))
    tool_extras = set(tools) - relevant_tools if relevant_tools else set()
    skill_extras = set(skills) - relevant_skills if relevant_skills else set()
    if set(tools) & _BROAD_NAMES or tool_extras or len(tools) > 8:
        findings.append(_finding("over_broad_toolset_request", "warning", "execution_envelope.tools_required", "Requested toolsets exceed the declared minimum relevant surface."))
    if set(skills) & _BROAD_NAMES or skill_extras or len(skills) > 5:
        findings.append(_finding("over_broad_skill_request", "warning", "execution_envelope.skills_required", "Requested skills exceed the declared minimum relevant surface."))

    requested_model, default_model, escalation_reason = _model_fields(metadata)
    if requested_model and default_model and requested_model.casefold() != default_model.casefold() and not escalation_reason:
        findings.append(_finding("unnecessary_model_escalation", "warning", "task_metadata.model", "A non-default model was requested without a measured blocker or escalation reason."))

    contract_id = _text(metadata.get("contract_id"))
    completion = _as_mapping(metadata.get("completion_receipt"))
    completion_contract_id = _text(completion.get("contract_id") or metadata.get("completion_contract_id"))
    if contract_id and completion_contract_id and contract_id != completion_contract_id:
        findings.append(_finding("completion_contract_mismatch", "error", "task_metadata.completion_receipt.contract_id", "Completion receipt does not echo the proposed contract id."))

    findings = _findings_sorted(findings)
    counts = Counter(item["severity"] for item in findings)
    normalized = {
        "acceptance_count": len(acceptance),
        "has_blocker_stop": has_blocker_stop,
        "has_outcome": bool(_text(envelope.get("outcome"))),
        "has_success_stop": has_acceptance_stop,
        "independent_package_count": len(independent),
        "package_count": 1 + len(independent),
        "proof_not_required_count": len(proof_not_required),
        "proof_not_required_present": proof_not_required_present,
        "proof_required_count": len(proof_required),
        "quality_mode": quality_mode or None,
        "review_policy": review_policy or None,
        "risk_tier": risk_tier or None,
        "scope_in_count": len(scope_in),
        "scope_out_count": len(scope_out),
        "skills_required": skills,
        "tools_required": tools,
    }
    return {
        "schema_version": 1,
        "mode": "shadow",
        "valid": not findings,
        "normalized_envelope": normalized,
        "findings": findings,
        "summary": {
            "error": counts["error"],
            "warning": counts["warning"],
            "info": counts["info"],
            "total": len(findings),
        },
    }
