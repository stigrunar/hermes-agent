"""Behavior tests for the fixed-policy private release supervisor."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.private_release_supervisor import (
    CandidateIdentity,
    POLICY_SCHEMA,
    PUBLICATION_SCHEMA,
    SCHEMA_VERSION,
    STAGE_SCHEMA,
    PrivateReleaseError,
    ProductionOperations,
    PrivateReleaseRequest,
    SupervisorOperations,
    execute_private_release,
    parse_host_policy,
    parse_sealed_request,
    seal_private_release_request,
    sha256_bytes,
    _validate_private_git_state,
)

COMMIT = "a" * 40
TREE = "b" * 40


def _write(path: Path, data: bytes, mode: int = 0o600) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return sha256_bytes(data)


def _policy_value(root: Path, home: Path) -> dict:
    gateway_wrapper = root / "scripts/hermes_gateway_with_x11.sh"
    dispatcher_wrapper = root / "scripts/kanban_safe_dispatch_loop.py"
    gateway_wrapper_digest = _write(gateway_wrapper, b"#!/bin/sh\nexec \"$@\"\n", 0o700)
    dispatcher_wrapper_digest = _write(dispatcher_wrapper, b"#!/usr/bin/env python3\n", 0o700)
    python_path = Path(sys.executable).resolve()
    python_digest = sha256_bytes(python_path.read_bytes())
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
        "gateway_wrapper": str(gateway_wrapper),
        "dispatcher_wrapper": str(dispatcher_wrapper),
        "activation_files": {
            "gateway_wrapper_sha256": gateway_wrapper_digest,
            "dispatcher_wrapper_sha256": dispatcher_wrapper_digest,
            "python_path": str(python_path),
            "python_sha256": python_digest,
        },
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
    python_digest = _write(runtime / "venv/bin/python3", b"candidate interpreter\n", 0o700)
    (runtime / "venv/bin/python").symlink_to("python3")
    stage_manifest = {
        "schema": STAGE_SCHEMA,
        "version": SCHEMA_VERSION,
        "commit": COMMIT,
        "tree": TREE,
        "artifacts": {
            "private-release-identity.json": identity_digest,
            "candidate.txt": artifact_digest,
            "venv/bin/python3": python_digest,
            "venv/bin/python": {
                "type": "symlink", "target": "python3", "target_sha256": python_digest,
            },
        },
    }
    stage_bytes = json.dumps(stage_manifest, sort_keys=True).encode()
    stage_digest = _write(runtime / "private-release-manifest.json", stage_bytes)
    publication = {
        "schema": PUBLICATION_SCHEMA,
        "version": SCHEMA_VERSION,
        "repository": "stigrunar/hermes-agent-review",
        "ref": "release/stig-tested-r12",
        "prior_ref": "release/stig-tested-r12",
        "prior_commit": "c" * 40,
        "commit": COMMIT,
        "tree": TREE,
        "merge_base": "c" * 40,
        "fast_forward": True,
        "forced": False,
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

    return SupervisorOperations(
        prepare, quiesce, replace, restart, health, canary, release, lambda _request: True
    )


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
    assert journal["phase"] == "committed_after_drain_release"
    rollback = state / "receipts/success.rollback.json"
    assert rollback.stat().st_mode & 0o777 == 0o600
    assert journal["rollback_bundle_sha256"] == sha256_bytes(rollback.read_bytes())
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "NousResearch/hermes-agent"),
        ("repository", "origin"),
        ("ref", "main"),
        ("forced", True),
        ("fast_forward", False),
        ("merge_base", "d" * 40),
    ],
)
def test_publication_evidence_rejects_wrong_destination_or_history(tmp_path, field, value):
    state, policy, payload, originals = _fixture(tmp_path, f"publication-{field}")
    publication = state / "receipts" / f"{COMMIT}.private-publication.json"
    body = json.loads(publication.read_text())
    body[field] = value
    raw = json.dumps(body, sort_keys=True).encode()
    payload["private_publication_sha256"] = _write(publication, raw)
    calls = []
    result = execute_private_release(
        payload, policy=policy, operations=_operations(policy, originals, calls), state_dir=state
    )
    assert result["status"] == "rejected"
    assert calls == []


def test_assertion_only_publication_receipt_fails_closed(tmp_path):
    state, policy, payload, originals = _fixture(tmp_path, "assertion-only")
    publication = state / "receipts" / f"{COMMIT}.private-publication.json"
    raw = json.dumps({
        "schema": PUBLICATION_SCHEMA, "version": SCHEMA_VERSION,
        "commit": COMMIT, "tree": TREE, "remote": "private-origin",
        "ref": "release/stig-tested-r12", "private": True, "verified": True,
    }, sort_keys=True).encode()
    payload["private_publication_sha256"] = _write(publication, raw)
    calls = []
    result = execute_private_release(
        payload, policy=policy, operations=_operations(policy, originals, calls), state_dir=state
    )
    assert result["status"] == "rejected"
    assert calls == []


def test_publication_is_grounded_in_fixed_private_remote_ref_and_reflog(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    state = root / "private-update"
    receipts = state / "receipts"
    runtime = root / "runtime" / "candidate"
    receipts.mkdir(parents=True)
    receipts.chmod(0o700)
    runtime.mkdir(parents=True)
    repository = root / "hermes-agent"
    repository.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        ).stdout.strip()

    git("init")
    git("config", "user.name", "R13 Test")
    git("config", "user.email", "r13@example.invalid")
    git("config", "core.logAllRefUpdates", "true")
    git("remote", "add", "private-review", "git@github.com:stigrunar/hermes-agent-review.git")
    (repository / "artifact").write_text("prior\n")
    git("add", "artifact")
    git("commit", "-m", "prior")
    prior = git("rev-parse", "HEAD")
    (repository / "artifact").write_text("candidate\n")
    git("commit", "-am", "candidate")
    commit = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    tracking = "refs/remotes/private-review/release/stig-tested-r13"
    git("update-ref", "-m", "prior", tracking, prior)
    git("update-ref", "-m", "fast-forward", tracking, commit, prior)

    publication = {
        "schema": PUBLICATION_SCHEMA, "version": SCHEMA_VERSION,
        "repository": "stigrunar/hermes-agent-review",
        "ref": "release/stig-tested-r13", "prior_ref": "release/stig-tested-r13",
        "prior_commit": prior, "commit": commit, "tree": tree,
        "merge_base": prior, "fast_forward": True, "forced": False,
    }
    publication_bytes = json.dumps(publication, sort_keys=True).encode()
    publication_path = receipts / f"{commit}.private-publication.json"
    publication_digest = _write(publication_path, publication_bytes)
    request = PrivateReleaseRequest(
        "git-proof", "project", "outcome", "execution", "correlation",
        CandidateIdentity(commit, tree), publication_digest, "0" * 64,
        state, runtime, publication_path, runtime / "private-release-manifest.json",
    )
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "attacker-selected-git-dir"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.private-review.url")
    monkeypatch.setenv(
        "GIT_CONFIG_VALUE_0", "git@github.com:NousResearch/hermes-agent.git"
    )
    _validate_private_git_state(request)

    git("remote", "set-url", "private-review", "git@github.com:NousResearch/hermes-agent.git")
    with pytest.raises(Exception, match="remote identity"):
        _validate_private_git_state(request)


def test_unmanifested_runtime_file_fails_before_operations(tmp_path):
    state, policy, payload, originals = _fixture(tmp_path, "extra-runtime")
    runtime = state.parent / "runtime" / f"downstream-{COMMIT[:10]}"
    _write(runtime / "unmanifested-executable", b"#!/bin/sh\nexit 0\n", 0o700)
    calls = []
    result = execute_private_release(
        payload, policy=policy, operations=_operations(policy, originals, calls), state_dir=state
    )
    assert result["status"] == "rejected"
    assert calls == []


def test_standard_virtualenv_interpreter_symlink_is_bound_and_supported(tmp_path):
    state, policy, payload, originals = _fixture(tmp_path, "venv-symlink")
    runtime = state.parent / "runtime" / f"downstream-{COMMIT[:10]}"
    interpreter = runtime / "venv/bin/python"
    interpreter.unlink()
    interpreter.symlink_to(policy.python_path)
    manifest_path = runtime / "private-release-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"]["venv/bin/python"] = {
        "type": "symlink",
        "target": str(policy.python_path),
        "target_sha256": policy.python_sha256,
    }
    payload["artifact_manifest_sha256"] = _write(
        manifest_path, json.dumps(manifest, sort_keys=True).encode()
    )
    calls = []
    result = execute_private_release(
        payload, policy=policy, operations=_operations(policy, originals, calls), state_dir=state
    )
    assert result["status"] == "succeeded"


@pytest.mark.parametrize("symlink", [False, True])
def test_large_staged_payload_digest_is_validated(tmp_path, symlink):
    from hermes_cli.private_release_supervisor import _MAX_FILE_BYTES, RequestValidationError

    state, _, payload, _ = _fixture(tmp_path, "large-artifact")
    runtime = state.parent / "runtime" / f"downstream-{COMMIT[:10]}"
    artifact = runtime / "candidate.txt"
    size = _MAX_FILE_BYTES + 1
    with artifact.open("wb") as handle:
        handle.truncate(size)
    digest = hashlib.sha256()
    block = bytes(1024 * 1024)
    for _ in range(size // len(block)):
        digest.update(block)
    digest.update(block[:size % len(block)])
    manifest_path = runtime / "private-release-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"]["candidate.txt"] = digest.hexdigest()
    if symlink:
        (runtime / "dependency-link").symlink_to("candidate.txt")
        manifest["artifacts"]["dependency-link"] = {
            "type": "symlink", "target": "candidate.txt",
            "target_sha256": digest.hexdigest(),
        }
    payload["artifact_manifest_sha256"] = _write(
        manifest_path, json.dumps(manifest, sort_keys=True).encode()
    )
    assert parse_sealed_request(payload, state_dir=state).candidate.commit == COMMIT

    if symlink:
        manifest["artifacts"]["dependency-link"]["target_sha256"] = "0" * 64
    else:
        manifest["artifacts"]["candidate.txt"] = "0" * 64
    payload["artifact_manifest_sha256"] = _write(
        manifest_path, json.dumps(manifest, sort_keys=True).encode()
    )
    with pytest.raises(RequestValidationError, match="digest mismatch"):
        parse_sealed_request(payload, state_dir=state)


@pytest.mark.parametrize("reader", ["_read_owned_bytes", "_read_boundary_bytes", "_read_json"])
def test_control_file_reads_remain_bounded(tmp_path, reader):
    from hermes_cli import private_release_supervisor as supervisor

    control = tmp_path / "control.json"
    with control.open("wb") as handle:
        handle.truncate(supervisor._MAX_FILE_BYTES + 1)
    control.chmod(0o600)
    with pytest.raises(supervisor.RequestValidationError, match="too large"):
        getattr(supervisor, reader)(control, "control")


@pytest.mark.parametrize("boundary", [False, True])
@pytest.mark.parametrize("change", ["mutate", "replace", "mode", "mtime"])
def test_staged_payload_hash_rejects_concurrent_changes(tmp_path, monkeypatch, boundary, change):
    from hermes_cli import private_release_supervisor as supervisor

    artifact = tmp_path / "artifact"
    _write(artifact, b"original payload")
    original_read = supervisor.os.read
    changed = False

    def read_and_change(fd, size):
        nonlocal changed
        block = original_read(fd, size)
        if block and not changed:
            changed = True
            if change == "replace":
                replacement = tmp_path / "replacement"
                _write(replacement, b"original payload")
                replacement.replace(artifact)
            elif change == "mode":
                artifact.chmod(0o400)
            elif change == "mtime":
                before = artifact.stat()
                artifact.write_bytes(b"modified payload")
                supervisor.os.utime(
                    artifact, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000)
                )
            else:
                artifact.write_bytes(b"modified payload with a different size")
        return block

    monkeypatch.setattr(supervisor.os, "read", read_and_change)
    with pytest.raises(supervisor.RequestValidationError, match="changed while being hashed"):
        supervisor._hash_staged_payload(artifact, "artifact", boundary=boundary)


@pytest.mark.parametrize("change", ["replace", "text", "remove", "regular"])
def test_manifested_symlink_change_during_target_hash_is_rejected(tmp_path, monkeypatch, change):
    from hermes_cli import private_release_supervisor as supervisor

    state, _, payload, _ = _fixture(tmp_path, "symlink-race")
    runtime = state.parent / "runtime" / f"downstream-{COMMIT[:10]}"
    link = runtime / "venv/bin/python"
    target = link.resolve(strict=True)
    target_info = target.stat()
    original_hash = supervisor._hash_staged_payload
    original_read = supervisor.os.read
    hashing_link = False
    changed = False

    def hash_payload(path, label, **kwargs):
        nonlocal hashing_link
        hashing_link = label == "resolved staged symlink target venv/bin/python"
        try:
            return original_hash(path, label, **kwargs)
        finally:
            hashing_link = False

    def read_and_change(fd, size):
        nonlocal changed
        block = original_read(fd, size)
        if block and hashing_link and not changed:
            info = supervisor.os.fstat(fd)
            assert (info.st_dev, info.st_ino) == (target_info.st_dev, target_info.st_ino)
            changed = True
            replacement = tmp_path / "replacement"
            if change == "remove":
                link.unlink()
            elif change == "regular":
                _write(replacement, target.read_bytes())
                replacement.replace(link)
            else:
                # Both texts resolve to the unchanged, approved target.
                replacement.symlink_to("./python3" if change == "text" else "python3")
                replacement.replace(link)
        return block

    monkeypatch.setattr(supervisor, "_hash_staged_payload", hash_payload)
    monkeypatch.setattr(supervisor.os, "read", read_and_change)
    with pytest.raises(supervisor.RequestValidationError, match="staged symlink changed"):
        parse_sealed_request(payload, state_dir=state)
    assert changed


def test_manifested_external_dependency_symlink_fails_before_operations(tmp_path):
    state, policy, payload, originals = _fixture(tmp_path, "external-dependency")
    runtime = state.parent / "runtime" / f"downstream-{COMMIT[:10]}"
    outside = tmp_path / "outside-dependency"
    outside_digest = _write(outside, b"external executable\n", 0o700)
    dependency = runtime / "candidate.txt"
    dependency.unlink()
    dependency.symlink_to(outside)
    manifest_path = runtime / "private-release-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"]["candidate.txt"] = {
        "type": "symlink",
        "target": str(outside),
        "target_sha256": outside_digest,
    }
    payload["artifact_manifest_sha256"] = _write(
        manifest_path, json.dumps(manifest, sort_keys=True).encode()
    )
    calls = []
    result = execute_private_release(
        payload, policy=policy, operations=_operations(policy, originals, calls), state_dir=state
    )
    assert result["status"] == "rejected"
    assert "unapproved runtime boundary" in result["error"]
    assert calls == []


def test_runtime_dependency_is_revalidated_immediately_before_binding_mutation(tmp_path):
    state, policy, payload, originals = _fixture(tmp_path, "runtime-drift")
    calls = []
    operations = _operations(policy, originals, calls)
    original_quiesce = operations.quiesce

    def drift_after_parse(request, active_policy, phase):
        result = original_quiesce(request, active_policy, phase)
        (request.runtime / "venv/bin/python3").write_bytes(b"changed interpreter\n")
        return result

    operations.quiesce = drift_after_parse
    result = execute_private_release(payload, policy=policy, operations=operations, state_dir=state)
    assert result["status"] == "rejected"
    assert not [call for call in calls if call[0] == "binding"]
    assert all(target.binding.read_bytes() == originals[target.unit] for target in policy.targets)


def test_host_wrapper_is_revalidated_immediately_before_binding_mutation(tmp_path):
    state, policy, payload, originals = _fixture(tmp_path, "wrapper-drift")
    calls = []
    operations = _operations(policy, originals, calls)
    original_quiesce = operations.quiesce

    def drift_after_parse(request, active_policy, phase):
        result = original_quiesce(request, active_policy, phase)
        active_policy.gateway_wrapper.write_bytes(b"changed host wrapper\n")
        return result

    operations.quiesce = drift_after_parse
    result = execute_private_release(payload, policy=policy, operations=operations, state_dir=state)
    assert result["status"] == "rejected"
    assert not [call for call in calls if call[0] == "binding"]
    assert all(target.binding.read_bytes() == originals[target.unit] for target in policy.targets)


def test_interrupted_binding_recovers_from_durable_rollback_bundle(tmp_path):
    state, policy, payload, originals = _fixture(tmp_path, "crash-recovery")
    first_calls = []
    crashing = _operations(policy, originals, first_calls)
    replace = crashing.replace_binding
    crashed = False

    def crash_after_first_byte(target, data, mode):
        nonlocal crashed
        replace(target, data, mode)
        if not crashed:
            crashed = True
            raise KeyboardInterrupt("simulated process death")

    crashing.replace_binding = crash_after_first_byte
    with pytest.raises(KeyboardInterrupt):
        execute_private_release(payload, policy=policy, operations=crashing, state_dir=state)
    assert policy.targets[0].binding.read_bytes() != originals[policy.targets[0].unit]
    # Candidate/publication evidence is not rollback authority and may be the
    # very state lost in a process/filesystem failure.
    (state.parent / "runtime" / f"downstream-{COMMIT[:10]}" / "private-release-manifest.json").unlink()
    (state / "receipts" / f"{COMMIT}.private-publication.json").unlink()

    recovery_calls = []
    recovery_operations = _operations(policy, originals, recovery_calls)
    recovery_operations.verify_publication = lambda _request: pytest.fail(
        "crash rollback consulted mutable publication evidence"
    )
    recovered = execute_private_release(
        payload,
        policy=policy,
        operations=recovery_operations,
        state_dir=state,
    )
    assert recovered["status"] == "rolled_back"
    assert recovered["recovery"] == "interrupted_activation"
    assert recovered["rollback"]["restored_units"] == [policy.targets[0].unit]
    assert all(target.binding.read_bytes() == originals[target.unit] for target in policy.targets)


def test_affected_cgroup_cannot_directly_prepare_activation(tmp_path, monkeypatch):
    state, policy, payload, _originals = _fixture(tmp_path, "affected-cgroup")
    request = parse_sealed_request(payload, state_dir=state)
    original_read_text = Path.read_text

    def fake_read_text(path, *args, **kwargs):
        if str(path) == "/proc/self/cgroup":
            return f"0::/user.slice/{policy.targets[0].unit}\n"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    with pytest.raises(PrivateReleaseError, match="affected target cgroup"):
        ProductionOperations().prepare(request, policy)


def _prime_idle_gateways(policy, principal, *, updated_at="2100-01-01T00:00:00+00:00"):
    for target in policy.gateway_targets:
        _write(
            target.profile_home / ".drain_request.json",
            json.dumps({"principal": principal}).encode(),
        )
        _write(
            target.profile_home / "gateway_state.json",
            json.dumps(
                {
                    "pid": 123,
                    "start_time": 456,
                    "gateway_state": "draining",
                    "active_agents": 0,
                    "active_cron_jobs": 0,
                    "active_api_runs": 0,
                    "code_sha": COMMIT,
                    "updated_at": updated_at,
                }
            ).encode(),
        )


def test_quiesce_accepts_two_independent_idle_samples_without_state_rewrite(
    tmp_path, monkeypatch
):
    from hermes_cli import private_release_supervisor as supervisor

    state, policy, payload, _originals = _fixture(tmp_path, "idle-no-rewrite")
    request = parse_sealed_request(payload, state_dir=state)
    operations = ProductionOperations()
    operations.marker_principal = f"private-release:{request.request_id}"
    _prime_idle_gateways(policy, operations.marker_principal)
    monkeypatch.setattr(operations, "_drain", lambda *_args, **_kwargs: None)
    show_calls = []
    start_calls = []

    def show(unit):
        show_calls.append(unit)
        return {"MainPID": "123", "ControlGroup": "/test"}

    def proc_start(pid):
        start_calls.append(pid)
        return 456

    monkeypatch.setattr(supervisor, "_show", show)
    monkeypatch.setattr(supervisor, "_proc_start", proc_start)
    reads = []
    read_json = supervisor._read_json

    def tracked_read(path, *args, **kwargs):
        reads.append(path)
        return read_json(path, *args, **kwargs)

    monkeypatch.setattr(supervisor, "_read_json", tracked_read)
    monotonic = iter((0.0, 0.0, 1.0, 31.0))
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)
    prestate_checks = []
    monkeypatch.setattr(
        operations, "_verify_prestate", lambda checked: prestate_checks.append(checked)
    )

    result = operations.quiesce(request, policy, "candidate")

    assert result["samples"] == 2
    assert prestate_checks == [policy]
    for target in policy.gateway_targets:
        assert show_calls.count(target.unit) == 2
        assert reads.count(target.profile_home / "gateway_state.json") == 2
        assert reads.count(target.profile_home / ".drain_request.json") == 3
    assert len(start_calls) >= 2 * len(policy.gateway_targets)


@pytest.mark.parametrize(
    "fault,match",
    [
        ("stale", "two fresh PID/start-bound samples"),
        ("pid_drift", "two fresh PID/start-bound samples"),
        ("start_drift", "two fresh PID/start-bound samples"),
        ("active", "two fresh PID/start-bound samples"),
        ("malformed", "not valid UTF-8 JSON"),
        ("unsafe_mode", "group/other writable"),
        ("foreign_marker", "foreign drain marker"),
        ("child_scope", "child scope is not empty"),
    ],
)
def test_quiesce_rejects_unsafe_or_unverified_samples(tmp_path, monkeypatch, fault, match):
    from hermes_cli import private_release_supervisor as supervisor

    state, policy, payload, _originals = _fixture(tmp_path, f"quiesce-{fault}")
    request = parse_sealed_request(payload, state_dir=state)
    operations = ProductionOperations()
    operations.marker_principal = f"private-release:{request.request_id}"
    _prime_idle_gateways(policy, operations.marker_principal)
    gateway = policy.gateway_targets[0]
    state_path = gateway.profile_home / "gateway_state.json"
    marker_path = gateway.profile_home / ".drain_request.json"
    if fault == "stale":
        _prime_idle_gateways(
            policy,
            operations.marker_principal,
            updated_at="1970-01-01T00:00:00+00:00",
        )
    elif fault in {"pid_drift", "start_drift", "active"}:
        gateway_state = json.loads(state_path.read_text())
        field = {"pid_drift": "pid", "start_drift": "start_time", "active": "active_agents"}[fault]
        gateway_state[field] = 999 if fault != "active" else 1
        _write(state_path, json.dumps(gateway_state).encode())
    elif fault == "malformed":
        _write(state_path, b"{")
    elif fault == "unsafe_mode":
        state_path.chmod(0o622)
    elif fault == "foreign_marker":
        _write(marker_path, json.dumps({"principal": "someone-else"}).encode())

    monkeypatch.setattr(operations, "_drain", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        supervisor,
        "_show",
        lambda _unit: {"MainPID": "123", "ControlGroup": "/test"},
    )
    monkeypatch.setattr(supervisor, "_proc_start", lambda _pid: 456)
    monkeypatch.setattr(supervisor, "_proc_cmdline", lambda _pid: ["gateway"])
    monkeypatch.setattr(supervisor, "_proc_env", lambda _pid: {"HERMES_REPO": "/runtime-old"})
    resolve = Path.resolve

    def resolve_cwd(path, *args, **kwargs):
        if str(path) == "/proc/123/cwd":
            return Path("/runtime-old")
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_cwd)
    operations.baseline = {
        target.unit: {
            "pid": 123,
            "start_tick": 456,
            "cwd": "/runtime-old",
            "argv": ["gateway"],
            "env": {"HERMES_REPO": "/runtime-old"},
            "source": "/runtime-old",
        }
        for target in policy.targets
    }
    monkeypatch.setattr(
        operations,
        "_child_scopes_empty",
        lambda _status: fault != "child_scope",
    )
    monotonic = iter((0.0, 0.0, 1.0, 31.0))
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)

    with pytest.raises(PrivateReleaseError, match=match):
        operations.quiesce(request, policy, "candidate")


def test_dispatcher_canary_is_exact_non_mutating_dry_run(tmp_path, monkeypatch):
    state, policy, payload, _originals = _fixture(tmp_path, "dry-canary")
    request = parse_sealed_request(payload, state_dir=state)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return "gateway_drain_requested"

    monkeypatch.setattr("hermes_cli.private_release_supervisor._run", fake_run)
    result = ProductionOperations().canary_probe(request, policy)
    assert result == {"ok": True, "dispatcher": "non-mutating dry-run"}
    assert len(calls) == 1
    assert calls[0][0] == (
        request.runtime / "venv/bin/python", policy.dispatcher_wrapper, "--once", "--dry-run"
    )


@pytest.mark.parametrize("crash_phase", ["before_release", "after_release", "after_journal"])
@pytest.mark.parametrize("damage", [None, "health", "binding", "evidence"])
def test_committed_crash_never_rolls_back(tmp_path, monkeypatch, crash_phase, damage):
    from hermes_cli import private_release_supervisor as supervisor

    state, policy, payload, originals = _fixture(tmp_path)
    request = parse_sealed_request(payload, state_dir=state)
    operations = _operations(policy, originals, [])
    release = operations.release_quiescence

    def crash_at_release(*args):
        if crash_phase == "after_release":
            release(*args)
        raise KeyboardInterrupt("commit crash")

    write = supervisor._write_json_atomic

    def crash_at_terminal(path, value):
        if path == request.terminal_receipt_path:
            raise KeyboardInterrupt("terminal crash")
        write(path, value)

    if crash_phase == "after_journal":
        monkeypatch.setattr(supervisor, "_write_json_atomic", crash_at_terminal)
    else:
        operations.release_quiescence = crash_at_release
    with pytest.raises(KeyboardInterrupt):
        execute_private_release(payload, policy=policy, operations=operations, state_dir=state)
    monkeypatch.setattr(supervisor, "_write_json_atomic", write)
    calls = []
    recovery = _operations(policy, originals, calls)
    if damage == "health":
        recovery.health_probe = lambda *_args: False
    elif damage == "binding":
        policy.targets[0].binding.write_bytes(originals[policy.targets[0].unit])
    elif damage == "evidence":
        request.artifact_manifest.unlink()
    before = {target.unit: target.binding.read_bytes() for target in policy.targets}
    result = execute_private_release(payload, policy=policy, operations=recovery, state_dir=state)
    assert result["status"] == ("reconciliation_required" if damage else "succeeded")
    assert result["recovery"] == "committed_activation"
    assert result["mutated_units"] == [target.unit for target in policy.targets]
    assert not any(call[0] in {"binding", "restart", "quiesce", "canary"} for call in calls)
    assert all(target.binding.read_bytes() == before[target.unit] for target in policy.targets)
    if damage:
        assert "explicit reconciliation" in result["error"]
        assert ("release", "drains") not in calls
    else:
        assert calls == [("health", "candidate"), ("release", "drains")]


@pytest.mark.parametrize("state_change", [None, "partial", "mismatched", "unhealthy", "source"])
def test_different_request_already_bound_candidate_is_no_change(tmp_path, state_change):
    state, policy, payload, originals = _fixture(tmp_path)
    first = execute_private_release(
        payload, policy=policy, operations=_operations(policy, originals, []), state_dir=state
    )
    assert first["status"] == "succeeded"
    payload = dict(payload, request_id="request-2")
    if state_change == "partial":
        policy.targets[0].binding.write_bytes(originals[policy.targets[0].unit])
    elif state_change == "mismatched":
        target = policy.targets[0]
        target.binding.write_bytes(target.binding.read_bytes().replace(b"ExecStart=", b"ExecStart=wrong ", 1))
    calls = []
    operations = _operations(policy, originals, calls)
    if state_change == "unhealthy":
        operations.health_probe = lambda *_args: False
    elif state_change == "source":
        request = parse_sealed_request(payload, state_dir=state)
        (request.runtime / "candidate.txt").write_bytes(b"changed source")
    before = {target.unit: (target.binding.read_bytes(), target.binding.stat().st_ino) for target in policy.targets}
    result = execute_private_release(payload, policy=policy, operations=operations, state_dir=state)
    if state_change in {"partial", "mismatched"}:
        assert result["status"] == "succeeded"
        assert result["mutated_units"] == [policy.targets[0].unit]
        assert ("restart", "candidate") in calls
    else:
        assert result["status"] == ("no_change" if state_change is None else "rejected")
        assert result["mutated_units"] == []
        assert not any(call[0] in {"binding", "restart", "quiesce", "release"} for call in calls)
        assert all((target.binding.read_bytes(), target.binding.stat().st_ino) == before[target.unit] for target in policy.targets)
        if state_change is None:
            assert calls == [("health", "no_change")]
    calls.clear()
    replay = execute_private_release(payload, policy=policy, operations=operations, state_dir=state)
    assert replay["admission"] == "replay_rejected"
    assert calls == []


@pytest.mark.parametrize("mismatch", [None, "source", "command", "gateway"])
def test_no_change_health_checks_candidate_identity_without_restart(tmp_path, monkeypatch, mismatch):
    from hermes_cli import private_release_supervisor as supervisor
    from contextlib import contextmanager
    from io import BytesIO

    state, policy, payload, _originals = _fixture(tmp_path)
    request = parse_sealed_request(payload, state_dir=state)
    operations = ProductionOperations()
    operations.baseline = {target.unit: {"pid": 123, "start_tick": 456} for target in policy.targets}
    monkeypatch.setattr(supervisor, "_show", lambda _unit: {
        "ActiveState": "active", "SubState": "running", "MainPID": "123", "NRestarts": "0",
    })
    monkeypatch.setattr(supervisor, "_proc_start", lambda _pid: 456)
    monkeypatch.setattr(supervisor, "_proc_env", lambda _pid: {
        "HERMES_REPO": "wrong" if mismatch == "source" else str(request.runtime),
        "PYTHONPATH": str(request.runtime),
    })
    monkeypatch.setattr(supervisor, "_expected_command", lambda *_args: ["candidate"])
    monkeypatch.setattr(supervisor, "_proc_cmdline", lambda _pid: ["wrong" if mismatch == "command" else "candidate"])
    monkeypatch.setattr(operations, "_child_scopes_empty", lambda _status: True)
    resolve = Path.resolve

    def resolve_cwd(path, *args, **kwargs):
        return request.runtime if str(path) == "/proc/123/cwd" else resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_cwd)
    for target in policy.gateway_targets:
        _write(target.profile_home / "gateway_state.json", json.dumps({
            "pid": 123, "start_time": 456,
            "code_sha": "wrong" if mismatch == "gateway" else COMMIT,
        }).encode())

    @contextmanager
    def response(url, **_kwargs):
        body = b'{"gateway_running": true}' if not isinstance(url, str) else b"<html></html>"
        stream = BytesIO(body)
        stream.status = 200
        yield stream

    monkeypatch.setattr(supervisor, "urlopen", response)
    if mismatch:
        with pytest.raises(PrivateReleaseError, match="identity mismatch|escaped candidate"):
            operations._health_once(request, policy, "no_change")
    else:
        assert set(operations._health_once(request, policy, "no_change")) == set(operations.baseline)
        with pytest.raises(PrivateReleaseError, match="did not restart"):
            operations._health_once(request, policy, "candidate")


def test_committed_drain_error_reports_reconciliation_without_rollback(tmp_path):
    state, policy, payload, originals = _fixture(tmp_path)
    calls = []
    operations = _operations(policy, originals, calls)
    operations.release_quiescence = lambda *_args: False
    result = execute_private_release(payload, policy=policy, operations=operations, state_dir=state)
    assert result["status"] == "reconciliation_required"
    assert result["rollback"]["attempted"] is False
    assert result["rollback"]["restored_units"] == []
    assert "automatic rollback forbidden" in result["rollback"]["errors"][0]
    assert ("restart", "rollback") not in calls
    assert all(target.binding.read_bytes() != originals[target.unit] for target in policy.targets)
