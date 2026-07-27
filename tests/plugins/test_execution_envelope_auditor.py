"""Behavior tests for the deterministic execution-envelope shadow auditor."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from plugins.execution_envelope_auditor import audit_execution_envelope, register
from plugins.execution_envelope_auditor.cli import main


def _base_envelope(**overrides):
    envelope = {
        "outcome": "A bounded source candidate is usable",
        "quality_mode": "FEATURE",
        "risk_tier": "R1",
        "acceptance": ["focused behavior test passes"],
        "scope_in": ["plugins/example/**"],
        "scope_out": ["runtime activation"],
        "independent_packages": [],
        "tools_required": ["file", "terminal"],
        "skills_required": ["code-change-and-debug-workflows"],
        "proof_required": ["focused behavior tests", "owner diff review"],
        "proof_not_required": ["full suite", "detached review", "deploy"],
        "review_policy": "owner_closeout",
        "stop_when": [
            "acceptance passes and no BLOCKER remains",
            "an exact authority, resource, or safety blocker",
        ],
    }
    envelope.update(overrides)
    return envelope


def _payload(envelope=None, **metadata):
    return {
        "execution_envelope": envelope or _base_envelope(),
        "task_metadata": {
            "contract_id": "contract-1",
            "relevant_toolsets": ["file", "terminal"],
            "relevant_skills": ["code-change-and-debug-workflows"],
            "default_model": "standard-coder",
            "requested_model": "standard-coder",
            **metadata,
        },
    }


def _codes(report):
    return [finding["code"] for finding in report["findings"]]


def test_ordinary_feature_passes_with_structural_normalization_only():
    payload = _payload()
    payload["execution_envelope"]["outcome"] = "private customer payload: SECRET-4711"
    payload["execution_envelope"]["acceptance"] = ["SECRET-4711 becomes visible"]

    first = audit_execution_envelope(payload)
    second = audit_execution_envelope(payload)

    assert first == second
    assert first["valid"] is True
    assert first["findings"] == []
    assert first["normalized_envelope"] == {
        "acceptance_count": 1,
        "has_blocker_stop": True,
        "has_outcome": True,
        "has_success_stop": True,
        "independent_package_count": 0,
        "package_count": 1,
        "proof_not_required_count": 3,
        "proof_not_required_present": True,
        "proof_required_count": 2,
        "quality_mode": "FEATURE",
        "review_policy": "owner_closeout",
        "risk_tier": "R1",
        "scope_in_count": 1,
        "scope_out_count": 1,
        "skills_required": ["code-change-and-debug-workflows"],
        "tools_required": ["file", "terminal"],
    }
    assert "SECRET-4711" not in json.dumps(first)


def test_release_requires_full_exact_candidate_and_runtime_proof():
    envelope = _base_envelope(
        quality_mode="RELEASE",
        risk_tier="R2",
        review_policy="release_gate",
        proof_required=["focused tests"],
        proof_not_required=[],
    )

    report = audit_execution_envelope(_payload(envelope))

    assert {
        "release_full_verification_missing",
        "release_exact_review_missing",
        "release_runtime_proof_missing",
    }.issubset(_codes(report))

    envelope["proof_required"] = [
        "relevant full integration suite",
        "independent exact-candidate review",
        "rollback and live actual-target proof",
    ]
    assert audit_execution_envelope(_payload(envelope))["valid"] is True


def test_valid_spike_uses_minimal_proof_and_owner_closeout():
    envelope = _base_envelope(
        quality_mode="SPIKE",
        risk_tier="R0",
        acceptance=["bounded question answered"],
        proof_required=["minimal behavior smoke"],
    )

    assert audit_execution_envelope(_payload(envelope))["valid"] is True


def test_missing_stop_condition_is_reported():
    envelope = _base_envelope(stop_when=["continue until time runs out"])

    report = audit_execution_envelope(_payload(envelope))

    assert _codes(report) == ["missing_stop_condition"]


def test_generic_full_suite_and_review_silently_promote_feature():
    envelope = _base_envelope(
        review_policy="one_exact_candidate",
        proof_required=["full suite", "owner diff review"],
    )

    report = audit_execution_envelope(_payload(envelope))

    assert _codes(report) == ["speculative_broad_proof", "speculative_review_gate"]


def test_valid_r2_exact_candidate_review_passes():
    envelope = _base_envelope(
        risk_tier="R2",
        review_policy="one_exact_candidate",
        proof_required=["focused tests", "exact-candidate authority review"],
        proof_not_required=[],
    )

    assert audit_execution_envelope(_payload(envelope))["valid"] is True


def test_speculative_detached_review_and_deploy_gates_are_reported():
    report = audit_execution_envelope(
        _payload(
            planned_gates=[
                {"type": "detached_review"},
                {"type": "deploy"},
            ]
        )
    )

    assert _codes(report) == ["speculative_deploy_gate", "speculative_review_gate"]
    assert all(finding["severity"] == "warning" for finding in report["findings"])


def test_mode_risk_and_missing_proof_are_reported():
    envelope = _base_envelope(
        quality_mode="FEATURE",
        risk_tier="R3",
        review_policy="owner_closeout",
        proof_required=[],
    )

    report = audit_execution_envelope(_payload(envelope))

    assert "missing_proof" in _codes(report)
    assert "mode_risk_mismatch" in _codes(report)


def test_shared_schema_port_and_parent_file_collisions_fail_independence():
    envelope = _base_envelope(
        active_package={
            "files": ["plugins/example"],
            "schemas": ["receipt-v1"],
            "ports": ["8123"],
        },
        independent_packages=[
            {
                "files": ["plugins/example/cli.py"],
                "schemas": ["receipt-v1"],
                "ports": ["8123"],
                "independence_evidence": {
                    "useful_artifact": True,
                    "reduces_lead_time": True,
                    "disjoint": True,
                },
            }
        ],
    )

    report = audit_execution_envelope(_payload(envelope))

    collision = next(item for item in report["findings"] if item["code"] == "package_independence_collision")
    assert collision["severity"] == "error"
    assert collision["message"].endswith("files, schemas, ports.")
    assert report["normalized_envelope"]["package_count"] == 2


def test_repeated_bootstrap_overbroad_surface_and_model_escalation_are_reported():
    envelope = _base_envelope(
        tools_required=["all"],
        skills_required=["code-change-and-debug-workflows", "unrelated-review"],
    )

    report = audit_execution_envelope(
        _payload(
            envelope,
            bootstrap_count=2,
            requested_model="ultra-coder",
            default_model="standard-coder",
        )
    )

    assert _codes(report) == [
        "over_broad_skill_request",
        "over_broad_toolset_request",
        "repeated_bootstrap",
        "unnecessary_model_escalation",
    ]


def test_completion_contract_mismatch_and_hard_gate_bypass_are_errors():
    envelope = _base_envelope(
        risk_tier="R2",
        review_policy="one_exact_candidate",
        proof_not_required=["auth and external-write approval"],
    )

    report = audit_execution_envelope(
        _payload(envelope, completion_receipt={"contract_id": "contract-2"})
    )

    assert "completion_contract_mismatch" in _codes(report)
    assert "hard_gate_bypass" in _codes(report)


def test_plugin_registers_cli_without_tools_or_hooks():
    calls = []

    class Context:
        def register_cli_command(self, **kwargs):
            calls.append(kwargs)

    register(Context())

    assert len(calls) == 1
    assert calls[0]["name"] == "execution-envelope-audit"
    parser = argparse.ArgumentParser()
    calls[0]["setup_fn"](parser)
    args = parser.parse_args(["--mode", "strict-fixture", "fixture.json"])
    assert args.mode == "strict-fixture"
    assert args.input == "fixture.json"


def test_cli_shadow_reports_findings_but_exits_zero(tmp_path, capsys):
    fixture = tmp_path / "invalid.json"
    fixture.write_text(json.dumps(_payload(_base_envelope(proof_required=[]))))

    exit_code = main([str(fixture)])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["mode"] == "shadow"
    assert "missing_proof" in _codes(report)


def test_cli_strict_fixture_exits_one_on_findings(tmp_path, capsys):
    fixture = tmp_path / "invalid.json"
    fixture.write_text(json.dumps(_payload(_base_envelope(proof_required=[]))))

    exit_code = main(["--mode", "strict-fixture", str(fixture)])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert report["mode"] == "strict-fixture"


def test_direct_module_entrypoint_emits_deterministic_json(tmp_path):
    fixture = tmp_path / "valid.json"
    fixture.write_text(json.dumps(_payload()))

    command = [
        sys.executable,
        "-m",
        "plugins.execution_envelope_auditor",
        str(fixture),
    ]
    first = subprocess.run(command, check=True, capture_output=True, text=True)
    second = subprocess.run(command, check=True, capture_output=True, text=True)

    assert first.stdout == second.stdout
    assert json.loads(first.stdout)["valid"] is True
    assert first.stderr == ""


def test_bundled_plugin_discovery_registers_cli(tmp_path, monkeypatch):
    import yaml

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["execution-envelope-auditor"]}})
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_cli.plugins import _ensure_plugins_discovered

    manager = _ensure_plugins_discovered(force=True)

    assert "execution-envelope-auditor" in manager._plugins
    assert "execution-envelope-audit" in manager._cli_commands
