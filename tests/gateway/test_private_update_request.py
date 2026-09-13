"""Fixed-path and descriptor-safety tests for private immutable update mode."""

import hashlib
import os

import pytest

from gateway.private_update_request import (
    PRIVATE_IMMUTABLE_EXTERNAL,
    PrivateUpdateConfigError,
    private_immutable_external_enabled,
    resolve_private_update_paths,
    validate_private_update_file,
)


def test_native_mode_is_off_and_all_private_paths_are_default_root_derived(tmp_path):
    paths = resolve_private_update_paths({"updates": {"mode": "native"}}, default_root=tmp_path)
    assert private_immutable_external_enabled({"updates": {"mode": "native"}}) is False
    assert paths.state_dir == tmp_path / "private-update"
    assert paths.request_path == paths.state_dir / "request.json"
    assert paths.helper_path == paths.state_dir / "private_release_supervisor.py"
    assert paths.policy_path == paths.state_dir / "policy.json"
    assert paths.manifest_path == paths.state_dir / "manifest.json"
    assert paths.lock_path == paths.state_dir / ".private-immutable-release.lock"
    assert paths.receipts_dir == paths.state_dir / "receipts"
    assert paths.runtime_root == tmp_path / "runtime"


def test_private_mode_allows_no_path_or_command_configuration():
    assert private_immutable_external_enabled(
        {"updates": {"mode": PRIVATE_IMMUTABLE_EXTERNAL}}
    ) is True
    for key in ("command", "request_path", "helper_path", "targets", "unit"):
        with pytest.raises(PrivateUpdateConfigError, match="unsupported fields"):
            private_immutable_external_enabled({
                "updates": {
                    "mode": PRIVATE_IMMUTABLE_EXTERNAL,
                    PRIVATE_IMMUTABLE_EXTERNAL: {key: "/tmp/attacker"},
                }
            })


@pytest.mark.parametrize("mode", ["shell", "private", "private_immutable_external "])
def test_unknown_mode_fails_closed(mode):
    with pytest.raises(PrivateUpdateConfigError):
        private_immutable_external_enabled({"updates": {"mode": mode}})


def test_fixed_files_reject_mode_symlink_and_hash_mismatch(tmp_path):
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")
    request.chmod(0o600)
    expected = hashlib.sha256(request.read_bytes()).hexdigest()
    assert validate_private_update_file(
        request, kind="request", expected_sha256=expected
    ) == expected
    request.chmod(0o644)
    with pytest.raises(PrivateUpdateConfigError, match="0600"):
        validate_private_update_file(request, kind="request")
    request.chmod(0o600)
    with pytest.raises(PrivateUpdateConfigError, match="hash"):
        validate_private_update_file(request, kind="request", expected_sha256="0" * 64)
    real = tmp_path / "real-helper.py"
    real.write_text("pass\n", encoding="utf-8")
    helper = tmp_path / "helper.py"
    helper.symlink_to(real)
    with pytest.raises(PrivateUpdateConfigError, match="readable|regular"):
        validate_private_update_file(helper, kind="helper")


def test_owner_guard_is_checked_when_chown_is_available(tmp_path):
    if not hasattr(os, "chown") or not hasattr(os, "getuid") or os.getuid() != 0:
        pytest.skip("requires a root-capable Linux test process")
    path = tmp_path / "request.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    os.chown(path, 65534, -1)
    with pytest.raises(PrivateUpdateConfigError, match="owner"):
        validate_private_update_file(path, kind="request")
