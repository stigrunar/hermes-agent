"""Inactive, pure DollyArchitect candidate hardening components."""

from .hardening import (
    ARCHITECTURE_FIT_KINDS,
    ARCHITECTURE_METRICS,
    PROFILE_POLICY,
    ArchitectureDecisionPacket,
    ArchitectureDispatchContract,
    ContractValidationError,
    FitDecision,
    HandoffEmission,
    ImplementationWorkspacePolicy,
    PathGuardError,
    WorkKind,
    classify_architect_fit,
    guard_write_target,
    packet_to_dollycode_handoff,
    validate_architecture_contract,
    validate_measurement_receipt,
)

__all__ = [
    "ARCHITECTURE_FIT_KINDS",
    "ARCHITECTURE_METRICS",
    "PROFILE_POLICY",
    "ArchitectureDecisionPacket",
    "ArchitectureDispatchContract",
    "ContractValidationError",
    "FitDecision",
    "HandoffEmission",
    "ImplementationWorkspacePolicy",
    "PathGuardError",
    "WorkKind",
    "classify_architect_fit",
    "guard_write_target",
    "packet_to_dollycode_handoff",
    "validate_architecture_contract",
    "validate_measurement_receipt",
]
