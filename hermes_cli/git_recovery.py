"""Git recovery primitives for destructive managed-checkout updates."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RECOVERY_REF_PREFIX = "refs/hermes/recovery/pre-update/"


@dataclass(frozen=True)
class PullRecoveryResult:
    """Outcome of an ff-only pull with divergence recovery."""

    succeeded: bool
    recovery_ref: str | None = None
    reset_attempted: bool = False
    error: str | None = None


def recovery_ref_for_sha(head_sha: str) -> str:
    """Return the deterministic, valid Git ref for a full commit SHA."""
    if not _FULL_SHA_RE.fullmatch(head_sha):
        raise ValueError(f"expected a full lowercase commit SHA, got {head_sha!r}")
    return f"{_RECOVERY_REF_PREFIX}{head_sha}"


def create_verified_recovery_ref(
    git_cmd: Sequence[str], cwd: Path, head_sha: str
) -> tuple[str | None, str | None]:
    """Create a collision-safe recovery ref and independently verify it.

    An existing ref is accepted only when it already points at ``head_sha``.
    ``git update-ref`` is given the observed old value so a concurrent ref
    change cannot be silently overwritten. The final ``rev-parse`` is a
    separate readback operation, not an inference from ``update-ref``'s exit
    code.
    """
    try:
        recovery_ref = recovery_ref_for_sha(head_sha)
    except ValueError as exc:
        return None, str(exc)

    existing = subprocess.run(
        list(git_cmd) + ["rev-parse", "--verify", recovery_ref],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0:
        old_sha = existing.stdout.strip()
        if old_sha != head_sha:
            return (
                None,
                f"recovery ref {recovery_ref} already points to {old_sha or '<empty>'}, "
                f"not {head_sha}",
            )
    else:
        old_sha = "0" * 40

    update = subprocess.run(
        list(git_cmd) + ["update-ref", recovery_ref, head_sha, old_sha],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if update.returncode != 0:
        detail = update.stderr.strip() or "git update-ref failed"
        return None, detail

    readback = subprocess.run(
        list(git_cmd) + ["rev-parse", "--verify", recovery_ref],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if readback.returncode != 0 or readback.stdout.strip() != head_sha:
        detail = readback.stderr.strip() or f"read back {readback.stdout.strip()!r}"
        return None, f"recovery ref readback failed: {detail}"

    return recovery_ref, None


def pull_with_divergence_recovery(
    git_cmd: Sequence[str], cwd: Path, branch: str, pre_pull_sha: str | None
) -> PullRecoveryResult:
    """Pull fast-forward-only, recovering divergence only after ref proof."""
    pull = subprocess.run(
        list(git_cmd) + ["pull", "--ff-only", "origin", branch],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if pull.returncode == 0:
        return PullRecoveryResult(succeeded=True)

    if pre_pull_sha is None:
        return PullRecoveryResult(
            succeeded=False,
            error="could not resolve the pre-update HEAD; no reset performed",
        )

    recovery_ref, error = create_verified_recovery_ref(git_cmd, cwd, pre_pull_sha)
    if recovery_ref is None:
        return PullRecoveryResult(succeeded=False, error=error)

    reset = subprocess.run(
        list(git_cmd) + ["reset", "--hard", f"origin/{branch}"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if reset.returncode != 0:
        detail = reset.stderr.strip() or f"git reset exited {reset.returncode}"
        return PullRecoveryResult(
            succeeded=False,
            recovery_ref=recovery_ref,
            reset_attempted=True,
            error=detail,
        )

    return PullRecoveryResult(
        succeeded=True,
        recovery_ref=recovery_ref,
        reset_attempted=True,
    )
