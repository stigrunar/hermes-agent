"""Profile-local runtime policy loading and mandatory tool enforcement.

The seam is intentionally generic at its call sites.  The only policy
implemented here is the reviewed DollyArchitect policy; every other profile
returns ``None`` and retains the normal Hermes runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import types
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from hermes_constants import get_hermes_home
POLICY_ID = "dollyarchitect.v1"
PROFILE_NAME = "dollyarchitect"
OVERLAY_RELATIVE_PATH = Path("runtime_policy") / PROFILE_NAME
INTERNAL_POLICY_ENV = "HERMES_INTERNAL_RUNTIME_POLICY"
DISPATCH_MARKER = "HERMES_ARCHITECT_DISPATCH_V1"
DECISION_MARKER = "HERMES_ARCHITECTURE_DECISION_V1"
HANDOFF_MARKER = "HERMES_DOLLYCODE_HANDOFF_V1"

# Code-owned pins for the immutable files DollyOps later copies into the
# profile-local overlay.  They deliberately do not come from the install
# manifest in that writable overlay.
EXPECTED_OVERLAY_HASHES: Mapping[str, str] = {
    "__init__.py": "3861f2bc40cd2195a6cc4d8b2c0ebe2e2bfd29bbea3553b78baf2e799e6aae8f",
    "hardening.py": "0fe3eee9aa8a5421c0506c839d70aa7c1f52b08f85caf276670060e6d73743a3",
    "measurement_schema.json": "1e943ba3a82b7dd01766b39c0ad60263b52f5774af08b4d37007680f4da2e6b5",
    "profile.json": "2d77e6caa08ff0b9e9a9cdee0f3e7ff72622ed5ef6776ba7a8c65e5bbc9beaa5",
}

ALLOWED_TOOLS = frozenset(
    {
        "read_file",
        "search_files",
        "skill_view",
        "skills_list",
        "session_search",
        "write_file",
        "patch",
        "kanban_show",
        "kanban_attachments",
        "kanban_heartbeat",
        "kanban_create",
        "kanban_complete",
        "kanban_block",
    }
)
WRITE_TOOLS = frozenset({"write_file", "patch"})
EXCLUDED_SKILLS = frozenset(
    {
        "contract-driven-frontend-implementation",
        "mobile-ui-verification",
        "release-candidate-evidence",
        "external-upstream-pr-recuts",
    }
)


class ProfileRuntimePolicyError(RuntimeError):
    """An explicitly selected profile policy could not be safely activated."""


@dataclass(frozen=True)
class ActiveProfileRuntimePolicy:
    policy_id: str
    profile: str
    hermes_home: Path
    hardening: Any


@dataclass(frozen=True)
class GuardedToolCall:
    args: dict[str, Any]
    run_key: tuple[str, str] | None = None
    handoff_id: str | None = None


_handoff_lock = threading.Lock()
_handoff_states: dict[tuple[str, str], str] = {}
_overlay_module_lock = threading.Lock()
_overlay_modules: dict[tuple[str, tuple[str, ...]], Any] = {}


def _canonical_profile_name(value: str | None) -> str:
    return (value or "").strip().casefold().replace("_", "-")


def _home_identifies_dollyarchitect(home: Path) -> bool:
    try:
        resolved = home.resolve(strict=True)
    except OSError:
        return False
    return (
        resolved.name.casefold() == PROFILE_NAME
        and resolved.parent.name.casefold() == "profiles"
    )


def _read_raw_config(home: Path) -> dict[str, Any]:
    path = home / "config.yaml"
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProfileRuntimePolicyError(
            f"DollyArchitect activation config is missing or invalid: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise ProfileRuntimePolicyError(
            "DollyArchitect activation config must be a YAML mapping"
        )
    return value


def _require_config_invariants(config: Mapping[str, Any]) -> None:
    agent = config.get("agent")
    model = config.get("model")
    telegram = config.get("telegram")
    if not isinstance(agent, Mapping) or not isinstance(model, Mapping):
        raise ProfileRuntimePolicyError("DollyArchitect model/agent config is missing")
    activation = agent.get("runtime_policy")
    if not isinstance(activation, Mapping) or dict(activation) != {
        "id": POLICY_ID,
        "enabled": True,
    }:
        raise ProfileRuntimePolicyError(
            "agent.runtime_policy must contain exactly id=dollyarchitect.v1 and enabled=true"
        )
    if model.get("default") != "gpt-5.6-sol":
        raise ProfileRuntimePolicyError("DollyArchitect model must be gpt-5.6-sol")
    if agent.get("reasoning_effort") != "high" or agent.get("max_turns") != 60:
        raise ProfileRuntimePolicyError(
            "DollyArchitect requires agent.reasoning_effort=high and agent.max_turns=60"
        )
    if not isinstance(telegram, Mapping):
        raise ProfileRuntimePolicyError("DollyArchitect telegram config is missing")
    inherited_bot_policy = os.environ.get("TELEGRAM_ALLOW_BOTS", "").strip().casefold()
    if inherited_bot_policy in {"mentions", "all"}:
        raise ProfileRuntimePolicyError(
            "DollyArchitect rejects inherited TELEGRAM_ALLOW_BOTS bot bypass"
        )
    memory = config.get("memory", {})
    if memory is not None and not isinstance(memory, Mapping):
        raise ProfileRuntimePolicyError("DollyArchitect memory config is invalid")
    if isinstance(memory, Mapping) and (
        str(memory.get("provider") or "").strip()
        or memory.get("memory_enabled") is True
        or memory.get("user_profile_enabled") is True
    ):
        raise ProfileRuntimePolicyError(
            "DollyArchitect persistent/external memory must remain disabled"
        )
    skills = config.get("skills")
    if not isinstance(skills, Mapping):
        raise ProfileRuntimePolicyError(
            "DollyArchitect profile-local skills.disabled config is missing"
        )
    disabled = skills.get("disabled")
    observed_disabled = (
        {str(item).strip() for item in disabled if str(item).strip()}
        if isinstance(disabled, list)
        else set()
    )
    if not EXCLUDED_SKILLS.issubset(observed_disabled):
        raise ProfileRuntimePolicyError(
            "DollyArchitect profile-local skills.disabled exclusions are incomplete"
        )
    allow_from = telegram.get("allow_from")
    if (
        telegram.get("dm_policy") != "allowlist"
        or telegram.get("group_policy") != "disabled"
        or not isinstance(allow_from, list)
        or not allow_from
        or any(
            not isinstance(item, str)
            or not item.strip()
            or item.strip() == "*"
            for item in allow_from
        )
    ):
        raise ProfileRuntimePolicyError(
            "DollyArchitect requires private allowlisted Telegram DMs, disabled "
            "groups, and a non-wildcard allow_from list"
        )


def _verify_overlay(home: Path) -> tuple[tuple[str, ...], Mapping[str, bytes]]:
    overlay = home / OVERLAY_RELATIVE_PATH
    if not overlay.is_dir() or overlay.is_symlink():
        raise ProfileRuntimePolicyError(
            f"DollyArchitect immutable overlay is missing: {overlay}"
        )
    actual_files = {
        path.name
        for path in overlay.iterdir()
    }
    if actual_files != set(EXPECTED_OVERLAY_HASHES):
        raise ProfileRuntimePolicyError(
            "DollyArchitect immutable overlay file set drifted"
        )
    observed_hashes: list[str] = []
    verified_sources: dict[str, bytes] = {}
    for name, expected in EXPECTED_OVERLAY_HASHES.items():
        path = overlay / name
        if path.is_symlink():
            raise ProfileRuntimePolicyError(
                f"DollyArchitect overlay file must not be a symlink: {name}"
            )
        try:
            source = path.read_bytes()
            digest = hashlib.sha256(source).hexdigest()
        except OSError as exc:
            raise ProfileRuntimePolicyError(
                f"DollyArchitect overlay file is unreadable: {name}"
            ) from exc
        if digest != expected:
            raise ProfileRuntimePolicyError(
                f"DollyArchitect immutable overlay hash mismatch: {name}"
            )
        observed_hashes.append(digest)
        verified_sources[name] = source
    try:
        metadata = json.loads(verified_sources["profile.json"].decode("utf-8"))
    except Exception as exc:
        raise ProfileRuntimePolicyError(
            "DollyArchitect overlay metadata is invalid"
        ) from exc
    expected_runtime = {
        "policy_id": POLICY_ID,
        "overlay_relative_path": OVERLAY_RELATIVE_PATH.as_posix(),
        "dispatch_marker": DISPATCH_MARKER,
        "decision_marker": DECISION_MARKER,
    }
    if metadata.get("runtime_policy") != expected_runtime:
        raise ProfileRuntimePolicyError(
            "DollyArchitect overlay runtime metadata drifted"
        )
    return tuple(observed_hashes), verified_sources


def _load_overlay_hardening(
    home: Path,
    signature: tuple[str, ...],
    verified_sources: Mapping[str, bytes],
) -> Any:
    """Import only the already hash-verified installed implementation."""

    cache_key = (str(home), signature)
    with _overlay_module_lock:
        cached = _overlay_modules.get(cache_key)
        if cached is not None:
            return cached
        module_name = (
            "_hermes_profile_runtime_dollyarchitect_"
            + hashlib.sha256(str(home).encode("utf-8")).hexdigest()[:12]
        )
        path = home / OVERLAY_RELATIVE_PATH / "hardening.py"
        module = types.ModuleType(module_name)
        module.__file__ = str(path)
        module.__package__ = ""
        sys.modules[module_name] = module
        try:
            source = verified_sources["hardening.py"]
            exec(compile(source, str(path), "exec"), module.__dict__)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise ProfileRuntimePolicyError(
                "DollyArchitect immutable overlay failed to load"
            ) from exc
        _overlay_modules[cache_key] = module
        return module


def load_active_profile_runtime_policy(
    *,
    profile_name: str | None = None,
    hermes_home: str | Path | None = None,
) -> ActiveProfileRuntimePolicy | None:
    """Load the active policy, returning ``None`` for every ordinary profile."""

    raw_profile = os.environ.get("HERMES_PROFILE") if profile_name is None else profile_name
    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    profile_match = _canonical_profile_name(raw_profile) == PROFILE_NAME
    home_match = _home_identifies_dollyarchitect(home)
    if not profile_match and not home_match:
        return None
    if not profile_match or not home_match:
        raise ProfileRuntimePolicyError(
            "DollyArchitect identity mismatch between HERMES_PROFILE and resolved HERMES_HOME"
        )
    resolved_home = home.resolve(strict=True)
    _require_config_invariants(_read_raw_config(resolved_home))
    signature, verified_sources = _verify_overlay(resolved_home)
    hardening = _load_overlay_hardening(
        resolved_home, signature, verified_sources
    )
    return ActiveProfileRuntimePolicy(
        POLICY_ID, PROFILE_NAME, resolved_home, hardening
    )


def filter_tool_definitions(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    policy = load_active_profile_runtime_policy()
    if policy is None:
        return definitions
    _, runtime = _runtime_contract()
    actions = {
        item.value for item in runtime["contract_object"].requested_actions
    }
    allowed = ALLOWED_TOOLS
    if "write_architecture_document" not in actions:
        allowed = allowed - WRITE_TOOLS
    return [
        definition
        for definition in definitions
        if definition.get("function", {}).get("name") in allowed
    ]


def enforce_agent_tool_surface(agent: Any) -> None:
    """Re-apply policy after agent-level memory/context tools are appended."""

    tools = getattr(agent, "tools", None)
    if tools is None:
        return
    filtered = filter_tool_definitions(list(tools))
    agent.tools = filtered
    allowed_names = {
        item.get("function", {}).get("name")
        for item in filtered
        if isinstance(item, dict)
    }
    allowed_names.discard(None)
    agent.valid_tool_names = allowed_names
    if hasattr(agent, "_context_engine_tool_names"):
        agent._context_engine_tool_names.intersection_update(allowed_names)


def _parse_exact_json_block(body: str | None, marker: str) -> Mapping[str, Any]:
    text = body or ""
    pattern = re.compile(
        rf"(?ms)^[ \t]*<!-- {re.escape(marker)}[ \t]*\n"
        rf"(?P<payload>\{{.*?\}})[ \t]*\n"
        rf"{re.escape(marker)} -->[ \t]*$"
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1 or text.count(marker) != 2:
        raise ProfileRuntimePolicyError(
            f"task body must contain exactly one well-formed {marker} block"
        )
    try:
        payload = json.loads(matches[0].group("payload"))
    except json.JSONDecodeError as exc:
        raise ProfileRuntimePolicyError(f"{marker} payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ProfileRuntimePolicyError(f"{marker} payload must be a JSON object")
    return payload


def _resolve_project_primary_repo(project_id: str) -> Path:
    """Resolve an exact project id to its trusted absolute primary repository."""

    try:
        from hermes_cli import projects_db

        with projects_db.connect_readonly() as project_conn:
            project = projects_db.get_project(project_conn, project_id)
    except Exception as exc:
        raise ProfileRuntimePolicyError(
            "architect route project binding could not be resolved read-only"
        ) from exc
    if project is None or project.id != project_id or not project.primary_path:
        raise ProfileRuntimePolicyError(
            "architect route project must resolve by id with a primary repository"
        )
    primary_repo = Path(project.primary_path).expanduser()
    if not primary_repo.is_absolute() or ".." in primary_repo.parts:
        raise ProfileRuntimePolicyError(
            "architect route primary repository must be absolute and traversal-free"
        )
    return primary_repo


def materialize_structured_architect_route(
    *,
    source_task: Any,
    route_slot: str,
    body: str,
    routing: Mapping[str, Any],
) -> tuple[str, str]:
    """Resolve explicit work-kind routing and build the architect contract."""

    from profile_candidates.dollyarchitect import hardening

    expected = {
        "architecture_document_path",
        "bounded_file_cluster",
        "non_goals",
        "requested_actions",
        "work_kind",
    }
    if set(routing) != expected:
        raise ProfileRuntimePolicyError(
            "structured routing fields must be exact"
        )
    try:
        decision = hardening.classify_architect_fit(
            {"work_kind": routing["work_kind"]}
        )
    except hardening.ContractValidationError as exc:
        raise ProfileRuntimePolicyError(
            f"structured routing rejected: {exc}"
        ) from exc
    if not decision.accepted:
        return decision.route, body
    if body.count(DISPATCH_MARKER):
        raise ProfileRuntimePolicyError(
            "decomposer body must not supply an architect dispatch marker"
        )
    actions = routing["requested_actions"]
    if not isinstance(actions, list):
        raise ProfileRuntimePolicyError("requested_actions must be a list")
    bounded_file_cluster = routing["bounded_file_cluster"]
    non_goals = routing["non_goals"]
    if not isinstance(bounded_file_cluster, list) or not isinstance(non_goals, list):
        raise ProfileRuntimePolicyError(
            "bounded_file_cluster and non_goals must be lists"
        )
    workspace_kind = str(getattr(source_task, "workspace_kind", "") or "scratch")
    workspace_path = str(getattr(source_task, "workspace_path", "") or "")
    document = routing["architecture_document_path"]
    writable_roots: list[str] = []
    document_paths: list[str] = []
    if actions == ["write_architecture_document"]:
        if not isinstance(document, str) or not document.strip():
            raise ProfileRuntimePolicyError(
                "write_architecture_document requires architecture_document_path"
            )
        document_path = Path(document.strip())
        if not workspace_path or not document_path.is_absolute():
            raise ProfileRuntimePolicyError(
                "document-writing routes require an explicit absolute workspace path"
            )
        writable_roots = [str(document_path.parent)]
        document_paths = [str(document_path)]
    elif document is not None:
        raise ProfileRuntimePolicyError(
            "handoff-only routes require architecture_document_path=null"
        )
    project_id = str(getattr(source_task, "project_id", "") or "").strip()
    if not project_id:
        raise ProfileRuntimePolicyError(
            "architect routes require a trusted source project binding"
        )
    implementation_repo_path = _resolve_project_primary_repo(project_id)
    implementation_repo = str(implementation_repo_path)
    repository_identity = f"project:{project_id}:repo:{implementation_repo}"
    repo_is_materialized = (
        implementation_repo_path.is_dir()
        and (implementation_repo_path / ".git").exists()
    )
    implementation_workspace_policy = (
        hardening.ImplementationWorkspacePolicy.PROJECT_WORKTREE
        if repo_is_materialized
        else hardening.ImplementationWorkspacePolicy.PREPARATION_ONLY
    )
    contract_seed = json.dumps(
        {
            "policy_id": POLICY_ID,
            "route_slot": route_slot,
            "routing": dict(routing),
            "source_task_id": source_task.id,
            "project_id": project_id,
            "repository_identity": repository_identity,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    contract_id = "architect-" + hashlib.sha256(
        contract_seed.encode("utf-8")
    ).hexdigest()[:24]
    payload = {
        "architecture_document_paths": document_paths,
        "bounded_file_cluster": bounded_file_cluster,
        "contract_id": contract_id,
        "implementation_owner": None,
        "implementation_repo": implementation_repo,
        "implementation_workspace_policy": implementation_workspace_policy.value,
        "non_goals": non_goals,
        "operations_owner": None,
        "project_id": project_id,
        "repository_identity": repository_identity,
        "requested_actions": actions,
        "work_kind": decision.work_kind.value,
        "workspace_kind": workspace_kind,
        "writable_artifact_roots": writable_roots,
    }
    try:
        hardening.validate_architecture_contract(
            hardening.ArchitectureDispatchContract.from_mapping(payload)
        )
    except hardening.ContractValidationError as exc:
        raise ProfileRuntimePolicyError(
            f"generated architect dispatch contract rejected: {exc}"
        ) from exc
    block = (
        f"<!-- {DISPATCH_MARKER}\n"
        f"{json.dumps(payload, separators=(',', ':'), sort_keys=True)}\n"
        f"{DISPATCH_MARKER} -->"
    )
    return "DollyArchitect", f"{body.rstrip()}\n\n{block}".strip()


def _validate_architect_task_overrides(task: Any) -> None:
    """Reject per-task inputs that weaken the reviewed profile policy."""

    if getattr(task, "model_override", None) not in (None, "", "gpt-5.6-sol"):
        raise ProfileRuntimePolicyError(
            "DollyArchitect task model override must remain gpt-5.6-sol"
        )
    if getattr(task, "provider_override", None) not in (None, ""):
        raise ProfileRuntimePolicyError(
            "DollyArchitect task provider override must use the profile default"
        )
    forced_skills = {
        str(item).strip()
        for item in (getattr(task, "skills", None) or [])
        if str(item).strip()
    }
    excluded = sorted(forced_skills & EXCLUDED_SKILLS)
    if excluded:
        raise ProfileRuntimePolicyError(
            "DollyArchitect task force-loads excluded skill(s): "
            + ", ".join(excluded)
        )


def materialize_direct_architect_create(
    *,
    title: str,
    body: str,
    project_id: str,
    workspace_kind: str,
    workspace_path: str | None,
    routing: Mapping[str, Any],
    model_override: str | None,
    provider_override: str | None,
    skills: list[str] | tuple[str, ...] | None,
) -> str:
    """Materialize and preflight one direct DollyArchitect create request."""

    identity_seed = json.dumps(
        {
            "body": body,
            "project_id": project_id,
            "routing": dict(routing),
            "title": title,
            "workspace_kind": workspace_kind,
            "workspace_path": workspace_path,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    task = types.SimpleNamespace(
        id="direct-" + hashlib.sha256(identity_seed.encode("utf-8")).hexdigest()[:24],
        project_id=project_id,
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
        model_override=model_override,
        provider_override=provider_override,
        skills=skills,
    )
    routed, materialized_body = materialize_structured_architect_route(
        source_task=task,
        route_slot="direct-create",
        body=body,
        routing=routing,
    )
    if _canonical_profile_name(routed) != PROFILE_NAME:
        raise ProfileRuntimePolicyError(
            f"structured work_kind is not architect-fit; assign it to {routed}"
        )
    _validate_architect_task_overrides(task)

    payload = _parse_exact_json_block(materialized_body, DISPATCH_MARKER)
    from profile_candidates.dollyarchitect import hardening

    try:
        contract = hardening.validate_architecture_contract(
            hardening.ArchitectureDispatchContract.from_mapping(payload)
        )
    except hardening.ContractValidationError as exc:
        raise ProfileRuntimePolicyError(
            f"generated architect dispatch contract rejected: {exc}"
        ) from exc

    if (
        contract.implementation_workspace_policy
        is hardening.ImplementationWorkspacePolicy.PROJECT_WORKTREE
        and workspace_path
    ):
        raise ProfileRuntimePolicyError(
            "direct DollyArchitect project worktrees must use the canonical task-id path"
        )
    if contract.writable_artifact_roots:
        if not workspace_path:
            raise ProfileRuntimePolicyError(
                "direct document-writing routes require an explicit workspace path"
            )
        raw_workspace = Path(workspace_path)
        if not raw_workspace.is_absolute() or ".." in raw_workspace.parts:
            raise ProfileRuntimePolicyError(
                "DollyArchitect workspace must be absolute and traversal-free"
            )
        resolved_workspace = raw_workspace.resolve(strict=True)
        for raw_root in contract.writable_artifact_roots:
            try:
                resolved_root = Path(raw_root).resolve(strict=True)
                relative = resolved_root.relative_to(resolved_workspace)
            except (OSError, ValueError) as exc:
                raise ProfileRuntimePolicyError(
                    "DollyArchitect artifact roots must resolve beneath the task workspace"
                ) from exc
            if not relative.parts or not resolved_root.is_dir():
                raise ProfileRuntimePolicyError(
                    "DollyArchitect artifact roots must be existing strict workspace descendants"
                )
    return materialized_body


def prepare_dollyarchitect_spawn_env(
    *,
    task: Any,
    workspace: str,
    profile_name: str,
    hermes_home: str | Path,
) -> str | None:
    """Validate an architect task and return its canonical child-only payload."""

    policy = load_active_profile_runtime_policy(
        profile_name=profile_name, hermes_home=hermes_home
    )
    if policy is None:
        return None
    hardening = policy.hardening
    try:
        payload = _parse_exact_json_block(task.body, DISPATCH_MARKER)
        decision = hardening.classify_architect_fit(
            {"work_kind": payload.get("work_kind")}
        )
        if not decision.accepted:
            raise ProfileRuntimePolicyError(
                f"work_kind {decision.work_kind.value} is not architect-fit; "
                f"assign it to {decision.route} before spawn"
            )
        contract = hardening.validate_architecture_contract(
            hardening.ArchitectureDispatchContract.from_mapping(payload)
        )
    except hardening.ContractValidationError as exc:
        work_kind = payload.get("work_kind") if "payload" in locals() else None
        try:
            decision = hardening.classify_architect_fit({"work_kind": work_kind})
        except hardening.ContractValidationError:
            decision = None
        if decision is not None and not decision.accepted:
            raise ProfileRuntimePolicyError(
                f"work_kind {decision.work_kind.value} is not architect-fit; "
                f"assign it to {decision.route} before spawn"
            ) from exc
        raise ProfileRuntimePolicyError(
            f"DollyArchitect dispatch contract rejected: {exc}"
        ) from exc
    if contract.workspace_kind.value != task.workspace_kind:
        raise ProfileRuntimePolicyError(
            "DollyArchitect workspace_kind contradicts the Kanban task"
        )
    if contract.project_id != str(getattr(task, "project_id", "") or ""):
        raise ProfileRuntimePolicyError(
            "DollyArchitect project binding contradicts the Kanban task"
        )
    current_project_repo = _resolve_project_primary_repo(contract.project_id)
    if (
        str(current_project_repo) != contract.implementation_repo
        or contract.repository_identity
        != f"project:{contract.project_id}:repo:{current_project_repo}"
    ):
        raise ProfileRuntimePolicyError(
            "DollyArchitect repository binding contradicts the current project"
        )
    if (
        contract.implementation_workspace_policy
        is hardening.ImplementationWorkspacePolicy.PROJECT_WORKTREE
        and not (
            current_project_repo.is_dir()
            and (current_project_repo / ".git").exists()
        )
    ):
        raise ProfileRuntimePolicyError(
            "DollyArchitect project worktree policy requires a materialized repository"
        )
    raw_workspace = Path(workspace)
    if not raw_workspace.is_absolute() or ".." in raw_workspace.parts:
        raise ProfileRuntimePolicyError(
            "DollyArchitect workspace must be absolute and traversal-free"
        )
    resolved_workspace = raw_workspace.resolve(strict=True)
    for raw_root in contract.writable_artifact_roots:
        root = Path(raw_root)
        try:
            resolved_root = root.resolve(strict=True)
            relative = resolved_root.relative_to(resolved_workspace)
        except (OSError, ValueError) as exc:
            raise ProfileRuntimePolicyError(
                "DollyArchitect artifact roots must resolve beneath the task workspace"
            ) from exc
        if not relative.parts or not resolved_root.is_dir():
            raise ProfileRuntimePolicyError(
                "DollyArchitect artifact roots must be existing strict workspace descendants"
            )
    _validate_architect_task_overrides(task)
    canonical = {
        "contract": {
            **asdict(contract),
            "work_kind": contract.work_kind.value,
            "workspace_kind": contract.workspace_kind.value,
            "requested_actions": [item.value for item in contract.requested_actions],
        },
        "policy_id": policy.policy_id,
        "run_id": str(getattr(task, "current_run_id", "") or ""),
        "task_id": task.id,
        "workspace": str(resolved_workspace),
    }
    return json.dumps(canonical, separators=(",", ":"), sort_keys=True)


def _runtime_contract() -> tuple[ActiveProfileRuntimePolicy | None, Mapping[str, Any] | None]:
    policy = load_active_profile_runtime_policy()
    if policy is None:
        return None, None
    raw = os.environ.get(INTERNAL_POLICY_ENV, "")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProfileRuntimePolicyError(
            "active DollyArchitect worker is missing its canonical dispatcher payload"
        ) from exc
    if not isinstance(payload, dict) or payload.get("policy_id") != policy.policy_id:
        raise ProfileRuntimePolicyError("DollyArchitect dispatcher payload is invalid")
    if payload.get("task_id") != os.environ.get("HERMES_KANBAN_TASK"):
        raise ProfileRuntimePolicyError("DollyArchitect dispatcher task identity drifted")
    if str(payload.get("run_id") or "") != str(
        os.environ.get("HERMES_KANBAN_RUN_ID") or ""
    ):
        raise ProfileRuntimePolicyError("DollyArchitect dispatcher run identity drifted")
    workspace = os.environ.get("HERMES_KANBAN_WORKSPACE")
    hardening = policy.hardening
    try:
        if Path(payload.get("workspace", "")).resolve(strict=True) != Path(
            workspace or ""
        ).resolve(strict=True):
            raise ProfileRuntimePolicyError(
                "DollyArchitect dispatcher workspace identity drifted"
            )
        contract = hardening.validate_architecture_contract(
            hardening.ArchitectureDispatchContract.from_mapping(payload["contract"])
        )
    except (KeyError, OSError, hardening.ContractValidationError) as exc:
        raise ProfileRuntimePolicyError(
            "DollyArchitect canonical runtime contract is invalid"
        ) from exc
    return policy, {**payload, "contract_object": contract}


def _canonical_handoff_body(emission: Any) -> str:
    handoff = emission.handoffs[0]
    payload = {
        **asdict(handoff),
        "acceptance_criteria": list(handoff.acceptance_criteria),
        "constraints": list(handoff.constraints),
        "implementation_actions": list(emission.implementation_actions),
    }
    return (
        f"<!-- {HANDOFF_MARKER}\n"
        f"{json.dumps(payload, separators=(',', ':'), sort_keys=True)}\n"
        f"{HANDOFF_MARKER} -->"
    )


def _architecture_artifact_sha256(
    policy: ActiveProfileRuntimePolicy,
    runtime: Mapping[str, Any],
    packet: Any,
) -> str:
    contract = runtime["contract_object"]
    if packet.architecture_artifact == "inline:architecture-decision":
        artifact = json.dumps(
            {
                "acceptance_criteria": list(packet.acceptance_criteria),
                "constraints": list(packet.constraints),
                "decision": packet.decision,
                "rationale": packet.rationale,
                "validation_hypothesis": packet.validation_hypothesis,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(artifact).hexdigest()
    if [
        action.value for action in contract.requested_actions
    ] != ["write_architecture_document"]:
        raise ProfileRuntimePolicyError(
            "handoff-only architecture must use inline:architecture-decision"
        )
    try:
        resolved = policy.hardening.guard_write_target(
            target=packet.architecture_artifact,
            hermes_kanban_workspace=os.environ.get("HERMES_KANBAN_WORKSPACE"),
            artifact_roots=contract.writable_artifact_roots,
            workspace_kind=contract.workspace_kind.value,
            architecture_document_paths=contract.architecture_document_paths,
        )
        source = resolved.read_bytes()
    except (OSError, policy.hardening.PathGuardError) as exc:
        raise ProfileRuntimePolicyError(
            "DollyArchitect architecture artifact is missing or unsafe"
        ) from exc
    return hashlib.sha256(source).hexdigest()


def _handoff_idempotency_key(runtime: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {
            "dispatch_contract_id": runtime["contract_object"].contract_id,
            "handoff_slot": "dollycode-primary-handoff",
            "policy_id": runtime["policy_id"],
            "source_task_id": runtime["task_id"],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return "dollyarchitect:" + hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


def validate_trusted_handoff_project_binding(
    binding: object,
) -> tuple[str, str]:
    """Validate an internal create binding against the signed runtime contract."""

    policy, runtime = _runtime_contract()
    if policy is None or runtime is None or not isinstance(binding, Mapping):
        raise ProfileRuntimePolicyError(
            "trusted handoff project binding requires active DollyArchitect policy"
        )
    if set(binding) != {"project_id", "implementation_repo"}:
        raise ProfileRuntimePolicyError("trusted handoff project binding fields must be exact")
    contract = runtime["contract_object"]
    project_id = str(binding["project_id"] or "")
    implementation_repo = str(binding["implementation_repo"] or "")
    if (
        project_id != contract.project_id
        or implementation_repo != contract.implementation_repo
    ):
        raise ProfileRuntimePolicyError(
            "trusted handoff project binding contradicts the dispatch contract"
        )
    return project_id, implementation_repo


def guard_tool_call(tool_name: str, args: dict[str, Any]) -> GuardedToolCall:
    """Mandatory last-mile guard used immediately before actual execution."""

    policy, runtime = _runtime_contract()
    if policy is None:
        return GuardedToolCall(args)
    if tool_name not in ALLOWED_TOOLS:
        raise ProfileRuntimePolicyError(
            f"tool {tool_name!r} is disabled by DollyArchitect policy"
        )
    guarded = dict(args)
    contract = runtime["contract_object"]
    if tool_name in {"write_file", "patch"}:
        if [
            action.value for action in contract.requested_actions
        ] != ["write_architecture_document"]:
            raise ProfileRuntimePolicyError(
                f"{tool_name} is disabled for this requested_actions capability"
            )
        if tool_name == "patch" and guarded.get("mode", "replace") != "replace":
            raise ProfileRuntimePolicyError(
                "DollyArchitect forbids V4A multi-file patch mode"
            )
        target = guarded.get("path")
        if not isinstance(target, str) or not target:
            raise ProfileRuntimePolicyError("guarded file tool requires path")
        try:
            resolved = policy.hardening.guard_write_target(
                target=target,
                hermes_kanban_workspace=os.environ.get("HERMES_KANBAN_WORKSPACE"),
                artifact_roots=contract.writable_artifact_roots,
                workspace_kind=contract.workspace_kind.value,
                architecture_document_paths=contract.architecture_document_paths,
            )
        except policy.hardening.PathGuardError as exc:
            raise ProfileRuntimePolicyError(str(exc)) from exc
        guarded["path"] = str(resolved)
        guarded["cross_profile"] = False
    run_key = (
        str(runtime["task_id"]),
        str(runtime.get("run_id") or ""),
    )
    if tool_name.startswith("kanban_") and tool_name != "kanban_create":
        requested_task = guarded.get("task_id")
        if requested_task not in (None, "", runtime["task_id"]):
            raise ProfileRuntimePolicyError(
                "DollyArchitect Kanban tools are limited to the assigned task"
            )
        guarded["task_id"] = runtime["task_id"]
    if tool_name == "kanban_complete":
        with _handoff_lock:
            if _handoff_states.get(run_key) != "emitted":
                raise ProfileRuntimePolicyError(
                    "DollyArchitect cannot complete before its DollyCode handoff"
                )
    if tool_name == "kanban_create":
        if set(guarded) != {"title", "assignee", "body"}:
            raise ProfileRuntimePolicyError(
                "DollyArchitect kanban_create accepts exactly title, assignee, and body"
            )
        if str(guarded.get("assignee", "")).strip().casefold() != "dollycode":
            raise ProfileRuntimePolicyError(
                "DollyArchitect handoff assignee must be dollycode"
            )
        packet_payload = _parse_exact_json_block(guarded.get("body"), DECISION_MARKER)
        try:
            packet = policy.hardening.ArchitectureDecisionPacket.from_mapping(
                packet_payload
            )
            artifact_sha256 = _architecture_artifact_sha256(
                policy, runtime, packet
            )
            emission = policy.hardening.packet_to_dollycode_handoff(
                packet,
                contract=contract,
                source_task_id=runtime["task_id"],
                architecture_artifact_sha256=artifact_sha256,
            )
        except policy.hardening.ContractValidationError as exc:
            raise ProfileRuntimePolicyError(
                f"DollyArchitect decision packet rejected: {exc}"
            ) from exc
        if len(emission.handoffs) != 1 or emission.implementation_actions:
            raise ProfileRuntimePolicyError(
                "DollyArchitect handoff transformation violated its zero-action invariant"
            )
        with _handoff_lock:
            if run_key in _handoff_states:
                raise ProfileRuntimePolicyError(
                    "DollyArchitect permits exactly one DollyCode handoff per run"
                )
            _handoff_states[run_key] = "reserved"
        guarded["assignee"] = "dollycode"
        guarded["body"] = _canonical_handoff_body(emission)
        guarded["parents"] = [runtime["task_id"]]
        guarded["idempotency_key"] = _handoff_idempotency_key(runtime)
        guarded["_strict_idempotency_match"] = True
        guarded["_trusted_project_binding"] = {
            "project_id": contract.project_id,
            "implementation_repo": contract.implementation_repo,
        }
        runnable = (
            contract.implementation_workspace_policy
            is policy.hardening.ImplementationWorkspacePolicy.PROJECT_WORKTREE
        )
        guarded["workspace_kind"] = "worktree" if runnable else "scratch"
        guarded["workspace_path"] = None
        guarded["initial_status"] = "running" if runnable else "blocked"
        guarded["skills"] = []
        guarded["goal_mode"] = False
        return GuardedToolCall(
            guarded, run_key=run_key, handoff_id=emission.handoffs[0].handoff_id
        )
    return GuardedToolCall(guarded)


def finish_guarded_tool_call(call: GuardedToolCall, result: Any) -> None:
    if call.run_key is None:
        return
    ok = False
    try:
        decoded = json.loads(result) if isinstance(result, str) else result
        ok = isinstance(decoded, dict) and decoded.get("ok") is True
    except (TypeError, json.JSONDecodeError):
        ok = False
    with _handoff_lock:
        if ok:
            _handoff_states[call.run_key] = "emitted"
        elif _handoff_states.get(call.run_key) == "reserved":
            _handoff_states.pop(call.run_key, None)


def reset_runtime_policy_state_for_tests() -> None:
    with _handoff_lock:
        _handoff_states.clear()
