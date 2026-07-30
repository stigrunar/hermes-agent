"""Pure execution-contract compatibility checks shared by intake surfaces."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_NO_DEPLOY_AUTHORITY_NAMES = {"no_deploy", "no-deploy"}
_NO_DEPLOY_ACCEPTANCE_TERMS = (
    "deploy",
    "deployment",
    "live qa",
    "live verification",
    "live acceptance",
    "actual target",
    "served release",
    "runtime activation",
)


def authority_names(value: Any) -> list[str]:
    """Normalize an authority declaration without retaining payload prose."""
    if isinstance(value, str):
        return [value.strip().casefold()] if value.strip() else []
    if isinstance(value, Mapping):
        return sorted(
            {str(key).strip().casefold() for key, enabled in value.items() if enabled}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return sorted(
            {
                str(item).strip().casefold()
                for item in value
                if isinstance(item, str) and item.strip()
            }
        )
    return []


def no_deploy_acceptance_mismatches(envelope: Mapping[str, Any]) -> list[str]:
    """Return incompatible field names for a declared no-deploy source.

    Only field names are returned. Candidate prose is never copied into the
    result, so callers may safely persist or display the deterministic error.
    """
    authority = authority_names(envelope.get("authority"))
    if not any(name in _NO_DEPLOY_AUTHORITY_NAMES for name in authority):
        return []

    mismatches: list[str] = []
    for field_name in ("acceptance", "stop_when"):
        value = envelope.get(field_name)
        values = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []
        text = "\n".join(item for item in values if isinstance(item, str)).casefold()
        if any(term in text for term in _NO_DEPLOY_ACCEPTANCE_TERMS):
            mismatches.append(field_name)
    return mismatches
