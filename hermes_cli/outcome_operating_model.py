"""Outcome-first operating-model primitives for Hermes.

This module is intentionally a projection and policy layer, not a new workflow
store. Project canon remains in repositories/knowledge, Kanban remains durable
execution truth, and Dolly/default remains the only outcome owner.

The helpers here are pure wherever possible so direct execution, durable
Kanban admission, status projection, and operator tooling can share the same
rules without adding a controller or lifecycle authority.
"""
from __future__ import annotations

import copy
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


class OutcomeTier(str, Enum):
    FOCUS = "focus"
    WARM = "warm"
    COLD = "cold"


class OutcomeKind(str, Enum):
    NORMAL = "normal"
    INCIDENT = "incident"


class Maturity(str, Enum):
    V0 = "V0"
    V1 = "V1"
    V2 = "V2"
    V3 = "V3"


class ExecutionMode(str, Enum):
    DIRECT = "direct"
    DURABLE = "durable"
    SPECIALIST = "specialist"
    OPS = "ops"


class ExecutionAccess(str, Enum):
    MUTATING = "mutating"
    READ_ONLY = "read_only"


OUTCOME_OWNER = "default"
OUTCOME_OWNER_ALIASES = frozenset({"default", "dolly/default"})
DEFAULT_MAX_NORMAL_FOCUS_OUTCOMES = 3
DEFAULT_MAX_MUTATING_WORKERS = 3
DEFAULT_MAX_INCIDENT_OUTCOMES = 1

_MARKER_KEYS = (
    "outcome_id",
    "outcome_tier",
    "maturity",
    "execution_mode",
    "execution_access",
    "shared_authority_scope",
    "authority_scope",
    "outcome_kind",
    "outcome_owner",
    "contract_id",
)
_MARKER_RE = re.compile(
    r"(?mi)^\s*(" + "|".join(_MARKER_KEYS) + r")\s*[:=]\s*(.*?)\s*$"
)
_OUTCOME_STATE_KEYS = frozenset(
    {
        "outcome_id",
        "outcome_tier",
        "maturity",
        "execution_mode",
        "execution_access",
        "shared_authority_scope",
        "authority_scope",
        "outcome_kind",
        "outcome_owner",
        "outcome_result",
        "outcome_receipt",
        "resume_receipt",
        "last_verified_evidence",
        "next_action",
        "decision_required",
        "preempts",
    }
)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        keys = value.keys()
    except (AttributeError, TypeError):
        return getattr(value, name, default)
    if name in keys:
        return value[name]
    return default


def _marker_map(body: str) -> dict[str, str]:
    markers: dict[str, str] = {}
    for match in _MARKER_RE.finditer(body or ""):
        key = match.group(1).strip().casefold()
        raw = match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
            raw = raw[1:-1].strip()
        markers[key] = raw
    return markers


def _enum_value(enum_type, value: Any, *, field_name: str):
    raw = str(value or "").strip()
    try:
        return enum_type(raw)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"invalid {field_name} {raw!r}; expected one of: {allowed}") from exc


def _maturity(value: Any) -> Maturity:
    raw = str(value or "").strip().upper()
    try:
        return Maturity(raw)
    except ValueError as exc:
        raise ValueError("invalid maturity; expected one of: V0, V1, V2, V3") from exc


def normalize_owner(value: Any) -> str:
    raw = str(value or OUTCOME_OWNER).strip().casefold()
    if raw not in OUTCOME_OWNER_ALIASES:
        raise ValueError("outcome_owner must remain Dolly/default")
    return OUTCOME_OWNER


def canonical_repository_scope(path: Any) -> str:
    """Resolve a workdir to a stable repository/workspace identity.

    Git common-dir wins so sibling worktrees are always the same repository.
    This identity is mechanical and cannot be overridden by task metadata.
    """
    raw = str(path or "").strip()
    if not raw:
        return "unscoped"
    candidate = Path(raw).expanduser()
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        resolved = candidate

    if resolved.exists():
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(resolved),
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0:
            common = proc.stdout.strip()
            if common:
                try:
                    return f"git:{Path(common).expanduser().resolve()}"
                except (OSError, RuntimeError):
                    return f"git:{common.rstrip('/')}"

    normalized = str(resolved)
    for marker in ("/.worktrees/", "/worktrees/"):
        if marker in normalized:
            normalized = normalized.split(marker, 1)[0]
            break
    return f"workspace:{normalized.rstrip('/')}"


# Backward-compatible helper name for callers from the first candidate. The
# returned value is repository/workspace identity, never logical shared scope.
def canonical_authority_scope(path: Any) -> str:
    return canonical_repository_scope(path)


def _derive_repository_scope(task: Any) -> str:
    workspace = str(_field(task, "workspace_path", "") or "").strip()
    if workspace:
        return canonical_repository_scope(workspace)

    project_id = str(_field(task, "project_id", "") or "").strip()
    if project_id:
        return f"project:{project_id}"

    return "unscoped"


def _derive_shared_authority_scope(markers: Mapping[str, str]) -> str:
    # ``authority_scope`` was the marker name in the first candidate. Preserve
    # it as an alias, but it is now additive logical scope and can never replace
    # repository identity.
    return str(
        markers.get("shared_authority_scope")
        or markers.get("authority_scope")
        or ""
    ).strip()


@dataclass(frozen=True)
class TaskExecutionClaim:
    task_id: str
    board: str
    outcome_id: str
    project_id: str
    tier: Optional[OutcomeTier]
    maturity: Optional[Maturity]
    mode: ExecutionMode
    access: ExecutionAccess
    repository_scope: str
    shared_authority_scope: str
    kind: OutcomeKind
    owner: str
    status: str = ""
    created_at: int = 0
    priority: int = 0
    explicit_outcome: bool = False

    @property
    def authority_scope(self) -> str:
        """Backward-compatible name for mechanical repository/workspace scope."""
        return self.repository_scope

    @property
    def mutating(self) -> bool:
        return self.access is ExecutionAccess.MUTATING

    @property
    def read_only(self) -> bool:
        return self.access is ExecutionAccess.READ_ONLY


def task_execution_claim(task: Any, *, board: str = "default") -> TaskExecutionClaim:
    """Project one Kanban task into the bounded execution-admission model.

    Legacy tasks deliberately fail closed as mutating durable work. They do not
    acquire guessed focus/warm/cold classification. New outcome-managed tasks
    opt in through explicit line markers in their existing task contract/body.
    """

    body = str(_field(task, "body", "") or "")
    markers = _marker_map(body)
    task_id = str(_field(task, "id", "") or "").strip()
    if not task_id:
        raise ValueError("task id is required for execution admission")

    explicit_outcome = any(key in markers for key in _MARKER_KEYS[:-1])
    outcome_id = str(markers.get("outcome_id") or markers.get("contract_id") or f"task:{task_id}").strip()
    if not outcome_id:
        raise ValueError("outcome_id must not be empty")

    tier = (
        _enum_value(OutcomeTier, markers["outcome_tier"], field_name="outcome_tier")
        if "outcome_tier" in markers
        else None
    )
    maturity = _maturity(markers["maturity"]) if "maturity" in markers else None
    mode = (
        _enum_value(ExecutionMode, markers["execution_mode"], field_name="execution_mode")
        if "execution_mode" in markers
        else ExecutionMode.DURABLE
    )
    access = (
        _enum_value(ExecutionAccess, markers["execution_access"], field_name="execution_access")
        if "execution_access" in markers
        else ExecutionAccess.MUTATING
    )
    kind = (
        _enum_value(OutcomeKind, markers["outcome_kind"], field_name="outcome_kind")
        if "outcome_kind" in markers
        else OutcomeKind.NORMAL
    )
    owner = normalize_owner(markers.get("outcome_owner"))

    return TaskExecutionClaim(
        task_id=task_id,
        board=str(board or "default"),
        outcome_id=outcome_id,
        project_id=str(_field(task, "project_id", "") or "").strip(),
        tier=tier,
        maturity=maturity,
        mode=mode,
        access=access,
        repository_scope=_derive_repository_scope(task),
        shared_authority_scope=_derive_shared_authority_scope(markers),
        kind=kind,
        owner=owner,
        status=str(_field(task, "status", "") or "").strip().casefold(),
        created_at=int(_field(task, "created_at", 0) or 0),
        priority=int(_field(task, "priority", 0) or 0),
        explicit_outcome=explicit_outcome,
    )


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    reason: str
    active_mutating: int = 0
    collides_with: tuple[str, ...] = ()
    preempt_required: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "active_mutating": self.active_mutating,
            "collides_with": list(self.collides_with),
            "preempt_required": self.preempt_required,
        }


def _unique_outcomes(
    claims: Iterable[TaskExecutionClaim],
    *,
    tier: Optional[OutcomeTier] = None,
    kind: Optional[OutcomeKind] = None,
) -> set[str]:
    result: set[str] = set()
    for claim in claims:
        if tier is not None and claim.tier is not tier:
            continue
        if kind is not None and claim.kind is not kind:
            continue
        result.add(claim.outcome_id)
    return result


def admit_execution(
    candidate: TaskExecutionClaim,
    *,
    active_claims: Sequence[TaskExecutionClaim] = (),
    portfolio_claims: Sequence[TaskExecutionClaim] = (),
    max_mutating_workers: int = DEFAULT_MAX_MUTATING_WORKERS,
    max_normal_focus_outcomes: int = DEFAULT_MAX_NORMAL_FOCUS_OUTCOMES,
    max_incident_outcomes: int = DEFAULT_MAX_INCIDENT_OUTCOMES,
) -> AdmissionDecision:
    """Fail-closed admission for durable automated execution.

    The function never stops or reclassifies existing work. An incident that
    needs capacity returns ``preempt_required=True`` so Dolly/default can choose
    what to preempt; it never manufactures an extra mutating slot beyond the configured cap.
    """

    if max_mutating_workers < 1 or max_normal_focus_outcomes < 1 or max_incident_outcomes < 1:
        return AdmissionDecision(False, "invalid_operating_model_capacity")

    try:
        normalize_owner(candidate.owner)
    except ValueError:
        return AdmissionDecision(False, "outcome_owner_conflict")

    if candidate.mode is ExecutionMode.DIRECT:
        return AdmissionDecision(False, "direct_mode_must_use_direct_path")

    if candidate.mutating and candidate.tier in {OutcomeTier.WARM, OutcomeTier.COLD}:
        tier_value = candidate.tier.value if candidate.tier is not None else "unknown"
        return AdmissionDecision(False, f"{tier_value}_outcome_cannot_mutate")

    normal_focus = _unique_outcomes(
        portfolio_claims, tier=OutcomeTier.FOCUS, kind=OutcomeKind.NORMAL
    )
    if candidate.tier is OutcomeTier.FOCUS and candidate.kind is OutcomeKind.NORMAL:
        normal_focus.add(candidate.outcome_id)
        if len(normal_focus) > max_normal_focus_outcomes:
            return AdmissionDecision(False, "focus_set_over_capacity_requires_owner")

    incidents = _unique_outcomes(portfolio_claims, kind=OutcomeKind.INCIDENT)
    if candidate.kind is OutcomeKind.INCIDENT:
        incidents.add(candidate.outcome_id)
        if len(incidents) > max_incident_outcomes:
            return AdmissionDecision(False, "incident_set_over_capacity_requires_owner")

    # A genuinely read-only specialist/review lane does not consume mutating
    # capacity and may coexist with a mutating worker in the same scope.
    if candidate.read_only:
        return AdmissionDecision(True, "read_only_separate_capacity")

    active_mutating = [claim for claim in active_claims if claim.mutating]
    collisions = tuple(
        sorted(
            claim.task_id
            for claim in active_mutating
            if claim.task_id != candidate.task_id
            and (
                claim.repository_scope == candidate.repository_scope
                or (
                    bool(candidate.shared_authority_scope)
                    and claim.shared_authority_scope == candidate.shared_authority_scope
                )
            )
        )
    )
    if collisions:
        return AdmissionDecision(
            False,
            "authority_scope_collision",
            active_mutating=len(active_mutating),
            collides_with=collisions,
            preempt_required=candidate.kind is OutcomeKind.INCIDENT,
        )

    if len(active_mutating) >= max_mutating_workers:
        return AdmissionDecision(
            False,
            "incident_preemption_required"
            if candidate.kind is OutcomeKind.INCIDENT
            else "global_mutating_capacity",
            active_mutating=len(active_mutating),
            collides_with=tuple(sorted(claim.task_id for claim in active_mutating)),
            preempt_required=candidate.kind is OutcomeKind.INCIDENT,
        )

    return AdmissionDecision(
        True,
        "mutating_capacity_available",
        active_mutating=len(active_mutating),
    )


_RESUME_REQUIRED_KEYS = (
    "project",
    "outcome",
    "last_verified_result",
    "repository",
    "works",
    "not_done",
    "frozen_acceptance",
    "next_action",
    "dependencies",
    "risks",
    "deploy_state",
    "terminal_history",
    "execution_mode",
)


def validate_resume_receipt(receipt: Any) -> list[str]:
    if not isinstance(receipt, Mapping):
        return ["resume_receipt must be an object"]
    errors: list[str] = []
    for key in _RESUME_REQUIRED_KEYS:
        if key not in receipt:
            errors.append(f"resume_receipt missing {key}")
    if "execution_mode" in receipt:
        try:
            _enum_value(ExecutionMode, receipt["execution_mode"], field_name="execution_mode")
        except ValueError as exc:
            errors.append(str(exc))
    terminal_history = receipt.get("terminal_history")
    if "terminal_history" in receipt and (
        not isinstance(terminal_history, Sequence)
        or isinstance(terminal_history, (str, bytes, bytearray))
    ):
        errors.append("resume_receipt terminal_history must be a sequence")
    return errors


def validate_outcome_state_payload(payload: Mapping[str, Any]) -> list[str]:
    """Validate optional outcome metadata on the existing repo-canon marker.

    Plain historical ``HERMES_EXECUTION_STATE`` payloads are left untouched.
    Validation activates only when an outcome-model key is present.
    """

    if not any(key in payload for key in _OUTCOME_STATE_KEYS):
        return []

    errors: list[str] = []
    try:
        normalize_owner(payload.get("outcome_owner"))
    except ValueError as exc:
        errors.append(str(exc))

    tier: Optional[OutcomeTier] = None
    if "outcome_tier" in payload:
        try:
            tier = _enum_value(OutcomeTier, payload.get("outcome_tier"), field_name="outcome_tier")
        except ValueError as exc:
            errors.append(str(exc))
    if "maturity" in payload:
        try:
            _maturity(payload.get("maturity"))
        except ValueError as exc:
            errors.append(str(exc))
    if "execution_mode" in payload:
        try:
            _enum_value(ExecutionMode, payload.get("execution_mode"), field_name="execution_mode")
        except ValueError as exc:
            errors.append(str(exc))
    if "execution_access" in payload:
        try:
            _enum_value(ExecutionAccess, payload.get("execution_access"), field_name="execution_access")
        except ValueError as exc:
            errors.append(str(exc))
    if "outcome_kind" in payload:
        try:
            _enum_value(OutcomeKind, payload.get("outcome_kind"), field_name="outcome_kind")
        except ValueError as exc:
            errors.append(str(exc))

    if tier is OutcomeTier.WARM:
        errors.extend(validate_resume_receipt(payload.get("resume_receipt")))

    if str(payload.get("outcome_result") or "").strip().casefold() == "delivered":
        receipt = payload.get("outcome_receipt")
        if not isinstance(receipt, Mapping):
            errors.append("delivered outcome requires outcome_receipt")
        else:
            if not str(receipt.get("verified_user_result") or "").strip():
                errors.append("outcome_receipt missing verified_user_result")
            evidence = receipt.get("evidence")
            if not evidence:
                errors.append("outcome_receipt missing evidence")
    return errors


def resume_warm_outcome(
    payload: Mapping[str, Any],
    *,
    current_repository_truth: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Return a focus projection from one valid warm outcome, without side effects.

    Terminal history is preserved as evidence only. This helper never touches a
    task/run row, so resuming an outcome cannot reactivate terminal runs.
    """

    if str(payload.get("outcome_tier") or "").strip().casefold() != OutcomeTier.WARM.value:
        raise ValueError("only a warm outcome can be resumed with this helper")
    errors = validate_outcome_state_payload(payload)
    if errors:
        raise ValueError("; ".join(errors))
    resumed = copy.deepcopy(dict(payload))
    resumed["outcome_tier"] = OutcomeTier.FOCUS.value
    receipt = copy.deepcopy(dict(resumed["resume_receipt"]))
    if current_repository_truth is not None:
        receipt["repository"] = dict(current_repository_truth)
    resumed["resume_receipt"] = receipt
    return resumed


def validate_focus_set(
    payloads: Iterable[Mapping[str, Any]],
    *,
    max_normal_focus: int = DEFAULT_MAX_NORMAL_FOCUS_OUTCOMES,
    max_incidents: int = DEFAULT_MAX_INCIDENT_OUTCOMES,
) -> list[str]:
    normal: set[str] = set()
    incidents: set[str] = set()
    for payload in payloads:
        tier = str(payload.get("outcome_tier") or "").strip().casefold()
        if tier != OutcomeTier.FOCUS.value:
            continue
        outcome_id = str(payload.get("outcome_id") or payload.get("contract_id") or "").strip()
        if not outcome_id:
            continue
        kind = str(payload.get("outcome_kind") or OutcomeKind.NORMAL.value).strip().casefold()
        if kind == OutcomeKind.INCIDENT.value:
            incidents.add(outcome_id)
        else:
            normal.add(outcome_id)
    errors: list[str] = []
    if len(normal) > max_normal_focus:
        errors.append(f"normal focus outcomes {len(normal)} exceed cap {max_normal_focus}")
    if len(incidents) > max_incidents:
        errors.append(f"incident outcomes {len(incidents)} exceed cap {max_incidents}")
    return errors


@dataclass(frozen=True)
class RoutingRequest:
    repositories: int = 1
    isolated_artifact: bool = False
    frozen_acceptance: bool = True
    bounded_session: bool = True
    must_survive_session: bool = False
    typed_dependencies: bool = False
    external_systems: bool = False
    planned_continuation: bool = False
    durable_audit_required: bool = False
    deploy: bool = False
    migration: bool = False
    credentials: bool = False
    production_data: bool = False
    irreversible: bool = False
    private_or_security_boundary: bool = False
    research_trigger: bool = False
    design_trigger: bool = False
    architect_triggers: tuple[str, ...] = ()
    independent_qa_trigger: bool = False
    phase: str = "implementation"


@dataclass(frozen=True)
class RoutingDecision:
    mode: ExecutionMode
    executor: str
    reason: str
    owner: str = OUTCOME_OWNER
    automatic_chain: tuple[str, ...] = field(default_factory=tuple)


def choose_execution_mode(request: RoutingRequest) -> RoutingDecision:
    """Return one execution path; never synthesize a specialist chain."""

    if any(
        (
            request.deploy,
            request.migration,
            request.credentials,
            request.production_data,
            request.irreversible,
            request.private_or_security_boundary,
        )
    ):
        return RoutingDecision(ExecutionMode.OPS, "dollyops", "ops_boundary")

    phase = str(request.phase or "implementation").strip().casefold()
    if request.research_trigger or phase == "research":
        return RoutingDecision(ExecutionMode.SPECIALIST, "dollyresearch", "critical_external_fact")
    if request.architect_triggers or phase == "architecture":
        return RoutingDecision(ExecutionMode.SPECIALIST, "dollyarchitect", "structural_architecture_trigger")
    if request.design_trigger or phase == "design":
        return RoutingDecision(ExecutionMode.SPECIALIST, "dollydesign", "design_is_central_acceptance")
    if phase in {"qa", "review"} and request.independent_qa_trigger:
        return RoutingDecision(ExecutionMode.SPECIALIST, "dollyqa", "independent_qa_risk_trigger")

    if (
        request.must_survive_session
        or request.typed_dependencies
        or request.repositories > 1
        or request.external_systems
        or request.planned_continuation
        or request.durable_audit_required
        or not request.bounded_session
    ):
        return RoutingDecision(ExecutionMode.DURABLE, "dollycode", "durable_execution_required")

    if (
        request.frozen_acceptance
        and request.bounded_session
        and (request.repositories == 1 or request.isolated_artifact)
    ):
        return RoutingDecision(ExecutionMode.DIRECT, "codex", "bounded_direct_execution")

    return RoutingDecision(ExecutionMode.DURABLE, "dollycode", "direct_contract_not_proven")


@dataclass(frozen=True)
class ReviewVerdict:
    verdict: str
    blockers: tuple[str, ...]
    accepted_risks: tuple[str, ...]


def bounded_review_verdict(
    blockers: Sequence[str], *, accepted_risks: Sequence[str] = ()
) -> ReviewVerdict:
    clean_blockers = tuple(str(item).strip() for item in blockers if str(item).strip())
    if len(clean_blockers) > 3:
        raise ValueError("bounded review permits at most three blockers")
    clean_risks = tuple(str(item).strip() for item in accepted_risks if str(item).strip())
    if clean_blockers:
        verdict = "BLOCKED"
    elif clean_risks:
        verdict = "SHIP_WITH_ACCEPTED_RISK"
    else:
        verdict = "SHIP"
    return ReviewVerdict(verdict, clean_blockers, clean_risks)


def render_portfolio_status(payloads: Iterable[Mapping[str, Any]]) -> str:
    """Render the compact user surface without worker/run/lifecycle noise."""

    rows = [dict(item) for item in payloads]
    focus: list[dict[str, Any]] = []
    incident: list[dict[str, Any]] = []
    warm: list[dict[str, Any]] = []
    decisions: list[str] = []

    for row in rows:
        tier = str(row.get("outcome_tier") or "").strip().casefold()
        kind = str(row.get("outcome_kind") or OutcomeKind.NORMAL.value).strip().casefold()
        if tier == OutcomeTier.FOCUS.value and kind == OutcomeKind.INCIDENT.value:
            incident.append(row)
        elif tier == OutcomeTier.FOCUS.value:
            focus.append(row)
        elif tier == OutcomeTier.WARM.value:
            warm.append(row)
        decision = str(row.get("decision_required") or "").strip()
        if decision:
            decisions.append(decision)

    if len(focus) > DEFAULT_MAX_NORMAL_FOCUS_OUTCOMES:
        decisions.append(
            f"Choose focus priority: {len(focus)} normal outcomes exceed the cap "
            f"of {DEFAULT_MAX_NORMAL_FOCUS_OUTCOMES}."
        )
    if len(incident) > DEFAULT_MAX_INCIDENT_OUTCOMES:
        decisions.append(
            f"Choose incident priority: {len(incident)} incidents exceed the cap "
            f"of {DEFAULT_MAX_INCIDENT_OUTCOMES}."
        )

    def line(row: Mapping[str, Any], *, is_warm: bool = False) -> str:
        project = str(row.get("project") or row.get("project_id") or "project")
        outcome = str(row.get("outcome") or row.get("outcome_id") or row.get("contract_id") or "outcome")
        maturity = str(row.get("maturity") or "-")
        mode = str(row.get("execution_mode") or "-")
        evidence = str(row.get("last_verified_evidence") or "").strip()
        next_action = str(row.get("next_action") or "").strip()
        if is_warm and isinstance(row.get("resume_receipt"), Mapping):
            receipt = row["resume_receipt"]
            evidence = evidence or str(receipt.get("last_verified_result") or "").strip()
            next_action = next_action or str(receipt.get("next_action") or "").strip()
        parts = [f"{project} — {outcome}", f"{maturity}/{mode}"]
        if evidence:
            parts.append(f"evidence: {evidence}")
        if next_action:
            parts.append(f"next: {next_action}")
        return " | ".join(parts)

    output = ["FOCUS:"]
    output.extend(f"- {line(row)}" for row in focus[:DEFAULT_MAX_NORMAL_FOCUS_OUTCOMES])
    if not focus:
        output.append("- none")
    output.append("INCIDENT:")
    if incident:
        for row in incident[:DEFAULT_MAX_INCIDENT_OUTCOMES]:
            preempts = str(row.get("preempts") or "").strip()
            rendered = line(row)
            output.append(f"- {rendered}" + (f" | preempts: {preempts}" if preempts else ""))
    else:
        output.append("- none")
    output.append("WARM:")
    output.extend(f"- {line(row, is_warm=True)}" for row in warm)
    if not warm:
        output.append("- none")
    output.append("DECISIONS:")
    output.extend(f"- {item}" for item in decisions)
    if not decisions:
        output.append("- none")
    return "\n".join(output)


def current_portfolio_payloads(hermes_home: Optional[Path] = None) -> list[dict[str, Any]]:
    """Project current project canon + running Kanban into the compact surface.

    This is read-only. Explicit outcome metadata in existing repo canon wins.
    Running legacy tasks are visible as provisional focus rows with
    ``not_verified`` mode/maturity and an owner decision instead of guessed
    migration state.
    """

    home = Path(
        hermes_home
        or os.environ.get("HERMES_HOME")
        or (Path.home() / ".hermes")
    ).expanduser()
    project_names: dict[str, str] = {}
    project_paths: list[tuple[str, str, Path]] = []
    projects_db = home / "projects.db"
    if projects_db.is_file():
        con = sqlite3.connect(f"file:{projects_db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            for row in con.execute(
                "SELECT id, name, primary_path FROM projects WHERE COALESCE(archived,0)=0"
            ):
                project_id = str(row["id"] or "").strip()
                name = str(row["name"] or project_id or "project").strip()
                path = str(row["primary_path"] or "").strip()
                if project_id:
                    project_names[project_id] = name
                if project_id and path:
                    project_paths.append((project_id, name, Path(path).expanduser()))
        finally:
            con.close()

    rows: list[dict[str, Any]] = []
    explicit_outcomes: set[str] = set()
    from hermes_cli.execution_state import read_repo_execution_states

    for project_id, project_name, project_path in project_paths:
        for filename in ("TASKS.md", "ROADMAP.md", "BACKLOG.md", "CHANGELOG.md"):
            canon = project_path / filename
            if not canon.is_file():
                continue
            try:
                states = read_repo_execution_states(canon)
            except (OSError, ValueError):
                continue
            for payload in states.values():
                if not isinstance(payload, Mapping) or not any(
                    key in payload for key in _OUTCOME_STATE_KEYS
                ):
                    continue
                tier = str(payload.get("outcome_tier") or "").strip().casefold()
                if tier not in {OutcomeTier.FOCUS.value, OutcomeTier.WARM.value}:
                    continue
                item = copy.deepcopy(dict(payload))
                outcome_id = str(
                    item.get("outcome_id") or item.get("contract_id") or ""
                ).strip()
                if not outcome_id or outcome_id in explicit_outcomes:
                    continue
                explicit_outcomes.add(outcome_id)
                item.setdefault("project_id", project_id)
                item.setdefault("project", project_name)
                item.setdefault("outcome", outcome_id)
                rows.append(item)

    board_paths: list[tuple[str, Path]] = []
    default_db = home / "kanban.db"
    if default_db.is_file():
        board_paths.append(("default", default_db))
    boards_root = home / "kanban" / "boards"
    if boards_root.is_dir():
        board_paths.extend(
            (path.parent.name, path)
            for path in sorted(boards_root.glob("*/kanban.db"))
            if not path.parent.name.startswith("_")
        )

    for board, db_path in board_paths:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
            required = {
                "id",
                "title",
                "body",
                "project_id",
                "workspace_path",
                "status",
                "created_at",
                "priority",
            }
            if not required.issubset(columns):
                continue
            tasks = con.execute(
                "SELECT id,title,body,project_id,workspace_path,status,created_at,priority "
                "FROM tasks WHERE status='running'"
            ).fetchall()
        finally:
            con.close()
        for task in tasks:
            claim = task_execution_claim(task, board=board)
            if claim.outcome_id in explicit_outcomes:
                continue
            project_name = project_names.get(claim.project_id, claim.project_id or board)
            title = str(task["title"] or claim.outcome_id).strip()
            if claim.explicit_outcome and claim.tier is not None:
                rows.append(
                    {
                        "project_id": claim.project_id,
                        "project": project_name,
                        "outcome_id": claim.outcome_id,
                        "outcome": title,
                        "outcome_tier": claim.tier.value,
                        "outcome_kind": claim.kind.value,
                        "maturity": claim.maturity.value if claim.maturity else "-",
                        "execution_mode": claim.mode.value,
                        "next_action": "Continue current bounded execution.",
                    }
                )
                explicit_outcomes.add(claim.outcome_id)
                continue
            rows.append(
                {
                    "project_id": claim.project_id,
                    "project": project_name,
                    "outcome_id": claim.outcome_id,
                    "outcome": title,
                    "outcome_tier": OutcomeTier.FOCUS.value,
                    "outcome_kind": OutcomeKind.NORMAL.value,
                    "maturity": "-",
                    "execution_mode": "not_verified",
                    "next_action": "Map current legacy execution to its outcome receipt.",
                    "decision_required": (
                        f"Dolly/default must map active legacy work '{title}' "
                        "to the current outcome before reclassification."
                    ),
                }
            )
    return rows


def render_current_portfolio_status(hermes_home: Optional[Path] = None) -> str:
    return render_portfolio_status(current_portfolio_payloads(hermes_home))
