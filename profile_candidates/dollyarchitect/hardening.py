"""Pure hardening primitives for an inactive DollyArchitect profile candidate.

Nothing in this module registers a profile, mutates live configuration, writes
files, dispatches work, or invokes an implementation action.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence


class ContractValidationError(ValueError):
    """Raised before dispatch when an architect contract is invalid."""


class PathGuardError(ValueError):
    """Raised when a candidate write target is not unambiguously safe."""


class WorkKind(str, Enum):
    """Explicit task ontology; unknown values are rejected."""

    ONTOLOGY = "ontology"
    SCENARIO_GRAMMAR = "scenario_grammar"
    CODE_VS_DATA = "code_vs_data"
    SHARED_HARNESS_ADAPTER_UI_PRIMITIVE = "shared_harness_adapter_ui_primitive"
    CROSS_REPO_CONTRACT = "cross_repo_contract"
    MIGRATION_SEAM = "migration_seam"
    MATERIALLY_DIFFERENT_SECOND_SCENARIO_REUSE = (
        "materially_different_second_scenario_reuse"
    )

    IMPLEMENTATION_CODE_PATCH = "implementation_code_patch"
    MODEL_EVALUATION = "model_evaluation"
    PORTFOLIO_EVALUATION = "portfolio_evaluation"
    BENCHMARK = "benchmark"
    ROUTINE_QA_REVIEW = "routine_qa_review"
    VISUAL_DESIGN = "visual_design"
    RELEASE = "release"
    PULL_REQUEST = "pull_request"
    DEPLOY = "deploy"


ARCHITECTURE_FIT_KINDS = frozenset(
    {
        WorkKind.ONTOLOGY,
        WorkKind.SCENARIO_GRAMMAR,
        WorkKind.CODE_VS_DATA,
        WorkKind.SHARED_HARNESS_ADAPTER_UI_PRIMITIVE,
        WorkKind.CROSS_REPO_CONTRACT,
        WorkKind.MIGRATION_SEAM,
        WorkKind.MATERIALLY_DIFFERENT_SECOND_SCENARIO_REUSE,
    }
)

_REROUTE_OWNERS = {
    WorkKind.IMPLEMENTATION_CODE_PATCH: "DollyCode",
    WorkKind.MODEL_EVALUATION: "DollyQA",
    WorkKind.PORTFOLIO_EVALUATION: "DollyQA",
    WorkKind.BENCHMARK: "DollyQA",
    WorkKind.ROUTINE_QA_REVIEW: "DollyQA",
    WorkKind.VISUAL_DESIGN: "DollyDesign",
    WorkKind.RELEASE: "DollyOps",
    WorkKind.PULL_REQUEST: "DollyOps",
    WorkKind.DEPLOY: "DollyOps",
}


@dataclass(frozen=True)
class FitDecision:
    accepted: bool
    work_kind: WorkKind
    route: str
    reason_code: str


def _require_exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ContractValidationError(
            f"{label} fields must be exact; missing={missing}, unknown={unknown}"
        )


def _parse_work_kind(value: object) -> WorkKind:
    if not isinstance(value, str):
        raise ContractValidationError("work_kind must be a string enum value")
    try:
        return WorkKind(value)
    except ValueError as exc:
        raise ContractValidationError(f"unknown work_kind: {value!r}") from exc


def classify_architect_fit(payload: Mapping[str, object]) -> FitDecision:
    """Classify one explicitly tagged request without fuzzy text matching."""

    _require_exact_fields(payload, frozenset({"work_kind"}), "fit request")
    work_kind = _parse_work_kind(payload["work_kind"])
    if work_kind in ARCHITECTURE_FIT_KINDS:
        return FitDecision(
            accepted=True,
            work_kind=work_kind,
            route="DollyArchitect",
            reason_code="architectural_boundary_or_reuse_decision",
        )
    return FitDecision(
        accepted=False,
        work_kind=work_kind,
        route=_REROUTE_OWNERS[work_kind],
        reason_code="explicit_non_architect_work_kind",
    )


class WorkspaceKind(str, Enum):
    SCRATCH = "scratch"
    WORKTREE = "worktree"


class RequestedAction(str, Enum):
    ARCHITECTURE_DECISION = "architecture_decision"
    WRITE_ARCHITECTURE_DOCUMENT = "write_architecture_document"
    NO_EDITS = "no_edits"
    IMPLEMENTATION = "implementation"
    QA_REVIEW = "qa_review"
    COMMIT = "commit"
    PUSH = "push"
    PULL_REQUEST = "pull_request"
    RELEASE = "release"
    DEPLOY = "deploy"


_EXECUTION_ACTIONS = frozenset(
    {
        RequestedAction.IMPLEMENTATION,
        RequestedAction.QA_REVIEW,
        RequestedAction.COMMIT,
        RequestedAction.PUSH,
        RequestedAction.PULL_REQUEST,
        RequestedAction.RELEASE,
        RequestedAction.DEPLOY,
    }
)


def _strict_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _strict_string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        raise ContractValidationError(f"{field_name} must be a list of strings")
    result = tuple(_strict_string(item, field_name) for item in value)
    return result


@dataclass(frozen=True)
class ArchitectureDispatchContract:
    contract_id: str
    work_kind: WorkKind
    workspace_kind: WorkspaceKind
    writable_artifact_roots: tuple[str, ...]
    architecture_document_paths: tuple[str, ...]
    requested_actions: tuple[RequestedAction, ...]
    implementation_owner: str | None
    operations_owner: str | None

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
    ) -> ArchitectureDispatchContract:
        expected = frozenset(
            {
                "contract_id",
                "work_kind",
                "workspace_kind",
                "writable_artifact_roots",
                "architecture_document_paths",
                "requested_actions",
                "implementation_owner",
                "operations_owner",
            }
        )
        _require_exact_fields(payload, expected, "architecture contract")
        try:
            workspace_kind = WorkspaceKind(payload["workspace_kind"])
        except (TypeError, ValueError) as exc:
            raise ContractValidationError(
                f"unknown workspace_kind: {payload['workspace_kind']!r}"
            ) from exc

        raw_actions = _strict_string_tuple(
            payload["requested_actions"], "requested_actions"
        )
        try:
            actions = tuple(RequestedAction(action) for action in raw_actions)
        except ValueError as exc:
            raise ContractValidationError(f"unknown requested action: {exc}") from exc

        implementation_owner = payload["implementation_owner"]
        operations_owner = payload["operations_owner"]
        if implementation_owner is not None:
            implementation_owner = _strict_string(
                implementation_owner, "implementation_owner"
            )
        if operations_owner is not None:
            operations_owner = _strict_string(
                operations_owner, "operations_owner"
            )

        return cls(
            contract_id=_strict_string(payload["contract_id"], "contract_id"),
            work_kind=_parse_work_kind(payload["work_kind"]),
            workspace_kind=workspace_kind,
            writable_artifact_roots=_strict_string_tuple(
                payload["writable_artifact_roots"], "writable_artifact_roots"
            ),
            architecture_document_paths=_strict_string_tuple(
                payload["architecture_document_paths"],
                "architecture_document_paths",
            ),
            requested_actions=actions,
            implementation_owner=implementation_owner,
            operations_owner=operations_owner,
        )


def _reject_ambiguous_declared_paths(paths: Sequence[str], label: str) -> None:
    if not paths:
        raise ContractValidationError(f"{label} must name at least one path")
    path_objects = tuple(Path(path) for path in paths)
    if any(not path.is_absolute() for path in path_objects):
        raise ContractValidationError(f"{label} paths must be absolute")
    if any(".." in path.parts for path in path_objects):
        raise ContractValidationError(f"{label} paths must not contain traversal")
    if any(any(token in raw_path for token in "*?[]{}") for raw_path in paths):
        raise ContractValidationError(f"{label} paths must not contain glob syntax")
    if len(set(path_objects)) != len(path_objects):
        raise ContractValidationError(f"{label} paths must be unique")
    for index, left in enumerate(path_objects):
        for right in path_objects[index + 1 :]:
            if left in right.parents or right in left.parents:
                raise ContractValidationError(f"{label} paths must not overlap")


def validate_architecture_contract(
    contract: ArchitectureDispatchContract,
) -> ArchitectureDispatchContract:
    """Fail closed on contradictory or execution-bearing dispatch contracts."""

    if contract.work_kind not in ARCHITECTURE_FIT_KINDS:
        raise ContractValidationError("work_kind is not architect-fit")
    if not contract.requested_actions:
        raise ContractValidationError("requested_actions must not be empty")
    if len(set(contract.requested_actions)) != len(contract.requested_actions):
        raise ContractValidationError("requested_actions must be unique")

    actions = frozenset(contract.requested_actions)
    if RequestedAction.NO_EDITS in actions and actions & {
        RequestedAction.COMMIT,
        RequestedAction.PUSH,
    }:
        raise ContractValidationError("no_edits cannot be combined with commit or push")
    if (
        RequestedAction.IMPLEMENTATION in actions
        and contract.implementation_owner != "DollyCode"
    ):
        raise ContractValidationError(
            "implementation requires separately named DollyCode owner"
        )
    if actions & {
        RequestedAction.RELEASE,
        RequestedAction.DEPLOY,
        RequestedAction.PULL_REQUEST,
    } and contract.operations_owner != "DollyOps":
        raise ContractValidationError(
            "release, deploy, or pull_request requires separately named DollyOps owner"
        )
    if actions & _EXECUTION_ACTIONS:
        raise ContractValidationError(
            "mixed architecture and execution contracts are forbidden"
        )

    _reject_ambiguous_declared_paths(
        contract.writable_artifact_roots, "writable_artifact_roots"
    )
    if contract.workspace_kind is WorkspaceKind.WORKTREE:
        if len(contract.architecture_document_paths) != 1:
            raise ContractValidationError(
                "worktree mode requires exactly one architecture document path"
            )
        _reject_ambiguous_declared_paths(
            contract.architecture_document_paths,
            "architecture_document_paths",
        )
        document = Path(contract.architecture_document_paths[0])
        containing_roots = tuple(
            Path(root)
            for root in contract.writable_artifact_roots
            if _is_relative_to(document, Path(root))
        )
        if len(containing_roots) != 1:
            raise ContractValidationError(
                "worktree architecture document must be beneath one artifact root"
            )
        relative_document = document.relative_to(containing_roots[0])
        if document.suffix.casefold() in _SOURCE_SUFFIXES:
            raise ContractValidationError(
                "worktree architecture document must not have a source suffix"
            )
        if any(
            part.casefold() in _SOURCE_DIRECTORY_NAMES
            for part in relative_document.parts[:-1]
        ):
            raise ContractValidationError(
                "worktree architecture document must not be in a source directory"
            )
        if document.suffix.casefold() not in _ARCHITECTURE_DOCUMENT_SUFFIXES:
            raise ContractValidationError(
                "worktree architecture document must use a document suffix"
            )
    elif contract.architecture_document_paths:
        raise ContractValidationError(
            "scratch mode forbids worktree-only architecture_document_paths"
        )
    return contract


_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".mjs",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
    }
)
_SOURCE_DIRECTORY_NAMES = frozenset(
    {
        "app",
        "apps",
        "bin",
        "cmd",
        "lib",
        "packages",
        "scripts",
        "src",
        "test",
        "tests",
    }
)
_ARCHITECTURE_DOCUMENT_SUFFIXES = frozenset(
    {".adoc", ".json", ".md", ".rst", ".txt", ".yaml", ".yml"}
)


def _resolved_existing_directory(raw_path: str, label: str) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise PathGuardError(f"{label} must be absolute and traversal-free")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PathGuardError(f"{label} is missing or unresolvable") from exc
    if not resolved.is_dir():
        raise PathGuardError(f"{label} must resolve to a directory")
    return resolved


def _resolve_target(raw_path: str) -> Path:
    target = Path(raw_path)
    if not target.is_absolute() or ".." in target.parts:
        raise PathGuardError("write target must be absolute and traversal-free")
    try:
        return target.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PathGuardError("write target is unresolvable") from exc


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_artifact_roots(
    raw_roots: Sequence[str],
    workspace: Path,
) -> tuple[Path, ...]:
    if not raw_roots:
        raise PathGuardError("explicit artifact roots are required")
    resolved = tuple(
        _resolved_existing_directory(root, "artifact root") for root in raw_roots
    )
    if len(set(resolved)) != len(resolved):
        raise PathGuardError("artifact roots are ambiguous after resolution")
    if any(root == workspace or not _is_relative_to(root, workspace) for root in resolved):
        raise PathGuardError("artifact roots must be strict descendants of workspace")
    for index, left in enumerate(resolved):
        for right in resolved[index + 1 :]:
            if _is_relative_to(left, right) or _is_relative_to(right, left):
                raise PathGuardError("artifact roots must not overlap")
    return resolved


def _reject_source_target(target: Path, workspace: Path) -> None:
    relative = target.relative_to(workspace)
    if target.suffix.casefold() in _SOURCE_SUFFIXES:
        raise PathGuardError("source-code-like suffix is forbidden in worktree mode")
    if any(part.casefold() in _SOURCE_DIRECTORY_NAMES for part in relative.parts[:-1]):
        raise PathGuardError("source-code-like directory is forbidden in worktree mode")
    if target.suffix.casefold() not in _ARCHITECTURE_DOCUMENT_SUFFIXES:
        raise PathGuardError("worktree target must be an architecture document")


def guard_write_target(
    *,
    target: str,
    hermes_kanban_workspace: str | None,
    artifact_roots: Sequence[str],
    workspace_kind: str,
    architecture_document_paths: Sequence[str] = (),
) -> Path:
    """Return the safe resolved target or reject it without writing.

    ``hermes_kanban_workspace`` is the explicit value supplied by the worker's
    ``HERMES_KANBAN_WORKSPACE`` environment. It is an argument, rather than an
    ambient read, so the decision remains pure and testable.
    """

    if not hermes_kanban_workspace:
        raise PathGuardError("HERMES_KANBAN_WORKSPACE is required")
    try:
        kind = WorkspaceKind(workspace_kind)
    except ValueError as exc:
        raise PathGuardError(f"unsupported workspace kind: {workspace_kind!r}") from exc

    workspace = _resolved_existing_directory(
        hermes_kanban_workspace, "HERMES_KANBAN_WORKSPACE"
    )
    roots = _resolve_artifact_roots(artifact_roots, workspace)
    resolved_target = _resolve_target(target)
    containing_roots = tuple(
        root for root in roots if _is_relative_to(resolved_target, root)
    )
    if len(containing_roots) != 1:
        raise PathGuardError(
            "write target must resolve beneath exactly one assigned artifact root"
        )

    if kind is WorkspaceKind.WORKTREE:
        if len(architecture_document_paths) != 1:
            raise PathGuardError(
                "worktree mode requires exactly one architecture document path"
            )
        raw_document = Path(architecture_document_paths[0])
        if not raw_document.is_absolute() or ".." in raw_document.parts:
            raise PathGuardError(
                "architecture document path must be absolute and traversal-free"
            )
        try:
            resolved_document = raw_document.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise PathGuardError("architecture document path is unresolvable") from exc
        if resolved_target != resolved_document:
            raise PathGuardError(
                "worktree writes are limited to the named architecture document"
            )
        _reject_source_target(resolved_target, workspace)
    elif architecture_document_paths:
        raise PathGuardError(
            "scratch mode must not declare worktree architecture document paths"
        )
    return resolved_target


@dataclass(frozen=True)
class ArchitectureDecisionPacket:
    packet_id: str
    decision: str
    rationale: str
    constraints: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    dollycode_owner: str

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
    ) -> ArchitectureDecisionPacket:
        expected = frozenset(
            {
                "packet_id",
                "decision",
                "rationale",
                "constraints",
                "acceptance_criteria",
                "dollycode_owner",
            }
        )
        _require_exact_fields(payload, expected, "architecture decision packet")
        owner = _strict_string(payload["dollycode_owner"], "dollycode_owner")
        if owner != "DollyCode":
            raise ContractValidationError("handoff owner must be DollyCode")
        constraints = _strict_string_tuple(payload["constraints"], "constraints")
        criteria = _strict_string_tuple(
            payload["acceptance_criteria"], "acceptance_criteria"
        )
        if not constraints or not criteria:
            raise ContractValidationError(
                "constraints and acceptance_criteria must not be empty"
            )
        return cls(
            packet_id=_strict_string(payload["packet_id"], "packet_id"),
            decision=_strict_string(payload["decision"], "decision"),
            rationale=_strict_string(payload["rationale"], "rationale"),
            constraints=constraints,
            acceptance_criteria=criteria,
            dollycode_owner=owner,
        )


@dataclass(frozen=True)
class DollyCodeHandoffContract:
    handoff_id: str
    source_packet_id: str
    owner: str
    requested_action: str
    architecture_decision: str
    constraints: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]


@dataclass(frozen=True)
class HandoffEmission:
    handoffs: tuple[DollyCodeHandoffContract, ...]
    implementation_actions: tuple[object, ...]


def packet_to_dollycode_handoff(
    packet: ArchitectureDecisionPacket,
) -> HandoffEmission:
    """Transform one accepted packet into one handoff and execute nothing."""

    canonical = json.dumps(
        {
            "acceptance_criteria": packet.acceptance_criteria,
            "constraints": packet.constraints,
            "decision": packet.decision,
            "packet_id": packet.packet_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    handoff = DollyCodeHandoffContract(
        handoff_id=f"dollycode-{digest}",
        source_packet_id=packet.packet_id,
        owner=packet.dollycode_owner,
        requested_action="implement_from_architecture_decision",
        architecture_decision=packet.decision,
        constraints=packet.constraints,
        acceptance_criteria=packet.acceptance_criteria,
    )
    return HandoffEmission(handoffs=(handoff,), implementation_actions=())


ARCHITECTURE_METRICS = (
    "implementation_without_redesign",
    "first_pass_QA",
    "architecture_related_escaped_defects",
    "role_leakage",
    "timeout",
    "architecture_to_green_code_time",
)


def validate_measurement_receipt(
    receipt: Mapping[str, object],
) -> Mapping[str, object]:
    """Validate a local candidate receipt containing exactly five packages."""

    _require_exact_fields(
        receipt,
        frozenset({"scope", "telemetry_activation", "packages"}),
        "measurement receipt",
    )
    if receipt["scope"] != "candidate_local":
        raise ContractValidationError("measurement scope must be candidate_local")
    if receipt["telemetry_activation"] is not False:
        raise ContractValidationError("telemetry_activation must remain false")
    packages = receipt["packages"]
    if not isinstance(packages, list) or len(packages) != 5:
        raise ContractValidationError("measurement receipt requires exactly five packages")

    expected_metrics = frozenset(ARCHITECTURE_METRICS)
    package_ids: set[str] = set()
    for package in packages:
        if not isinstance(package, Mapping):
            raise ContractValidationError("each package measurement must be a mapping")
        _require_exact_fields(
            package, frozenset({"package_id", "metrics"}), "package measurement"
        )
        package_id = _strict_string(package["package_id"], "package_id")
        if package_id in package_ids:
            raise ContractValidationError("package_id values must be unique")
        package_ids.add(package_id)
        metrics = package["metrics"]
        if not isinstance(metrics, Mapping):
            raise ContractValidationError("metrics must be a mapping")
        _require_exact_fields(metrics, expected_metrics, "metrics")
        _validate_metric_types(metrics)
    return receipt


def _validate_metric_types(metrics: Mapping[str, object]) -> None:
    boolean_metrics = {
        "implementation_without_redesign",
        "first_pass_QA",
        "role_leakage",
        "timeout",
    }
    for name in boolean_metrics:
        if not isinstance(metrics[name], bool):
            raise ContractValidationError(f"{name} must be boolean")
    defects = metrics["architecture_related_escaped_defects"]
    if isinstance(defects, bool) or not isinstance(defects, int) or defects < 0:
        raise ContractValidationError(
            "architecture_related_escaped_defects must be a non-negative integer"
        )
    elapsed = metrics["architecture_to_green_code_time"]
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or elapsed < 0
    ):
        raise ContractValidationError(
            "architecture_to_green_code_time must be a non-negative number"
        )


@dataclass(frozen=True)
class CandidateProfilePolicy:
    status: str
    activated: bool
    default_workspace_kind: str
    model: str
    reasoning: str
    max_turns: int
    allowed_capabilities: tuple[str, ...]
    disabled_capabilities: tuple[str, ...]
    terminal_enabled: bool
    terminal_disabled_reason: str
    hindsight_enabled: bool
    memory_continuity: tuple[str, ...]
    memory_disabled_reason_code: str
    active_priority_exclusions: tuple[str, ...]
    exclusions_scope: str
    shared_skills_mutation: bool


PROFILE_POLICY = CandidateProfilePolicy(
    status="inactive_candidate",
    activated=False,
    default_workspace_kind="scratch",
    model="gpt-5.6-sol",
    reasoning="high",
    max_turns=60,
    allowed_capabilities=(
        "read_search",
        "knowledge_code_intel",
        "kanban",
        "session_search",
        "guarded_assigned_artifact_write",
    ),
    disabled_capabilities=(
        "normal_cron",
        "delegation",
        "computer_use",
        "media_image",
        "github_write",
        "deploy_release",
    ),
    terminal_enabled=False,
    terminal_disabled_reason=(
        "candidate cannot enforce command, cwd, and resolved-path allowlisting"
    ),
    hindsight_enabled=False,
    memory_continuity=("knowledge", "session_search"),
    memory_disabled_reason_code=(
        "missing_HINDSIGHT_API_KEY_and_HINDSIGHT_LLM_API_KEY"
    ),
    active_priority_exclusions=(
        "contract-driven-frontend-implementation",
        "mobile-ui-verification",
        "release-candidate-evidence",
        "external-upstream-pr-recuts",
    ),
    exclusions_scope="profile_local_only",
    shared_skills_mutation=False,
)
