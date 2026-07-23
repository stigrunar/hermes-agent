"""Shared release-channel policy for git-based Hermes updates.

An explicit CLI branch wins. Otherwise ``updates.release_channel`` in the
active profile's config is authoritative. Ordinary upstream checkouts retain
``main`` compatibility, but a checkout with Stig's purpose-named ``stig``
remote must be configured and fails closed rather than silently deploying
``main``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from hermes_constants import get_hermes_home


class UpdateChannelError(ValueError):
    """Raised when a safe update channel cannot be resolved."""


_MISSING = object()
STIG_TESTED_RELEASE_BRANCH = "release/stig-tested"


@dataclass(frozen=True)
class UpdateTarget:
    """Validated git remote and branch selected for an update."""

    remote: str
    branch: str


def _load_config(config_path: Path) -> Mapping[str, Any]:
    if not config_path.exists():
        return {}
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise UpdateChannelError(
            f"cannot read update policy from {config_path}: {exc}"
        ) from exc
    if not isinstance(data, Mapping):
        raise UpdateChannelError(f"update policy file is not a mapping: {config_path}")
    return data


def _configured_channel(config: Mapping[str, Any]) -> Optional[str]:
    updates = config.get("updates", {})
    if updates is None:
        return None
    if not isinstance(updates, Mapping):
        raise UpdateChannelError("updates config must be a mapping")
    raw = updates.get("release_channel")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise UpdateChannelError("updates.release_channel must be a non-empty branch name")
    return raw.strip()


def _validate_branch(branch: Any, *, source: str) -> str:
    """Validate a branch name without invoking git or changing the checkout."""
    if not isinstance(branch, str) or not branch.strip():
        raise UpdateChannelError(f"{source} must be a non-empty branch name")
    value = branch.strip()
    if (
        value.startswith(("-", "/"))
        or value.endswith(("/", "."))
        or value in {".", "..", "@"}
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(char.isspace() or ord(char) < 0x20 for char in value)
        or any(char in value for char in "~^:?*[\\")
        or any(
            part.startswith(".")
            or part.endswith((".", ".lock"))
            for part in value.split("/")
        )
    ):
        raise UpdateChannelError(f"{source} is not a valid branch name: {value!r}")
    return value


def _has_remote(project_root: Path, name: str) -> bool:
    dot_git = project_root / ".git"
    try:
        if dot_git.is_file():
            marker = dot_git.read_text(encoding="utf-8").strip()
            if not marker.startswith("gitdir:"):
                raise UpdateChannelError(f"invalid gitdir marker: {dot_git}")
            git_dir = Path(marker.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = (project_root / git_dir).resolve()
            common_dir_file = git_dir / "commondir"
            if common_dir_file.exists():
                common_dir = Path(common_dir_file.read_text(encoding="utf-8").strip())
                git_dir = common_dir if common_dir.is_absolute() else (git_dir / common_dir).resolve()
        else:
            git_dir = dot_git
        config_path = git_dir / "config"
        if not config_path.exists():
            return False
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UpdateChannelError(f"cannot inspect git remotes: {exc}") from exc
    pattern = rf'^\s*\[remote\s+"{re.escape(name)}"\]\s*$'
    return re.search(pattern, text, flags=re.MULTILINE) is not None


def resolve_update_target(
    explicit_branch: Optional[str] = None,
    *,
    project_root: Path,
    config: Any = _MISSING,
    config_path: Optional[Path] = None,
) -> UpdateTarget:
    """Resolve the validated update remote and branch.

    ``main`` remains the generic upstream default only for checkouts without the
    Stig deployment remote. A checkout carrying a remote literally named
    ``stig`` must declare ``updates.release_channel`` or pass an explicit branch.
    """
    if explicit_branch is not None:
        branch = _validate_branch(explicit_branch, source="--branch")
        remote = "stig" if branch == STIG_TESTED_RELEASE_BRANCH and _has_remote(
            Path(project_root), "stig"
        ) else "origin"
        return UpdateTarget(remote=remote, branch=branch)

    if config is _MISSING:
        path = config_path or (get_hermes_home() / "config.yaml")
        config = _load_config(path)
    if not isinstance(config, Mapping):
        raise UpdateChannelError("update config must be a mapping")

    configured = _configured_channel(config)
    has_stig_remote = _has_remote(Path(project_root), "stig")
    if has_stig_remote:
        if configured != STIG_TESTED_RELEASE_BRANCH:
            detail = (
                "not configured"
                if configured is None
                else f"configured as {configured!r}"
            )
            raise UpdateChannelError(
                "Stig deployment channel is "
                f"{detail}; expected {STIG_TESTED_RELEASE_BRANCH} or pass "
                "--branch explicitly"
            )
        return UpdateTarget(remote="stig", branch=STIG_TESTED_RELEASE_BRANCH)

    branch = _validate_branch(configured or "main", source="updates.release_channel")
    return UpdateTarget(remote="origin", branch=branch)


def resolve_update_branch(
    explicit_branch: Optional[str] = None,
    *,
    project_root: Path,
    config: Any = _MISSING,
    config_path: Optional[Path] = None,
) -> str:
    """Backward-compatible branch-only adapter for existing callers."""
    return resolve_update_target(
        explicit_branch,
        project_root=project_root,
        config=config,
        config_path=config_path,
    ).branch
