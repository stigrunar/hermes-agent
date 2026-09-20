from __future__ import annotations

import json
import urllib.error

import pytest

from agent import jev_evaluation as jev


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


@pytest.mark.parametrize(
    ("title", "outcome", "choice"),
    [
        (
            "Implementer avgrenset CSV-eksport med de oppgitte testene",
            "Legg til CSV-eksport uten å endre eksisterende JSON-format",
            "settled_implementation",
        ),
        (
            "Inspect the parser regression and return evidence only",
            "Read the current parser and identify the failing boundary",
            "bounded_read",
        ),
        (
            "Bevar én aktiv writer under lager-migreringen",
            "Endre migreringen uten å bryte single-writer-invarianten",
            "hard_invariant",
        ),
        (
            "Fix it",
            "Make the service better",
            "insufficient_evidence",
        ),
    ],
)
def test_shadow_accepts_representative_norwegian_and_english_tasks(
    monkeypatch, tmp_path, title, outcome, choice
):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response(
            {
                "model": "typesafe/jev-1.13",
                "answers": {
                    jev.QUESTION_ID: {
                        "type": "choice",
                        "choice": choice,
                        "confidence": 0.91,
                        "probabilities": {
                            label: 0.91 if label == choice else 0.03
                            for label in jev.ALLOWED_CHOICES
                        },
                    },
                    jev.EVIDENCE_QUESTION_ID: {
                        "type": "noul",
                        "probability": 0.96,
                    },
                },
                "usage": {"input_tokens": 123, "cost": 0.000005},
            }
        )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setattr(jev.urllib.request, "urlopen", fake_urlopen)

    result = jev.evaluate_dollycode_task_shadow(
        title=title,
        execution_contract={
            "outcome": outcome,
            "quality_mode": "FEATURE",
            "frozen_acceptance": ["Focused acceptance passes"],
            "mutation_scope": ["src/feature.py"],
            "will_not_do": ["No deploy"],
            "verification": ["pytest tests/test_feature.py"],
            "stop_when": ["Acceptance passes"],
        },
        task_id="t_shadow",
        enabled=True,
        timeout_seconds=2.5,
    )

    assert result is not None
    assert result["proposal"] == choice
    assert result["probability"] == pytest.approx(0.91)
    assert result["confidence"] == pytest.approx(0.91)
    assert result["pinned_model"] == "typesafe/jev-1.13"
    assert result["gate_result"] == "shadow_gate_pass_no_route_effect"
    assert result["shadow_gate_passed"] is True
    assert result["evidence_probability"] == pytest.approx(0.96)
    assert result["gate_metrics"]["margin"] >= 0.30
    assert result["usage"] == {"input_tokens": 123, "cost": 0.000005}
    assert result["cost"] == pytest.approx(0.000005)
    assert captured["payload"]["model"] == "typesafe/jev-1.13"
    assert captured["timeout"] == pytest.approx(2.5)

    receipt_path = (
        tmp_path
        / ".hermes"
        / "logs"
        / "jev-shadow"
        / "dollycode-technical-task.jsonl"
    )
    receipt = json.loads(receipt_path.read_text().splitlines()[-1])
    assert receipt["snapshot_id"].startswith("dollycode-technical-task-v1:")
    assert len(receipt["snapshot_sha256"]) == 64
    assert receipt["latency_ms"] is not None
    assert "state" not in receipt


def test_shadow_filters_secret_like_task_text_before_transport(monkeypatch, tmp_path):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["wire"] = request.data.decode("utf-8")
        return _Response(
            {
                "answers": {
                    jev.QUESTION_ID: {
                        "type": "choice",
                        "choice": "insufficient_evidence",
                        "confidence": 1.0,
                        "probabilities": {"insufficient_evidence": 1.0},
                    }
                }
            }
        )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "transport-key-not-sent-in-state")
    monkeypatch.setattr(jev.urllib.request, "urlopen", fake_urlopen)

    jev.evaluate_dollycode_task_shadow(
        title="Debug OPENROUTER_API_KEY=«redacted:sk-…»",
        execution_contract={
            "outcome": "Inspect .env and browser cookie dump",
            "quality_mode": "FEATURE",
            "frozen_acceptance": ["token=opaque-secret-value"],
            "mutation_scope": ["src/safe.py"],
            "will_not_do": ["No credential change"],
            "verification": ["pytest"],
            "stop_when": ["done"],
        },
        enabled=True,
    )

    wire = captured["wire"]
    assert "«redacted:sk-…»" not in wire
    assert "opaque-secret-value" not in wire
    assert "transport-key-not-sent-in-state" not in wire
    assert "[filtered sensitive field]" in wire


def test_shadow_timeout_fails_open_and_logs(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def timeout(*_args, **_kwargs):
        raise TimeoutError("simulated")

    monkeypatch.setattr(jev.urllib.request, "urlopen", timeout)
    result = jev.evaluate_dollycode_task_shadow(
        title="Implement bounded fix",
        execution_contract={"outcome": "fix"},
        enabled=True,
        timeout_seconds=0.1,
    )

    assert result is not None
    assert result["proposal"] is None
    assert result["gate_result"] == "fail_open_timeout"
    assert result["error_type"] == "TimeoutError"


def test_shadow_transport_failure_fails_open(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def fail(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(jev.urllib.request, "urlopen", fail)
    result = jev.evaluate_dollycode_task_shadow(
        title="Implement bounded fix",
        execution_contract={"outcome": "fix"},
        enabled=True,
    )

    assert result is not None
    assert result["gate_result"] == "fail_open_transport_error"
    assert result["error_type"] == "URLError"


def test_shadow_low_confidence_abstains_without_route_effect(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout):
        return _Response(
            {
                "model": "typesafe/jev-1.13",
                "answers": {
                    jev.QUESTION_ID: {
                        "type": "choice",
                        "choice": "settled_implementation",
                        "confidence": 0.72,
                        "probabilities": {
                            "settled_implementation": 0.88,
                            "bounded_read": 0.06,
                            "hard_invariant": 0.04,
                            "insufficient_evidence": 0.02,
                        },
                    },
                    jev.EVIDENCE_QUESTION_ID: {
                        "type": "noul",
                        "probability": 0.97,
                    },
                },
            }
        )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(jev.urllib.request, "urlopen", fake_urlopen)

    result = jev.evaluate_dollycode_task_shadow(
        title="Implement bounded fix",
        execution_contract={
            "outcome": "Ship the bounded fix",
            "quality_mode": "FEATURE",
            "frozen_acceptance": ["Focused acceptance passes"],
            "mutation_scope": ["src/fix.py"],
            "will_not_do": ["No deploy"],
            "verification": ["pytest"],
            "stop_when": ["Acceptance passes"],
        },
        enabled=True,
    )

    assert result is not None
    assert result["proposal"] == "settled_implementation"
    assert result["shadow_gate_passed"] is False
    assert result["gate_result"] == "shadow_gate_abstain_no_route_effect"
    assert result["gate_metrics"]["confidence"] == pytest.approx(0.72)
