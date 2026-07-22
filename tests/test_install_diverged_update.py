"""Executable managed-installer tests for divergence recovery."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
PWSH = shutil.which("pwsh")


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="needs git and bash",
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


def _checkout_pair(tmp_path: Path) -> tuple[Path, Path, str]:
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
    return seed, managed, _sha(managed)


def _remote_commit(seed: Path, message: str) -> str:
    _git_with_identity(seed, "commit", "--allow-empty", "-m", message)
    _git(seed, "push", "origin", "main")
    return _sha(seed)


def _local_commit(managed: Path) -> str:
    _git_with_identity(managed, "commit", "--allow-empty", "-m", "local-only")
    return _sha(managed)


def _run_installer(installer: str, managed: Path, hermes_home: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HERMES_HOME": str(hermes_home)}
    if installer == "sh":
        command = [
            "bash",
            str(INSTALL_SH),
            "--stage",
            "repository",
            "--dir",
            str(managed),
            "--non-interactive",
        ]
    else:
        if PWSH is None:
            pytest.skip("pwsh unavailable")
        command = [
            PWSH,
            "-NoProfile",
            "-File",
            str(INSTALL_PS1),
            "-Stage",
            "repository",
            "-InstallDir",
            str(managed),
            "-NonInteractive",
        ]
    return subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True, text=True)


INSTALLERS = [
    pytest.param("sh", id="install-sh"),
    pytest.param("ps1", id="install-ps1", marks=pytest.mark.skipif(PWSH is None, reason="pwsh unavailable")),
]


@pytest.mark.parametrize("installer", INSTALLERS)
def test_managed_installer_divergence_preserves_local_commit(
    tmp_path: Path, installer: str
) -> None:
    seed, managed, _ = _checkout_pair(tmp_path)
    remote_sha = _remote_commit(seed, "remote-only")
    local_sha = _local_commit(managed)
    recovery_ref = f"refs/hermes/recovery/pre-update/{local_sha}"

    result = _run_installer(installer, managed, tmp_path / "hermes-home")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _sha(managed) == remote_sha
    assert _sha(managed, recovery_ref) == local_sha
    output = result.stdout + result.stderr
    assert f"Manual recovery: git reset --hard {recovery_ref}" in output


@pytest.mark.parametrize("installer", INSTALLERS)
def test_managed_installer_ref_failure_leaves_head_unchanged(
    tmp_path: Path, installer: str
) -> None:
    seed, managed, _ = _checkout_pair(tmp_path)
    remote_sha = _remote_commit(seed, "remote-only")
    local_sha = _local_commit(managed)
    recovery_ref = f"refs/hermes/recovery/pre-update/{local_sha}"
    base_sha = _sha(managed, "HEAD~1")
    _git(managed, "update-ref", recovery_ref, base_sha)

    result = _run_installer(installer, managed, tmp_path / "hermes-home")

    assert result.returncode != 0
    assert _sha(managed) == local_sha
    assert _sha(managed, recovery_ref) == base_sha
    assert _sha(seed) == remote_sha
    output = result.stdout + result.stderr
    assert "no reset performed" in output.lower()


@pytest.mark.parametrize("installer", INSTALLERS)
def test_managed_installer_fast_forward_creates_no_recovery_ref(
    tmp_path: Path, installer: str
) -> None:
    seed, managed, base_sha = _checkout_pair(tmp_path)
    remote_sha = _remote_commit(seed, "remote-only")
    recovery_ref = f"refs/hermes/recovery/pre-update/{base_sha}"

    result = _run_installer(installer, managed, tmp_path / "hermes-home")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _sha(managed) == remote_sha
    assert _git(managed, "rev-parse", "--verify", recovery_ref, check=False).returncode != 0
