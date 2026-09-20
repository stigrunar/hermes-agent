"""Central, fail-open Jev decision evaluation for Hermes shadow pilots.

This module deliberately has no routing authority.  Its first consumer observes
new DollyCode technical task contracts, writes a metadata-only JSONL receipt,
and returns the existing Hermes result unchanged on success, timeout, malformed
response, or transport failure.
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
ALLOWED_CHOICES = frozenset({
    "settled_implementation",
    "bounded_read",
    "hard_invariant",
    "insufficient_evidence",
})

# This is intentionally stricter than ordinary log redaction.  Jev never needs
# credential-bearing assignments, browser/cookie dumps, or raw environment
# material to classify a technical task.
_SENSITIVE_LINE = re.compile(
    r"(?i)(?:^|\b)(?:[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|COOKIE)|"
    r"authorization|set-cookie|\.env)(?:\b|\s*[:=])"
)
_LONG_OPAQUE = re.compile(r"\b[A-Za-z0-9_\-+/=]{48,}\b")
_MAX_ITEM_CHARS = 2_000
_MAX_LIST_ITEMS = 12


def _filtered_text(value: Any) -> str:
    """Return bounded task text with secret-like lines removed."""
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
    """Resolve the existing default/root OpenRouter credential without output."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    try:
        from hermes_cli.env_loader import load_hermes_dotenv

        load_hermes_dotenv(hermes_home=get_default_hermes_root())
    except Exception:
        return ""
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def _question() -> dict[str, Any]:
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


def _log_path() -> Path:
    return get_default_hermes_root() / "logs" / "jev-shadow" / "dollycode-technical-task.jsonl"


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
    """Evaluate and log one DollyCode task without affecting its creation.

    The caller must ignore the return value for routing.  Every operational
    failure is converted into a metadata-only fail-open receipt.
    """
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
            "questions": {QUESTION_ID: _question()},
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
        with urllib.request.urlopen(request, timeout=max(0.1, timeout_seconds)) as response:
            parsed = json.loads(response.read().decode("utf-8"))

        answer = (parsed.get("answers") or {}).get(QUESTION_ID) or {}
        proposal = answer.get("choice")
        if answer.get("type") != "choice" or proposal not in ALLOWED_CHOICES:
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
        usage = _numeric_usage(parsed.get("usage"))
        cost = usage.get("cost", usage.get("total_cost"))

        receipt.update(
            {
                "resolved_model": str(parsed.get("model") or JEV_MODEL),
                "proposal": proposal,
                "probability": normalized_probabilities.get(proposal),
                "confidence": float(confidence) if confidence is not None else None,
                "probabilities": normalized_probabilities,
                "gate_result": "shadow_only_no_route_effect",
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
