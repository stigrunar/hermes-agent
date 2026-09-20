"""Central, fail-open Jev decision evaluation for Hermes shadow pilots.

This module has no routing authority. Its first consumer observes new DollyCode
technical task contracts, writes metadata-only JSONL receipts, and leaves the
existing Hermes result unchanged on success, timeout, malformed response, or
transport failure.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from agent.redact import redact_sensitive_text
from hermes_constants import get_default_hermes_root

OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.13"
QUESTION_ID = "technical_task_classification"
EVIDENCE_QUESTION_ID = "enough_evidence"
ALLOWED_CHOICES = frozenset({
    "settled_implementation",
    "bounded_read",
    "hard_invariant",
    "insufficient_evidence",
})

CHOICE_PROBABILITY_THRESHOLD = 0.90
CHOICE_MARGIN_THRESHOLD = 0.30
CHOICE_CONFIDENCE_THRESHOLD = 0.80
EVIDENCE_THRESHOLD = 0.90

# Stricter than ordinary log redaction. Jev never needs credential-bearing
# assignments, browser/cookie dumps, or raw environment material to classify a
# technical task.
_SENSITIVE_LINE = re.compile(
    r"(?i)(?:^|\b)(?:[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|COOKIE)|"
    r"authorization|set-cookie|\.env)(?:\b|\s*[:=])"
)
_LONG_OPAQUE = re.compile(r"\b[A-Za-z0-9_\-+/=]{48,}\b")
_MAX_ITEM_CHARS = 2_000
_MAX_LIST_ITEMS = 12


def _filtered_text(value: Any) -> str:
    """Return bounded task text with secret-like material removed."""
    compact = " ".join(str(value or "").split())[:_MAX_ITEM_CHARS]
    if not compact:
        return ""
    if _SENSITIVE_LINE.search(compact):
        return "[filtered sensitive field]"
    compact = redact_sensitive_text(compact)
    return _LONG_OPAQUE.sub("[filtered opaque value]", compact)


def _filtered_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value[:_MAX_LIST_ITEMS]:
        text = _filtered_text(item)
        if text:
            result.append(text)
    return result


def build_dollycode_shadow_state(
    *, title: Any, execution_contract: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the allowlisted state sent to Jev.

    Card background, comments, logs, environment, browser/session data,
    credentials, user/profile context, permissions, leases, model selection and
    routing fields are intentionally absent.
    """
    return {
        "task_title": _filtered_text(title),
        "outcome": _filtered_text(execution_contract.get("outcome")),
        "quality_mode": _filtered_text(execution_contract.get("quality_mode")),
        "frozen_acceptance": _filtered_list(
            execution_contract.get("frozen_acceptance")
        ),
        "mutation_scope": _filtered_list(execution_contract.get("mutation_scope")),
        "will_not_do": _filtered_list(execution_contract.get("will_not_do")),
        "verification": _filtered_list(execution_contract.get("verification")),
        "stop_when": _filtered_list(execution_contract.get("stop_when")),
    }


def _snapshot(state: Mapping[str, Any]) -> tuple[str, str, bytes]:
    encoded = json.dumps(
        state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"dollycode-technical-task-v1:{digest[:16]}", digest, encoded


def _load_openrouter_key() -> str:
    """Resolve the caller's profile-scoped OpenRouter credential.

    Shadow evaluation must honor Hermes secret isolation. In multiplex mode an
    unscoped read fails closed instead of reading another profile's environment.
    """
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret

        try:
            return str(get_secret("OPENROUTER_API_KEY") or "").strip()
        except UnscopedSecretError:
            return ""
    except Exception:
        return str(os.environ.get("OPENROUTER_API_KEY") or "").strip()


def _choice_question() -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": (
            "Classify only the technical task shape described in state. Do not "
            "decide assignee, model, permissions, execution mode, leases, budget, "
            "retry, terminal state, ownership, or routing. Which label best fits?"
        ),
        "criteria": {
            "settled_implementation": (
                "A concrete implementation/fix is requested with sufficiently "
                "settled outcome, bounded scope, and observable acceptance."
            ),
            "bounded_read": (
                "The requested work is inspection, diagnosis, inventory, review, "
                "or another read-only evidence task with no implementation write."
            ),
            "hard_invariant": (
                "Correct work depends on a hard authority, security, privacy, "
                "concurrency, migration, source-of-truth, ownership, or irreversible "
                "state invariant that must remain deterministic code/policy."
            ),
            "insufficient_evidence": (
                "The state does not contain enough evidence to distinguish the "
                "other labels reliably."
            ),
        },
    }


def _evidence_question() -> dict[str, Any]:
    return {
        "type": "noul",
        "instructions": (
            "Does the supplied state contain enough concrete technical evidence "
            "to distinguish the execution class without assuming absent facts?"
        ),
        "criteria": {
            "true": (
                "The requested outcome, scope and completion conditions are "
                "sufficiently bounded for classification."
            ),
            "false": (
                "A decisive fact is absent, contradictory, or only assumed."
            ),
        },
    }


def _numeric_usage(value: Any) -> dict[str, int | float]:
    """Keep only numeric usage/cost fields actually reported by the API."""
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cost",
        "total_cost",
    }
    return {
        str(key): item
        for key, item in value.items()
        if str(key) in allowed
        and isinstance(item, (int, float))
        and not isinstance(item, bool)
    }


def _noul_probability(value: Any) -> float | None:
    """Extract a true-probability from known Jev/OpenRouter Noul shapes."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, Mapping):
        return None
    for key in ("noul", "probability", "value", "true_probability", "true"):
        item = value.get(key)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            return float(item)
    probabilities = value.get("probabilities")
    if isinstance(probabilities, Mapping):
        for key in ("true", "yes", "1"):
            item = probabilities.get(key)
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                return float(item)
    return None


def _decision_gate(
    *, proposal: str, probabilities: Mapping[str, float], confidence: float | None,
    evidence_probability: float | None
) -> tuple[bool, dict[str, float | None]]:
    p1 = probabilities.get(proposal)
    ordered = sorted(probabilities.values(), reverse=True)
    p2 = ordered[1] if len(ordered) > 1 else 0.0
    margin = (p1 - p2) if p1 is not None else None
    passed = (
        p1 is not None
        and p1 >= CHOICE_PROBABILITY_THRESHOLD
        and margin is not None
        and margin >= CHOICE_MARGIN_THRESHOLD
        and confidence is not None
        and confidence >= CHOICE_CONFIDENCE_THRESHOLD
        and evidence_probability is not None
        and evidence_probability >= EVIDENCE_THRESHOLD
    )
    return passed, {
        "p1": p1,
        "margin": margin,
        "confidence": confidence,
        "evidence_probability": evidence_probability,
    }


def _log_path() -> Path:
    return (
        get_default_hermes_root()
        / "logs"
        / "jev-shadow"
        / "dollycode-technical-task.jsonl"
    )


def _append_receipt(receipt: Mapping[str, Any]) -> None:
    path = _log_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = (
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def evaluate_dollycode_task_shadow(
    *,
    title: Any,
    execution_contract: Mapping[str, Any],
    task_id: str | None = None,
    enabled: bool = False,
    timeout_seconds: float = 5.0,
) -> dict[str, Any] | None:
    """Evaluate and log one DollyCode task without affecting its creation."""
    if not enabled:
        return None

    state = build_dollycode_shadow_state(
        title=title, execution_contract=execution_contract
    )
    snapshot_id, snapshot_hash, _ = _snapshot(state)
    started = time.monotonic()
    receipt: dict[str, Any] = {
        "schema": "hermes.jev-shadow.v1",
        "consumer": "dollycode_technical_task_classification",
        "task_id": task_id,
        "snapshot_id": snapshot_id,
        "snapshot_sha256": snapshot_hash,
        "pinned_model": JEV_MODEL,
        "proposal": None,
        "probability": None,
        "confidence": None,
        "probabilities": {},
        "evidence_probability": None,
        "shadow_gate_passed": False,
        "gate_metrics": {},
        "latency_ms": None,
        "gate_result": "fail_open_unclassified",
        "usage": {},
        "cost": None,
    }

    try:
        api_key = _load_openrouter_key()
        if not api_key:
            receipt["gate_result"] = "fail_open_missing_credential"
            receipt["error_type"] = "MissingCredential"
            return receipt

        payload = {
            "model": JEV_MODEL,
            "state": state,
            "questions": {
                QUESTION_ID: _choice_question(),
                EVIDENCE_QUESTION_ID: _evidence_question(),
            },
        }
        request = urllib.request.Request(
            OPENROUTER_DECISIONS_URL,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Hermes-Jev-Shadow/1",
            },
            method="POST",
        )
        with urllib.request.urlopen(
            request, timeout=max(0.1, timeout_seconds)
        ) as response:
            parsed = json.loads(response.read().decode("utf-8"))

        answers = parsed.get("answers") or {}
        answer = answers.get(QUESTION_ID) or {}
        proposal = answer.get("choice")
        if answer.get("type") not in (None, "choice") or proposal not in ALLOWED_CHOICES:
            raise ValueError("malformed Jev choice response")
        probabilities = answer.get("probabilities") or {}
        normalized_probabilities = {
            choice: float(probabilities[choice])
            for choice in ALLOWED_CHOICES
            if isinstance(probabilities.get(choice), (int, float))
            and not isinstance(probabilities.get(choice), bool)
        }
        confidence = answer.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            confidence = None
        else:
            confidence = float(confidence)

        evidence_probability = _noul_probability(
            answers.get(EVIDENCE_QUESTION_ID)
        )
        gate_passed, gate_metrics = _decision_gate(
            proposal=proposal,
            probabilities=normalized_probabilities,
            confidence=confidence,
            evidence_probability=evidence_probability,
        )
        usage = _numeric_usage(parsed.get("usage"))
        cost = usage.get("cost", usage.get("total_cost"))

        receipt.update(
            {
                "resolved_model": str(parsed.get("model") or JEV_MODEL),
                "proposal": proposal,
                "probability": normalized_probabilities.get(proposal),
                "confidence": confidence,
                "probabilities": normalized_probabilities,
                "evidence_probability": evidence_probability,
                "shadow_gate_passed": gate_passed,
                "gate_metrics": gate_metrics,
                "gate_result": (
                    "shadow_gate_pass_no_route_effect"
                    if gate_passed
                    else "shadow_gate_abstain_no_route_effect"
                ),
                "usage": usage,
                "cost": cost,
            }
        )
        return receipt
    except TimeoutError:
        receipt["gate_result"] = "fail_open_timeout"
        receipt["error_type"] = "TimeoutError"
        return receipt
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError):
            receipt["gate_result"] = "fail_open_timeout"
            receipt["error_type"] = "TimeoutError"
        else:
            receipt["gate_result"] = "fail_open_transport_error"
            receipt["error_type"] = type(exc).__name__
        return receipt
    except Exception as exc:
        receipt["gate_result"] = "fail_open_response_error"
        receipt["error_type"] = type(exc).__name__
        return receipt
    finally:
        receipt["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
        try:
            _append_receipt(receipt)
        except Exception:
            # Telemetry must never make task creation fail.
            pass
