"""Resolve repository-local execution policy for Kanban tasks.

This module deliberately has no dependency on the Projects or Kanban stores.
Callers pass the canonical primary repository path after resolving a Project.
The resolver is fail-closed: a broken, ambiguous, or missing policy can never
make a requested operation look safer than it is.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml


LIFECYCLE_VALUES = frozenset({"experimental", "active", "maintained", "retired"})
KIND_VALUES = frozenset({
    "local",
    "private_test",
    "private_preview",
    "staging",
    "production",
})
USAGE_VALUES = frozenset({"unused", "internal", "business_active", "customer_active"})
EXPOSURE_VALUES = frozenset({"local", "private", "public"})
CONTINUITY_VALUES = frozenset({"disposable", "restartable", "rollback_required"})
EFFECT_VALUES = frozenset({
    "none",
    "read_only",
    "reversible_write",
    "external_write",
    "destructive",
})
QUALITY_VALUES = ("SPIKE", "FEATURE", "RELEASE")
RISK_VALUES = ("R0", "R1", "R2", "R3")
ACTION_VALUES = frozenset({
    "inspect",
    "test",
    "build",
    "restart",
    "deploy",
    "migrate",
    "write",
    "destructive",
})

_MAX_PROFILE_BYTES = 128 * 1024
_MAX_YAML_TOKENS = 4096
_MAX_YAML_DEPTH = 24
_MAX_YAML_SCALAR_BYTES = 32 * 1024

# A malformed or missing policy is represented by this maximum-protection
# profile. It is intentionally not an ordinary project's default.
_CONSERVATIVE_PROFILE = {
    "lifecycle": "active",
    "kind": "production",
    "usage": "customer_active",
    "exposure": "public",
    "continuity": "rollback_required",
    "effect": "destructive",
}

_KIND_MINIMUMS = {
    "local": ("SPIKE", "R0"),
    "private_test": ("SPIKE", "R0"),
    "private_preview": ("FEATURE", "R2"),
    "staging": ("FEATURE", "R2"),
    "production": ("RELEASE", "R3"),
}
_USAGE_MINIMUMS = {
    "unused": ("SPIKE", "R0"),
    "internal": ("SPIKE", "R1"),
    "business_active": ("FEATURE", "R2"),
    "customer_active": ("RELEASE", "R3"),
}
_EXPOSURE_MINIMUMS = {
    "local": ("SPIKE", "R0"),
    "private": ("SPIKE", "R1"),
    "public": ("RELEASE", "R3"),
}
_CONTINUITY_MINIMUMS = {
    "disposable": ("SPIKE", "R0"),
    "restartable": ("SPIKE", "R1"),
    "rollback_required": ("FEATURE", "R2"),
}
_EFFECT_MINIMUMS = {
    "none": ("SPIKE", "R0"),
    "read_only": ("SPIKE", "R0"),
    "reversible_write": ("FEATURE", "R2"),
    "external_write": ("RELEASE", "R3"),
    "destructive": ("RELEASE", "R3"),
}
_EFFECT_CONTINUITY = {
    "none": "disposable",
    "read_only": "disposable",
    "reversible_write": "restartable",
    "external_write": "restartable",
    "destructive": "rollback_required",
}
_CONTINUITY_ORDER = {
    name: n for n, name in enumerate(("disposable", "restartable", "rollback_required"))
}

_ACTION_MINIMUMS = {
    "inspect": ("SPIKE", "R0", "read_only", "disposable"),
    "test": ("SPIKE", "R0", "read_only", "disposable"),
    "build": ("SPIKE", "R0", "reversible_write", "restartable"),
    "restart": ("FEATURE", "R1", "external_write", "restartable"),
    # A private-test deploy can remain feature-level; environment metadata
    # controls whether rollback proof is required.
    "deploy": ("FEATURE", "R1", "external_write", "restartable"),
    "migrate": ("RELEASE", "R3", "external_write", "rollback_required"),
    "write": ("FEATURE", "R1", "reversible_write", "restartable"),
    "destructive": ("RELEASE", "R3", "destructive", "rollback_required"),
}


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects aliases and explicit tags.

    ``safe_load`` blocks Python object construction but permits aliases. A
    project policy is tiny configuration, so aliases add complexity without
    useful expressiveness and can be used for resource-amplification attacks.
    """


def _bounded_load(raw: str) -> Any:
    token_count = 0
    for token in yaml.scan(raw):
        token_count += 1
        if token_count > _MAX_YAML_TOKENS:
            raise ValueError("profile is too large")
        if token.__class__.__name__ in {"AliasToken", "TagToken"}:
            raise ValueError("aliases and custom tags are not permitted")
    # Compose first lets us enforce depth/scalar bounds before constructing a
    # potentially very broad Python object graph.
    node = yaml.compose(raw, Loader=_StrictSafeLoader)

    def inspect(cur: Optional[yaml.Node], depth: int = 0) -> None:
        if cur is None:
            return
        if depth > _MAX_YAML_DEPTH:
            raise ValueError("profile nesting is too deep")
        if (
            isinstance(cur, yaml.ScalarNode)
            and len(cur.value.encode("utf-8")) > _MAX_YAML_SCALAR_BYTES
        ):
            raise ValueError("profile scalar is too large")
        if isinstance(cur, yaml.MappingNode):
            keys_seen: set[tuple[str, str]] = set()
            for key, value in cur.value:
                key_marker = (
                    key.tag,
                    key.value if isinstance(key, yaml.ScalarNode) else str(key),
                )
                if key_marker in keys_seen:
                    raise ValueError("duplicate mapping key")
                keys_seen.add(key_marker)
                inspect(key, depth + 1)
                inspect(value, depth + 1)
        elif isinstance(cur, yaml.SequenceNode):
            for value in cur.value:
                inspect(value, depth + 1)

    inspect(node)
    return yaml.load(raw, Loader=_StrictSafeLoader)


def _clean_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _norm(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value).strip().lower() or None


def _quality(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip().upper()
    return value if value in QUALITY_VALUES else None


def _risk(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip().upper()
    return value if value in RISK_VALUES else None


def _max_quality(*values: Optional[str]) -> str:
    return max(
        (v for v in values if v in QUALITY_VALUES),
        key=QUALITY_VALUES.index,
        default="SPIKE",
    )


def _max_risk(*values: Optional[str]) -> str:
    return max(
        (v for v in values if v in RISK_VALUES),
        key=RISK_VALUES.index,
        default="R0",
    )


def _minimum_for_profile(profile: Mapping[str, Any]) -> tuple[str, str]:
    quality = _quality(profile.get("quality_mode"))
    risk = _risk(profile.get("risk_tier"))
    for key, table in (
        ("kind", _KIND_MINIMUMS),
        ("usage", _USAGE_MINIMUMS),
        ("exposure", _EXPOSURE_MINIMUMS),
        ("continuity", _CONTINUITY_MINIMUMS),
    ):
        value = profile.get(key)
        if value in table:
            q, r = table[value]
            quality, risk = _max_quality(quality, q), _max_risk(risk, r)
    effect = profile.get("effect")
    if effect in _EFFECT_MINIMUMS:
        q, r = _EFFECT_MINIMUMS[effect]
        quality, risk = _max_quality(quality, q), _max_risk(risk, r)
    return quality or "SPIKE", risk or "R0"


def _diagnostic(code: str, detail: Optional[str] = None) -> str:
    # Never include raw YAML, scalar values, or filesystem contents in a
    # diagnostic. Details here are stable machine-readable labels only.
    return code if not detail else f"{code}:{detail}"


def _profile_from_path(
    repo_path: Optional[str | Path],
) -> tuple[dict[str, Any], Optional[str], Optional[str], list[str], bool]:
    diagnostics: list[str] = []
    if repo_path is None or not str(repo_path).strip():
        return {}, None, None, diagnostics, False
    repo = Path(repo_path).expanduser()
    if not repo.is_absolute():
        # Never turn an untrusted relative task/workspace path into a profile
        # lookup under whichever process cwd happens to be active.
        diagnostics.append(_diagnostic("project_repo_not_absolute"))
        return {}, None, None, diagnostics, False
    profile_path = repo / ".hermes" / "project.yaml"
    digest: Optional[str] = None
    try:
        if not profile_path.is_file():
            return {}, str(profile_path), None, diagnostics, False
        raw_bytes = profile_path.read_bytes()
        if len(raw_bytes) > _MAX_PROFILE_BYTES:
            diagnostics.append(_diagnostic("profile_rejected", "size_limit"))
            return (
                {},
                str(profile_path),
                hashlib.sha256(raw_bytes).hexdigest(),
                diagnostics,
                True,
            )
        # Keep a usable digest even when decoding/parsing fails below.
        digest = hashlib.sha256(raw_bytes).hexdigest()
        raw = raw_bytes.decode("utf-8")
        loaded = _bounded_load(raw)
        if not isinstance(loaded, Mapping):
            diagnostics.append(_diagnostic("profile_rejected", "root_type"))
            return {}, str(profile_path), digest, diagnostics, True
        # Valid profiles hash their canonical data model, so harmless mapping
        # order changes do not produce a different policy identity.
        canonical_profile = json.dumps(
            loaded,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical_profile).hexdigest()
        return dict(loaded), str(profile_path), digest, diagnostics, True
    except (OSError, UnicodeError):
        diagnostics.append(_diagnostic("profile_rejected", "read_error"))
    except Exception:
        diagnostics.append(_diagnostic("profile_rejected", "parse_error"))
    return {}, str(profile_path), digest, diagnostics, True


def _extract_profile(
    raw: Mapping[str, Any], diagnostics: list[str]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    # Accept the natural root form as well as a ``project:`` wrapper. A
    # wrapper is useful when project.yaml later grows unrelated metadata.
    wrapped = raw.get("project")
    if wrapped is not None and not isinstance(wrapped, Mapping):
        diagnostics.append(_diagnostic("profile_invalid", "project_type"))
        return {}, {}
    base = _clean_mapping(wrapped) if isinstance(wrapped, Mapping) else dict(raw)
    declared_version = base.get("version", raw.get("version"))
    if declared_version is not None and str(declared_version).strip() != "1":
        diagnostics.append(_diagnostic("profile_invalid", "version"))
        return {}, {}
    defaults = {
        key: base[key]
        for key in (
            "lifecycle",
            "kind",
            "usage",
            "exposure",
            "continuity",
            "effect",
            "quality_mode",
            "risk_tier",
            "environment",
        )
        if key in base
    }
    declared_defaults = base.get("defaults")
    if declared_defaults is not None and not isinstance(declared_defaults, Mapping):
        diagnostics.append(_diagnostic("profile_invalid", "defaults_type"))
        return {}, {}
    defaults.update(_clean_mapping(declared_defaults))
    environments_raw = base.get(
        "environments",
        base.get("envs", raw.get("environments", raw.get("envs", {}))),
    )
    if environments_raw is None:
        environments_raw = {}
    if not isinstance(environments_raw, Mapping):
        diagnostics.append(_diagnostic("profile_invalid", "environments_type"))
        environments_raw = {}
    environments: dict[str, dict[str, Any]] = {}
    for name, values in environments_raw.items():
        env_name = str(name).strip().lower()
        if not env_name:
            diagnostics.append(_diagnostic("environment_name_invalid"))
            continue
        if not isinstance(values, Mapping):
            diagnostics.append(_diagnostic("environment_entry_invalid"))
            continue
        environments[env_name] = dict(values)
    return defaults, environments


def _proof_policy(
    *, quality: str, risk: str, action: Optional[str], continuity: str
) -> dict[str, Any]:
    """Describe the smallest proof package proportional to the resolved gate."""
    if risk == "R3" or quality == "RELEASE":
        return {
            "scope": "independent_release",
            "build_artifact_check": True,
            "restart": True,
            "sustained_health": True,
            "actual_target_smoke": True,
            "rollback_required": continuity == "rollback_required",
            "rollback_ready": True,
            "rollback_plan": "required_where_meaningful",
            "independent_qa": True,
            "full_suite": True,
        }
    if risk == "R2":
        return {
            "scope": "rollback_ready_integration",
            "build_artifact_check": True,
            "restart": True,
            "sustained_health": True,
            "actual_target_smoke": True,
            "rollback_required": continuity == "rollback_required",
            "rollback_ready": True,
            "rollback_plan": "ready",
            "independent_qa": False,
            "full_suite": False,
        }
    if quality == "SPIKE":
        return {
            "scope": "focused_spike",
            "build_artifact_check": action == "build",
            "restart": action == "restart",
            "sustained_health": False,
            "actual_target_smoke": action in {"inspect", "test", "build"},
            "rollback_required": False,
            "rollback_ready": False,
            "rollback_plan": "not_required",
            "independent_qa": False,
            "full_suite": False,
        }
    if risk == "R1" or quality == "FEATURE":
        return {
            "scope": "private_test_feature",
            "build_artifact_check": True,
            "restart": True,
            "sustained_health": True,
            "actual_target_smoke": True,
            "rollback_required": False,
            "rollback_ready": False,
            "rollback_plan": "optional",
            "independent_qa": False,
            "full_suite": False,
        }
    return {
        "scope": "focused_spike",
        "build_artifact_check": action == "build",
        "restart": action == "restart",
        "sustained_health": False,
        "actual_target_smoke": action in {"inspect", "test", "build"},
        "rollback_required": False,
        "rollback_ready": False,
        "rollback_plan": "not_required",
        "independent_qa": False,
        "full_suite": False,
    }


def resolve_project_execution_policy(
    project_repo: Optional[str | Path],
    execution: Optional[Mapping[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Resolve a deterministic execution preflight result.

    ``None`` is returned only when both the profile and execution request are
    absent. Every other case yields a conservative, serializable mapping.
    """
    has_execution = execution is not None
    request = dict(execution) if isinstance(execution, Mapping) else {}
    profile, profile_path, digest, diagnostics, profile_present = _profile_from_path(
        project_repo
    )
    if not profile_present and not has_execution:
        return None
    if has_execution and not profile_present:
        diagnostics.append(_diagnostic("profile_missing"))

    reasons: list[str] = []
    if profile_present:
        reasons.append(
            "project_profile_loaded"
            if not diagnostics
            else "project_profile_not_usable"
        )
    if has_execution:
        reasons.append("execution_request_present")

    defaults, environments = (
        _extract_profile(profile, diagnostics) if profile else ({}, {})
    )
    requested_environment = _norm(request.get("environment"))
    default_environment = _norm(defaults.get("environment")) or "default"
    environment = requested_environment or default_environment
    selected = dict(defaults)
    if environment in environments:
        selected.update(environments[environment])
        reasons.append(f"environment:{environment}")
    elif environments and environment != "default":
        diagnostics.append(_diagnostic("environment_unknown"))
        selected = dict(_CONSERVATIVE_PROFILE)
        reasons.append("conservative_unknown_environment")

    # Invalid metadata is upgraded to the strongest value for that dimension.
    enum_specs = {
        "lifecycle": (LIFECYCLE_VALUES, "active"),
        "kind": (KIND_VALUES, "production"),
        "usage": (USAGE_VALUES, "customer_active"),
        "exposure": (EXPOSURE_VALUES, "public"),
        "continuity": (CONTINUITY_VALUES, "rollback_required"),
        "effect": (EFFECT_VALUES, "destructive"),
    }
    resolved: dict[str, Any] = {}
    for key, (allowed, fallback) in enum_specs.items():
        value = _norm(selected.get(key))
        if value is None:
            # Missing fields in an otherwise usable profile use conservative
            # protection for safety; a completely absent profile is handled
            # identically when an explicit execution was requested.
            resolved[key] = fallback
            diagnostics.append(_diagnostic("profile_field_missing", key))
        elif value not in allowed:
            resolved[key] = fallback
            diagnostics.append(_diagnostic("profile_field_unknown", key))
        else:
            resolved[key] = value

    requested_action = _norm(request.get("action"))
    action = requested_action
    if action not in ACTION_VALUES:
        if requested_action is not None:
            diagnostics.append(_diagnostic("action_unknown"))
        action = "destructive" if has_execution else "inspect"
    action_quality, action_risk, action_effect, action_continuity = _ACTION_MINIMUMS[
        action
    ]
    if not has_execution:
        # A profile-only task still needs a useful feature-level handoff;
        # SPIKE is reserved for an explicitly requested low-impact action.
        action_quality, action_risk = "FEATURE", "R1"

    # A profile can provide explicit floors in addition to the categorical
    # metadata. Unknown explicit floors are handled as maximum protection.
    profile_quality = _quality(selected.get("quality_mode"))
    profile_risk = _risk(selected.get("risk_tier"))
    if selected.get("quality_mode") is not None and profile_quality is None:
        diagnostics.append(_diagnostic("profile_field_unknown", "quality_mode"))
        profile_quality = "RELEASE"
    if selected.get("risk_tier") is not None and profile_risk is None:
        diagnostics.append(_diagnostic("profile_field_unknown", "risk_tier"))
        profile_risk = "R3"
    minimum_quality, minimum_risk = _minimum_for_profile(resolved)
    floor_quality = _max_quality(minimum_quality, profile_quality, action_quality)
    floor_risk = _max_risk(minimum_risk, profile_risk, action_risk)
    explicit_quality = _quality(request.get("quality_mode"))
    explicit_risk = _risk(request.get("risk_tier"))
    if request.get("quality_mode") is not None and explicit_quality is None:
        diagnostics.append(_diagnostic("quality_mode_unknown"))
        explicit_quality = "RELEASE"
    if request.get("risk_tier") is not None and explicit_risk is None:
        diagnostics.append(_diagnostic("risk_tier_unknown"))
        explicit_risk = "R3"
    if diagnostics and action in {
        "restart",
        "deploy",
        "migrate",
        "write",
        "destructive",
    }:
        # Malformed or unknown metadata may not make a mutating operation look
        # safer than it is. Preserve diagnostics, but fail closed on the gate.
        floor_quality = "RELEASE"
        floor_risk = "R3"
        reasons.append("conservative_mutation_floor")
    resolved_quality = _max_quality(floor_quality, profile_quality, explicit_quality)
    resolved_risk = _max_risk(floor_risk, profile_risk, explicit_risk)

    continuity = resolved["continuity"]
    effect = resolved["effect"]
    if "conservative_mutation_floor" in reasons:
        continuity = "rollback_required"
    if (
        effect in _EFFECT_CONTINUITY
        and _CONTINUITY_ORDER[_EFFECT_CONTINUITY[effect]]
        > _CONTINUITY_ORDER[continuity]
    ):
        continuity = _EFFECT_CONTINUITY[effect]
        reasons.append("effect_continuity_floor")
    if (
        action in {"migrate", "destructive"}
        and _CONTINUITY_ORDER[action_continuity] > _CONTINUITY_ORDER[continuity]
    ):
        continuity = action_continuity
        reasons.append(f"action_continuity_floor:{action}")
    if resolved_quality != explicit_quality and explicit_quality is not None:
        reasons.append("quality_floor_applied")
    if resolved_risk != explicit_risk and explicit_risk is not None:
        reasons.append("risk_floor_applied")

    continuity_proof = {
        "disposable": "none",
        "restartable": "restart",
        "rollback_required": "rollback",
    }[continuity]
    proof_policy = _proof_policy(
        quality=resolved_quality,
        risk=resolved_risk,
        action=requested_action,
        continuity=continuity,
    )
    input_repo: Optional[str] = None
    if project_repo is not None:
        candidate = Path(str(project_repo)).expanduser()
        if candidate.is_absolute():
            input_repo = str(candidate)
    result = {
        "version": 1,
        "mode": "policy" if profile_present else "conservative_missing_profile",
        "inputs": {
            "project_repo": input_repo,
            "environment": requested_environment,
            "action": request.get("action"),
            "quality_mode": request.get("quality_mode"),
            "risk_tier": request.get("risk_tier"),
        },
        "resolved": {
            **resolved,
            "environment": environment,
            "action": requested_action,
            "quality_mode": resolved_quality,
            "risk_tier": resolved_risk,
            "continuity": continuity,
            "continuity_proof": continuity_proof,
            "effect": effect,
            "action_effect": action_effect,
        },
        "proof_policy": proof_policy,
        "floor": {
            "quality_mode": floor_quality,
            "risk_tier": floor_risk,
            "continuity": continuity,
            "effect": effect,
        },
        "ceiling": {
            "quality_mode": "RELEASE",
            "risk_tier": "R3",
            "continuity": "rollback_required",
            "effect": "destructive",
        },
        "profile_path": profile_path,
        "profile_digest": digest,
        "reasons": reasons,
        "diagnostics": diagnostics,
    }
    return result


def canonical_execution_preflight(value: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Return stable compact JSON for persistence, or ``None``."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_execution_preflight(value: Any) -> Optional[dict[str, Any]]:
    if not value:
        return None
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


# Compatibility aliases for callers that prefer the shorter terminology.
resolve_execution_policy = resolve_project_execution_policy
serialize_execution_preflight = canonical_execution_preflight
