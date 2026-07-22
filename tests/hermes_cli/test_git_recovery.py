"""Executable Git tests for managed-update divergence recovery."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli.git_recovery import (
    create_verified_recovery_ref,
    pull_with_divergence_recovery,
    recovery_ref_for_sha,
)


pytestmark = pytest.mark.skipif(
    __import__("shutil").which("git") is None,
    reason="needs the real git CLI",
)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True
    )


def _git_with_identity(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _git(
        cwd,
        "-c",
        "user.email=hermes-test@example.invalid",
        "-c",
        "user.name=Hermes test",
        *args,
    )


def _sha(cwd: Path, ref: str = "HEAD") -> str:
    return _git(cwd, "rev-parse", ref).stdout.strip()


def _checkout_pair(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    managed = tmp_path / "managed"
    _git(tmp_path, "init", "--bare", str(bare))
    seed.mkdir()
    _git(seed, "init")
    _git(seed, "branch", "-M", "main")
    _git_with_identity(seed, "commit", "--allow-empty", "-m", "base")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", "--branch", "main", str(bare), str(managed))
    _git_with_identity(managed, "config", "user.email", "hermes-test@example.invalid")
    _git_with_identity(managed, "config", "user.name", "Hermes test")
    return bare, seed, managed, _sha(managed)


def _push_remote_commit(seed: Path, message: str) -> str:
    _git_with_identity(seed, "commit", "--allow-empty", "-m", message)
    _git(seed, "push", "origin", "main")
    return _sha(seed)


def _create_local_commit(managed: Path, message: str) -> str:
    _git_with_identity(managed, "commit", "--allow-empty", "-m", message)
    return _sha(managed)


def test_divergent_update_creates_verified_ref_before_reset(tmp_path: Path) -> None:
    _, seed, managed, base_sha = _checkout_pair(tmp_path)
    remote_sha = _push_remote_commit(seed, "remote-only")
    local_sha = _create_local_commit(managed, "local-only")
    assert base_sha != local_sha != remote_sha

    result = pull_with_divergence_recovery(["git"], managed, "main", local_sha)

    assert result.succeeded is True
    assert result.reset_attempted is True
    assert result.recovery_ref == recovery_ref_for_sha(local_sha)
    assert _sha(managed) == remote_sha
    assert _sha(managed, result.recovery_ref) == local_sha


def test_recovery_ref_collision_fails_closed_without_reset(tmp_path: Path) -> None:
    _, seed, managed, _ = _checkout_pair(tmp_path)
    remote_sha = _push_remote_commit(seed, "remote-only")
    local_sha = _create_local_commit(managed, "local-only")
    recovery_ref = recovery_ref_for_sha(local_sha)
    _git(managed, "update-ref", recovery_ref, _sha(managed, "HEAD~1"))

    result = pull_with_divergence_recovery(["git"], managed, "main", local_sha)

    assert result.succeeded is False
    assert result.reset_attempted is False
    assert result.recovery_ref is None
    assert _sha(managed) == local_sha
    assert _sha(managed, recovery_ref) != local_sha
    assert _sha(seed) == remote_sha


def test_fast_forward_does_not_create_recovery_ref(tmp_path: Path) -> None:
    _, seed, managed, base_sha = _checkout_pair(tmp_path)
    remote_sha = _push_remote_commit(seed, "remote-only")

    result = pull_with_divergence_recovery(["git"], managed, "main", base_sha)

    assert result.succeeded is True
    assert result.reset_attempted is False
    assert result.recovery_ref is None
    assert _sha(managed) == remote_sha
    assert _git(
        managed, "rev-parse", "--verify", recovery_ref_for_sha(base_sha), check=False
    ).returncode != 0


def test_existing_matching_recovery_ref_is_verified_and_reused(tmp_path: Path) -> None:
    _, _, managed, base_sha = _checkout_pair(tmp_path)
    recovery_ref = recovery_ref_for_sha(base_sha)
    _git(managed, "update-ref", recovery_ref, base_sha)

    ref, error = create_verified_recovery_ref(["git"], managed, base_sha)

    assert error is None
    assert ref == recovery_ref
    assert _sha(managed, recovery_ref) == base_sha
