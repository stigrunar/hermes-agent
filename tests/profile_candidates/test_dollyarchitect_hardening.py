"""Behavior contracts for the inactive DollyArchitect hardening candidate."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from profile_candidates.dollyarchitect import (
    ARCHITECTURE_FIT_KINDS,
    ARCHITECTURE_METRICS,
    PROFILE_POLICY,
    ArchitectureDecisionPacket,
    ArchitectureDispatchContract,
    ContractValidationError,
    PathGuardError,
    WorkKind,
    classify_architect_fit,
    guard_write_target,
    packet_to_dollycode_handoff,
    validate_architecture_contract,
    validate_measurement_receipt,
)
from hermes_cli.profiles import read_profile_meta


@pytest.mark.parametrize(
    "work_kind",
    [
        "ontology",
        "scenario_grammar",
        "code_vs_data",
        "shared_harness_adapter_ui_primitive",
        "cross_repo_contract",
        "migration_seam",
        "materially_different_second_scenario_reuse",
    ],
)
def test_explicit_architect_fit_matrix_is_accepted(work_kind: str):
    decision = classify_architect_fit({"work_kind": work_kind})

    assert decision.accepted is True
    assert decision.route == "DollyArchitect"
    assert decision.work_kind in ARCHITECTURE_FIT_KINDS


@pytest.mark.parametrize(
    ("work_kind", "route"),
    [
        ("model_evaluation", "DollyQA"),
        ("portfolio_evaluation", "DollyQA"),
        ("benchmark", "DollyQA"),
        ("implementation_code_patch", "DollyCode"),
        ("routine_qa_review", "DollyQA"),
        ("visual_design", "DollyDesign"),
        ("release", "DollyOps"),
        ("pull_request", "DollyOps"),
        ("deploy", "DollyOps"),
    ],
)
def test_non_architect_work_is_deterministically_classified_for_named_owner(
    work_kind: str, route: str
):
    decision = classify_architect_fit({"work_kind": work_kind})

    assert decision.accepted is False
    assert decision.route == route
    assert decision.reason_code == "explicit_non_architect_work_kind"


@pytest.mark.parametrize(
    "payload",
    [
        {"work_kind": "architecture_maybe"},
        {"work_kind": "ontology", "prompt": "fuzzy input is forbidden"},
        {},
    ],
)
def test_fit_filter_rejects_unknown_values_and_fields(payload: dict[str, object]):
    with pytest.raises(ContractValidationError):
        classify_architect_fit(payload)


def _valid_contract_payload(tmp_path: Path) -> dict[str, object]:
    workspace = tmp_path / "workspace"
    artifact_root = workspace / "artifacts"
    return {
        "contract_id": "architecture-1",
        "work_kind": WorkKind.CROSS_REPO_CONTRACT.value,
        "workspace_kind": "scratch",
        "writable_artifact_roots": [],
        "architecture_document_paths": [],
        "requested_actions": ["architecture_decision"],
        "implementation_owner": None,
        "operations_owner": None,
        "project_id": "project-1",
        "repository_identity": "repo-1",
        "implementation_repo": "/repos/repo-1",
        "implementation_workspace_policy": (
            "preparation_only_requires_distinct_workspace"
        ),
        "bounded_file_cluster": ["agent/profile_runtime_policy.py"],
        "non_goals": ["Implementation"],
    }


def test_valid_contract_is_accepted_before_dispatch(tmp_path: Path):
    contract = ArchitectureDispatchContract.from_mapping(
        _valid_contract_payload(tmp_path)
    )

    assert validate_architecture_contract(contract) is contract


def test_handoff_only_contract_rejects_document_paths(tmp_path: Path):
    payload = _valid_contract_payload(tmp_path)
    payload["architecture_document_paths"] = [
        str(tmp_path / "workspace" / "artifacts" / "architecture.md")
    ]
    contract = ArchitectureDispatchContract.from_mapping(payload)

    with pytest.raises(
        ContractValidationError,
        match="handoff-only actions require empty",
    ):
        validate_architecture_contract(contract)


def test_contract_parser_rejects_unknown_fields(tmp_path: Path):
    payload = _valid_contract_payload(tmp_path)
    payload["surprise"] = True

    with pytest.raises(ContractValidationError, match="unknown=.*surprise"):
        ArchitectureDispatchContract.from_mapping(payload)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            {"requested_actions": ["no_edits", "commit"]},
            "exactly one architecture capability",
        ),
        (
            {
                "requested_actions": ["architecture_decision", "implementation"],
                "implementation_owner": None,
            },
            "exactly one architecture capability",
        ),
        (
            {
                "requested_actions": ["architecture_decision", "release"],
                "operations_owner": None,
            },
            "exactly one architecture capability",
        ),
        (
            {
                "requested_actions": ["architecture_decision", "implementation"],
                "implementation_owner": "DollyCode",
            },
            "exactly one architecture capability",
        ),
        (
            {
                "requested_actions": ["architecture_decision", "deploy"],
                "operations_owner": "DollyOps",
            },
            "exactly one architecture capability",
        ),
        (
            {
                "requested_actions": ["write_architecture_document"],
                "writable_artifact_roots": [],
                "architecture_document_paths": ["/workspace/artifacts/architecture.md"],
            },
            "at least one path",
        ),
        (
            {
                "requested_actions": ["write_architecture_document"],
                "writable_artifact_roots": ["relative/artifacts"],
                "architecture_document_paths": ["/absolute/architecture.md"],
            },
            "must be absolute",
        ),
        (
            {
                "requested_actions": ["write_architecture_document"],
                "writable_artifact_roots": ["/absolute/artifacts/*"],
                "architecture_document_paths": ["/absolute/artifacts/architecture.md"],
            },
            "glob syntax",
        ),
    ],
)
def test_contradictory_or_ambiguous_contracts_are_rejected_pre_dispatch(
    tmp_path: Path, updates: dict[str, object], message: str
):
    payload = _valid_contract_payload(tmp_path)
    payload.update(updates)
    contract = ArchitectureDispatchContract.from_mapping(payload)

    with pytest.raises(ContractValidationError, match=message):
        validate_architecture_contract(contract)


def test_overlapping_writable_roots_are_ambiguous(tmp_path: Path):
    payload = _valid_contract_payload(tmp_path)
    parent = tmp_path / "workspace" / "artifacts"
    payload["requested_actions"] = ["write_architecture_document"]
    payload["writable_artifact_roots"] = [str(parent), str(parent / "nested")]
    payload["architecture_document_paths"] = [str(parent / "architecture.md")]
    contract = ArchitectureDispatchContract.from_mapping(payload)

    with pytest.raises(ContractValidationError, match="must not overlap"):
        validate_architecture_contract(contract)


def _scratch_workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True)
    return workspace, artifacts


def test_scratch_guard_accepts_resolved_target_under_assigned_root(tmp_path: Path):
    workspace, artifacts = _scratch_workspace(tmp_path)
    target = artifacts / "decision.md"

    resolved = guard_write_target(
        target=str(target),
        hermes_kanban_workspace=str(workspace),
        artifact_roots=[str(artifacts)],
        workspace_kind="scratch",
        architecture_document_paths=[str(target)],
    )

    assert resolved == target.resolve()


@pytest.mark.parametrize("missing_workspace", [None, ""])
def test_guard_fails_closed_without_workspace(
    tmp_path: Path, missing_workspace: str | None
):
    _, artifacts = _scratch_workspace(tmp_path)

    with pytest.raises(PathGuardError, match="HERMES_KANBAN_WORKSPACE"):
        guard_write_target(
            target=str(artifacts / "decision.md"),
            hermes_kanban_workspace=missing_workspace,
            artifact_roots=[str(artifacts)],
            workspace_kind="scratch",
            architecture_document_paths=[str(artifacts / "decision.md")],
        )


def test_guard_rejects_traversal_even_when_it_would_normalize_inside(tmp_path: Path):
    workspace, artifacts = _scratch_workspace(tmp_path)
    traversal = artifacts / "drafts" / ".." / "decision.md"

    with pytest.raises(PathGuardError, match="traversal-free"):
        guard_write_target(
            target=str(traversal),
            hermes_kanban_workspace=str(workspace),
            artifact_roots=[str(artifacts)],
            workspace_kind="scratch",
            architecture_document_paths=[str(artifacts / "decision.md")],
        )


def test_guard_rejects_outside_target(tmp_path: Path):
    workspace, artifacts = _scratch_workspace(tmp_path)
    outside = tmp_path / "outside" / "decision.md"

    with pytest.raises(PathGuardError, match="exactly one assigned artifact root"):
        guard_write_target(
            target=str(outside),
            hermes_kanban_workspace=str(workspace),
            artifact_roots=[str(artifacts)],
            workspace_kind="scratch",
        )


def test_guard_rejects_target_symlink_escape(tmp_path: Path):
    workspace, artifacts = _scratch_workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = artifacts / "escape"
    escape.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathGuardError, match="exactly one assigned artifact root"):
        guard_write_target(
            target=str(escape / "decision.md"),
            hermes_kanban_workspace=str(workspace),
            artifact_roots=[str(artifacts)],
            workspace_kind="scratch",
        )


def test_guard_rejects_artifact_root_symlink_escape(tmp_path: Path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    linked_root = workspace / "artifacts"
    linked_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathGuardError, match="strict descendants"):
        guard_write_target(
            target=str(linked_root / "decision.md"),
            hermes_kanban_workspace=str(workspace),
            artifact_roots=[str(linked_root)],
            workspace_kind="scratch",
            architecture_document_paths=[str(linked_root / "decision.md")],
        )


def test_guard_rejects_missing_and_ambiguous_artifact_roots(tmp_path: Path):
    workspace, artifacts = _scratch_workspace(tmp_path)

    with pytest.raises(PathGuardError, match="explicit artifact roots"):
        guard_write_target(
            target=str(artifacts / "decision.md"),
            hermes_kanban_workspace=str(workspace),
            artifact_roots=[],
            workspace_kind="scratch",
            architecture_document_paths=[str(artifacts / "decision.md")],
        )
    with pytest.raises(PathGuardError, match="ambiguous after resolution"):
        guard_write_target(
            target=str(artifacts / "decision.md"),
            hermes_kanban_workspace=str(workspace),
            artifact_roots=[str(artifacts), str(artifacts)],
            workspace_kind="scratch",
            architecture_document_paths=[str(artifacts / "decision.md")],
        )
    with pytest.raises(PathGuardError, match="missing or unresolvable"):
        guard_write_target(
            target=str(artifacts / "decision.md"),
            hermes_kanban_workspace=str(workspace),
            artifact_roots=[str(workspace / "missing")],
            workspace_kind="scratch",
            architecture_document_paths=[str(artifacts / "decision.md")],
        )


def test_worktree_contract_requires_exactly_one_document(tmp_path: Path):
    payload = _valid_contract_payload(tmp_path)
    payload["workspace_kind"] = "worktree"
    payload["requested_actions"] = ["write_architecture_document"]
    payload["writable_artifact_roots"] = [
        str(tmp_path / "workspace" / "artifacts")
    ]

    for documents in ([], ["/a.md", "/b.md"]):
        payload["architecture_document_paths"] = documents
        contract = ArchitectureDispatchContract.from_mapping(payload)
        with pytest.raises(ContractValidationError, match="exactly one"):
            validate_architecture_contract(contract)

    payload["architecture_document_paths"] = [
        str(tmp_path / "workspace" / "artifacts" / "architecture.md")
    ]
    contract = ArchitectureDispatchContract.from_mapping(payload)
    assert validate_architecture_contract(contract) is contract


@pytest.mark.parametrize(
    "document",
    [
        "/outside/architecture.md",
        "src/architecture.md",
        "architecture.py",
        "architecture.lock",
    ],
)
def test_worktree_contract_rejects_outside_or_source_like_document(
    tmp_path: Path, document: str
):
    payload = _valid_contract_payload(tmp_path)
    payload["workspace_kind"] = "worktree"
    payload["requested_actions"] = ["write_architecture_document"]
    payload["writable_artifact_roots"] = [
        str(tmp_path / "workspace" / "artifacts")
    ]
    artifact_root = Path(payload["writable_artifact_roots"][0])
    payload["architecture_document_paths"] = [
        document if Path(document).is_absolute() else str(artifact_root / document)
    ]
    contract = ArchitectureDispatchContract.from_mapping(payload)

    with pytest.raises(ContractValidationError):
        validate_architecture_contract(contract)


def test_worktree_guard_allows_only_named_architecture_document(tmp_path: Path):
    workspace, artifacts = _scratch_workspace(tmp_path)
    document = artifacts / "architecture.md"

    resolved = guard_write_target(
        target=str(document),
        hermes_kanban_workspace=str(workspace),
        artifact_roots=[str(artifacts)],
        workspace_kind="worktree",
        architecture_document_paths=[str(document)],
    )
    assert resolved == document.resolve()

    with pytest.raises(PathGuardError, match="limited to the named"):
        guard_write_target(
            target=str(artifacts / "other.md"),
            hermes_kanban_workspace=str(workspace),
            artifact_roots=[str(artifacts)],
            workspace_kind="worktree",
            architecture_document_paths=[str(document)],
        )


@pytest.mark.parametrize(
    "relative_target",
    ["architecture.py", "src/architecture.md", "notes/architecture.lock"],
)
def test_worktree_guard_rejects_source_like_or_non_document_targets(
    tmp_path: Path, relative_target: str
):
    workspace, artifacts = _scratch_workspace(tmp_path)
    target = artifacts / relative_target
    target.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(PathGuardError):
        guard_write_target(
            target=str(target),
            hermes_kanban_workspace=str(workspace),
            artifact_roots=[str(artifacts)],
            workspace_kind="worktree",
            architecture_document_paths=[str(target)],
        )


def test_staged_profile_policy_is_least_privilege_and_profile_local():
    assert PROFILE_POLICY.status == "inactive_candidate"
    assert PROFILE_POLICY.activated is False
    assert PROFILE_POLICY.default_workspace_kind == "scratch"
    assert (
        PROFILE_POLICY.model,
        PROFILE_POLICY.reasoning,
        PROFILE_POLICY.max_turns,
    ) == ("gpt-5.6-sol", "high", 60)
    assert set(PROFILE_POLICY.allowed_capabilities) == {
        "read_search",
        "knowledge_code_intel",
        "kanban",
        "session_search",
        "guarded_assigned_artifact_write",
    }
    assert set(PROFILE_POLICY.disabled_capabilities) == {
        "normal_cron",
        "delegation",
        "computer_use",
        "media_image",
        "github_write",
        "deploy_release",
    }
    assert PROFILE_POLICY.terminal_enabled is False
    assert "command, cwd, and resolved-path" in PROFILE_POLICY.terminal_disabled_reason
    assert PROFILE_POLICY.hindsight_enabled is False
    assert PROFILE_POLICY.memory_continuity == ("knowledge", "session_search")
    assert "HINDSIGHT_API_KEY" in PROFILE_POLICY.memory_disabled_reason_code
    assert "HINDSIGHT_LLM_API_KEY" in PROFILE_POLICY.memory_disabled_reason_code
    assert set(PROFILE_POLICY.active_priority_exclusions) == {
        "contract-driven-frontend-implementation",
        "mobile-ui-verification",
        "release-candidate-evidence",
        "external-upstream-pr-recuts",
    }
    assert PROFILE_POLICY.exclusions_scope == "profile_local_only"
    assert PROFILE_POLICY.shared_skills_mutation is False


def test_profile_artifact_matches_executable_policy():
    profile_path = (
        Path(__file__).parents[2]
        / "profile_candidates"
        / "dollyarchitect"
        / "profile.json"
    )
    profile = json.loads(profile_path.read_text(encoding="utf-8"))

    assert profile["status"] == PROFILE_POLICY.status
    assert profile["workspace"]["default_kind"] == PROFILE_POLICY.default_workspace_kind
    assert profile["model"] == {
        "name": PROFILE_POLICY.model,
        "reasoning": PROFILE_POLICY.reasoning,
        "max_turns": PROFILE_POLICY.max_turns,
    }
    assert profile["least_privilege"]["allowed"] == list(
        PROFILE_POLICY.allowed_capabilities
    )
    assert profile["least_privilege"]["disabled"] == list(
        PROFILE_POLICY.disabled_capabilities
    )
    assert (
        profile["least_privilege"]["terminal_enabled"]
        is PROFILE_POLICY.terminal_enabled
    )
    assert (
        profile["least_privilege"]["terminal_disabled_reason"]
        == PROFILE_POLICY.terminal_disabled_reason
    )
    assert profile["memory"] == {
        "hindsight_enabled": PROFILE_POLICY.hindsight_enabled,
        "continuity": list(PROFILE_POLICY.memory_continuity),
        "reason_code": PROFILE_POLICY.memory_disabled_reason_code,
    }
    exclusions = profile["active_priority_exclusions"]
    assert exclusions["scope"] == PROFILE_POLICY.exclusions_scope
    assert exclusions["values"] == list(PROFILE_POLICY.active_priority_exclusions)
    assert exclusions["mutate_shared_skills"] is PROFILE_POLICY.shared_skills_mutation
    assert exclusions["supported_config_path"] == "skills.disabled"


def _decision_packet() -> ArchitectureDecisionPacket:
    return ArchitectureDecisionPacket.from_mapping(
        {
            "packet_id": "packet-1",
            "decision": "Use a versioned adapter boundary.",
            "rationale": "Two materially different scenarios share the contract.",
            "constraints": ["Preserve prompt caching.", "No runtime wiring."],
            "acceptance_criteria": ["DollyCode tests both scenarios."],
            "dollycode_owner": "DollyCode",
            "architecture_artifact": "inline:architecture-decision",
            "validation_hypothesis": "The adapter passes two scenario tests.",
        }
    )


def test_packet_emits_exactly_one_separate_handoff_and_no_implementation():
    packet = _decision_packet()

    contract = validate_architecture_contract(
        ArchitectureDispatchContract.from_mapping(
            _valid_contract_payload(Path("/tmp"))
        )
    )
    artifact_hash = "a" * 64
    emission = packet_to_dollycode_handoff(
        packet,
        contract=contract,
        source_task_id="t_source",
        architecture_artifact_sha256=artifact_hash,
    )
    repeated = packet_to_dollycode_handoff(
        packet,
        contract=contract,
        source_task_id="t_source",
        architecture_artifact_sha256=artifact_hash,
    )

    assert len(emission.handoffs) == 1
    assert emission.implementation_actions == ()
    assert emission == repeated
    handoff = emission.handoffs[0]
    assert type(handoff) is not type(packet)
    assert handoff.source_packet_id == packet.packet_id
    assert handoff.owner == "DollyCode"
    assert handoff.requested_action == "implement_from_architecture_decision"
    assert handoff.source_task_id == "t_source"
    assert handoff.dispatch_contract_id == contract.contract_id
    assert handoff.architecture_artifact_sha256 == artifact_hash


def test_reviewed_profile_description_reads_back_through_real_metadata_reader():
    candidate = (
        Path(__file__).parents[2]
        / "profile_candidates"
        / "dollyarchitect"
    )
    expected = json.loads(
        (candidate / "profile.json").read_text(encoding="utf-8")
    )["description"]

    assert read_profile_meta(candidate) == {
        "description": expected,
        "description_auto": False,
    }


def _valid_measurement_receipt() -> dict[str, object]:
    metrics = {
        "implementation_without_redesign": True,
        "first_pass_QA": True,
        "architecture_related_escaped_defects": 0,
        "role_leakage": False,
        "timeout": False,
        "architecture_to_green_code_time": 42.5,
    }
    return {
        "scope": "candidate_local",
        "telemetry_activation": False,
        "packages": [
            {"package_id": f"package-{index}", "metrics": dict(metrics)}
            for index in range(5)
        ],
    }


def test_five_package_measurement_schema_has_exact_metrics_and_no_telemetry():
    receipt = _valid_measurement_receipt()

    assert validate_measurement_receipt(receipt) is receipt
    assert len(receipt["packages"]) == 5
    for package in receipt["packages"]:
        assert tuple(package["metrics"]) == ARCHITECTURE_METRICS


def test_measurement_schema_artifact_matches_executable_contract():
    schema_path = (
        Path(__file__).parents[2]
        / "profile_candidates"
        / "dollyarchitect"
        / "measurement_schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    packages = schema["properties"]["packages"]
    metric_properties = packages["items"]["properties"]["metrics"]["properties"]

    assert packages["minItems"] == packages["maxItems"] == 5
    assert schema["properties"]["scope"]["const"] == "candidate_local"
    assert schema["properties"]["telemetry_activation"]["const"] is False
    assert tuple(metric_properties) == ARCHITECTURE_METRICS
    assert metric_properties.keys() == set(
        packages["items"]["properties"]["metrics"]["required"]
    )


@pytest.mark.parametrize(
    "mutation",
    ["sixth_package", "extra_metric", "telemetry", "negative_elapsed"],
)
def test_measurement_schema_fails_closed(mutation: str):
    receipt = _valid_measurement_receipt()
    packages = receipt["packages"]
    if mutation == "sixth_package":
        packages.append(
            {"package_id": "package-5", "metrics": dict(packages[0]["metrics"])}
        )
    elif mutation == "extra_metric":
        packages[0]["metrics"]["unapproved_metric"] = 1
    elif mutation == "telemetry":
        receipt["telemetry_activation"] = True
    else:
        packages[0]["metrics"]["architecture_to_green_code_time"] = -1

    with pytest.raises(ContractValidationError):
        validate_measurement_receipt(receipt)


def test_install_manifest_allows_private_review_ref_without_activation_or_public_write():
    manifest_path = (
        Path(__file__).parents[2]
        / "profile_candidates"
        / "dollyarchitect"
        / "install_rollback_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["status"] == "inactive_candidate"
    assert manifest["activation"] is False
    assert "candidate_commit" not in manifest
    assert "candidate_tree" not in manifest
    assert "deferred_runtime_wiring_seam" not in manifest
    assert manifest["rollback_actions"]
    assert manifest["candidate_commit_policy"] == {
        "local_candidate_commit_permitted": True,
        "commit_is_activation": False,
        "immutable_private_review_ref_permitted": True,
        "public_or_release_push_permitted": False,
    }
    mutation_boundary = " ".join(manifest["live_no_mutation_boundary"]).casefold()
    assert "local candidate commit is permitted" in mutation_boundary
    assert "does not install or activate" in mutation_boundary
    assert "no live profile or shared skill changes" in mutation_boundary
    assert "one immutable private-review ref is permitted" in mutation_boundary
    assert "no public/main/release push, pull request, release, or deploy" in (
        mutation_boundary
    )
    assert manifest["post_closeout_sequence"][0].startswith("Hermes creates")
    assert manifest["contract_id"] == "ARCHITECT-HARDEN-02-SKRUE-RECUT-R1"
    assert "profile_candidates/dollyarchitect/profile.yaml" in manifest["source_files"]
    assert "hermes_cli/kanban_decompose.py" in manifest["source_files"]
    assert "tools/kanban_tools.py" in manifest["source_files"]


def test_profile_policy_is_immutable_value_data():
    copied = asdict(PROFILE_POLICY)

    assert copied["status"] == "inactive_candidate"
    with pytest.raises(AttributeError):
        PROFILE_POLICY.status = "active"
