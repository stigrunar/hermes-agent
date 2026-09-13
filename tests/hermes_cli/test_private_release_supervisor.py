"""Behavior tests for the fixed-policy private release supervisor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.private_release_supervisor import (
    POLICY_SCHEMA,
    PUBLICATION_SCHEMA,
    SCHEMA_VERSION,
    STAGE_SCHEMA,
    SupervisorOperations,
    execute_private_release,
    parse_host_policy,
    parse_sealed_request,
    seal_private_release_request,
    sha256_bytes,
)

COMMIT = "a" * 40
TREE = "b" * 40


def _write(path: Path, data: bytes, mode: int = 0o600) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return sha256_bytes(data)


def _policy_value(root: Path, home: Path) -> dict:
    units = [
        ("hermes-gateway.service", "gateway", root),
        ("hermes-gateway-dollydesign.service", "gateway", root / "profiles/dollydesign"),
        ("hermes-gateway-dollyops.service", "gateway", root / "profiles/dollyops"),
        ("hermes-gateway-dollyprivate.service", "gateway", root / "profiles/dollyprivate"),
        ("hermes-dashboard.service", "dashboard", None),
        ("hermes-kanban-safe-dispatcher.service", "dispatcher", None),
    ]
    return {
        "schema": POLICY_SCHEMA,
        "version": SCHEMA_VERSION,
        "os_home": str(home),
        "runtime_root": str(root / "runtime"),
        "gateway_wrapper": str(root / "scripts/hermes_gateway_with_x11.sh"),
        "dispatcher_wrapper": str(root / "scripts/kanban_safe_dispatch_loop.py"),
        "default_state_db": {
            "path": str(root / "state.db"),
            "backup_integrity": "excluded",
            "restore": "forbidden",
        },
        "targets": [
            {
                "unit": unit,
                "kind": kind,
                "binding": str(home / ".config/systemd/user" / f"{unit}.d/99-z-downstream-main.conf"),
                "profile_home": None if profile is None else str(profile),
            }
            for unit, kind, profile in units
        ],
        "health": {
            "dashboard_status_url": "http://127.0.0.1:9120/api/status",
            "dashboard_root_url": "http://127.0.0.1:9120/",
        },
        "timeouts": {"drain_seconds": 30, "health_seconds": 30, "sustained_seconds": 1},
    }


def _fixture(tmp_path: Path, request_id: str = "request-1"):
    root, home = tmp_path / "hermes", tmp_path / "account"
    state = root / "private-update"
    receipts = state / "receipts"
    receipts.mkdir(parents=True)
    receipts.chmod(0o700)
    policy = parse_host_policy(
        _policy_value(root, home), default_root=root, expected_os_home=home
    )
    runtime = root / "runtime" / f"downstream-{COMMIT[:10]}"
    identity = json.dumps({"commit": COMMIT, "tree": TREE}, sort_keys=True).encode()
    identity_digest = _write(runtime / "private-release-identity.json", identity)
    artifact_digest = _write(runtime / "candidate.txt", b"candidate artifact\n")
    stage_manifest = {
        "schema": STAGE_SCHEMA,
        "version": SCHEMA_VERSION,
        "commit": COMMIT,
        "tree": TREE,
        "artifacts": {
            "private-release-identity.json": identity_digest,
            "candidate.txt": artifact_digest,
        },
    }
    stage_bytes = json.dumps(stage_manifest, sort_keys=True).encode()
    stage_digest = _write(runtime / "private-release-manifest.json", stage_bytes)
    publication = {
        "schema": PUBLICATION_SCHEMA,
        "version": SCHEMA_VERSION,
        "commit": COMMIT,
        "tree": TREE,
        "remote": "private-origin",
        "ref": "refs/releases/candidate",
        "private": True,
        "verified": True,
    }
    publication_bytes = json.dumps(publication, sort_keys=True).encode()
    publication_digest = _write(
        receipts / f"{COMMIT}.private-publication.json", publication_bytes
    )
    request = seal_private_release_request(
        request_id=request_id,
        project_id="p_155df2bb",
        outcome_id="o_cde72dc3",
        execution_id="execution-1",
        correlation_id="terminal-1",
        commit=COMMIT,
        tree=TREE,
        private_publication_sha256=publication_digest,
        artifact_manifest_sha256=stage_digest,
    )
    originals = {}
    for target in policy.targets:
        old_runtime = root / "runtime/downstream-old"
        body = (
            "[Service]\n"
            f"ExecStart={old_runtime}/venv/bin/python old\n"
            f"Environment=HERMES_REPO={old_runtime}\n"
            f"Environment=PYTHONPATH={old_runtime}\n"
            f"Environment=PATH={old_runtime}/venv/bin:/usr/bin\n"
        ).encode()
        _write(target.binding, body)
        originals[target.unit] = body
    return state, policy, request, originals


def _operations(policy, originals, calls, *, fail_unit=None):
    def prepare(_request, _policy):
        snapshot = {
            target.unit: (target.binding.read_bytes(), target.binding.stat().st_mode & 0o777)
            for target in policy.targets
        }
        return snapshot, {"baseline": sorted(snapshot)}

    def quiesce(_request, _policy, phase):
        calls.append(("quiesce", phase))
        return True

    def replace(target, data, mode):
        calls.append(("binding", target.unit))
        target.binding.write_bytes(data)
        target.binding.chmod(mode)
        if target.unit == fail_unit and data != originals[target.unit]:
            raise RuntimeError("injected post-byte failure")

    def restart(_targets, phase):
        calls.append(("restart", phase))

    def health(_request, _policy, phase):
        calls.append(("health", phase))
        return True

    def canary(_request, _policy):
        calls.append(("canary", "candidate"))
        return True

    def release(_request, _policy):
        calls.append(("release", "drains"))
        return True

    return SupervisorOperations(prepare, quiesce, replace, restart, health, canary, release)


def test_request_has_only_identity_and_digests_and_derives_every_path(tmp_path):
    state, _policy, payload, _originals = _fixture(tmp_path)
    assert "targets" not in payload
    assert not any("path" in key or "unit" in key for key in payload)
    request = parse_sealed_request(payload, state_dir=state)
    assert request.runtime == state.parent / "runtime" / f"downstream-{COMMIT[:10]}"
    assert request.lock_path == state / ".private-immutable-release.lock"
    assert request.receipt_dir == state / "receipts"


@pytest.mark.parametrize("field", ["targets", "helper_path", "receipt_dir", "runtime"])
def test_request_rejects_every_unknown_authority_field(tmp_path, field):
    state, _policy, payload, _originals = _fixture(tmp_path)
    payload[field] = [] if field == "targets" else "/tmp/attacker"
    with pytest.raises(Exception, match="unexpected"):
        parse_sealed_request(payload, state_dir=state)


def test_policy_rejects_a_seventh_or_reordered_target(tmp_path):
    root, home = tmp_path / "hermes", tmp_path / "account"
    value = _policy_value(root, home)
    value["targets"].append(dict(value["targets"][0]))
    with pytest.raises(Exception, match="exactly 6"):
        parse_host_policy(value, default_root=root, expected_os_home=home)
    value = _policy_value(root, home)
    value["targets"].reverse()
    with pytest.raises(Exception, match="fixed six-target"):
        parse_host_policy(value, default_root=root, expected_os_home=home)


def test_success_journals_then_updates_all_six_bindings(tmp_path):
    state, policy, payload, originals = _fixture(tmp_path, "success")
    calls = []
    result = execute_private_release(
        payload, policy=policy, operations=_operations(policy, originals, calls), state_dir=state
    )
    assert result["status"] == "succeeded"
    assert len(result["mutated_units"]) == 6
    assert (state / "receipts/success.prestate.json").is_file()
    journal = json.loads((state / "receipts/success.journal.json").read_text())
    assert journal["phase"] == "committed_before_drain_release"
    assert result["default_profile_state_db"] == {
        "backup_integrity": "excluded", "restore": "forbidden", "verified": False,
    }


def test_post_byte_failure_attempts_each_changed_binding_rollback(tmp_path):
    state, policy, payload, originals = _fixture(tmp_path, "rollback")
    calls = []
    failure_unit = policy.targets[2].unit
    result = execute_private_release(
        payload,
        policy=policy,
        operations=_operations(policy, originals, calls, fail_unit=failure_unit),
        state_dir=state,
    )
    assert result["status"] == "rolled_back"
    assert result["rollback"]["attempted"] is True
    assert set(result["rollback"]["restored_units"]) == {
        target.unit for target in policy.targets[:3]
    }
    assert all(target.binding.read_bytes() == originals[target.unit] for target in policy.targets)
    assert ("quiesce", "rollback") in calls


def test_terminal_receipt_rejects_replay_without_a_second_mutation(tmp_path):
    state, policy, payload, originals = _fixture(tmp_path, "replay")
    calls = []
    operations = _operations(policy, originals, calls)
    first = execute_private_release(payload, policy=policy, operations=operations, state_dir=state)
    second = execute_private_release(payload, policy=policy, operations=operations, state_dir=state)
    assert first["status"] == "succeeded"
    assert second["admission"] == "replay_rejected"
    assert [kind for kind, _value in calls].count("binding") == 6


def test_evidence_symlink_fails_before_operations(tmp_path):
    state, policy, payload, originals = _fixture(tmp_path, "evidence")
    publication = state / "receipts" / f"{COMMIT}.private-publication.json"
    real = publication.with_name("publication.real.json")
    publication.rename(real)
    publication.symlink_to(real)
    calls = []
    result = execute_private_release(
        payload, policy=policy, operations=_operations(policy, originals, calls), state_dir=state
    )
    assert result["status"] == "rejected"
    assert calls == []
    assert result["mutated_units"] == []
