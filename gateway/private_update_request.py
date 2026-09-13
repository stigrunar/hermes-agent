"""Private immutable update mode and fixed default-root adapter locations.

The mode is intentionally a selector, not a command hook.  The request and
helper paths are derived from the default Hermes root and cannot be overridden
by config.yaml.  This module is safe to import from gateway code because it
loads the full config only when a caller does not provide one explicitly.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

PRIVATE_IMMUTABLE_EXTERNAL = "private_immutable_external"
NATIVE_UPDATE_MODE = "native"
PRIVATE_UPDATE_STATE_RELATIVE_PATH = Path("private-update")
PRIVATE_UPDATE_REQUEST_RELATIVE_PATH = PRIVATE_UPDATE_STATE_RELATIVE_PATH / "request.json"
PRIVATE_UPDATE_HELPER_RELATIVE_PATH = PRIVATE_UPDATE_STATE_RELATIVE_PATH / "private_release_supervisor.py"
PRIVATE_UPDATE_LOCK_RELATIVE_PATH = PRIVATE_UPDATE_STATE_RELATIVE_PATH / ".private-immutable-release.lock"
PRIVATE_UPDATE_MANIFEST_RELATIVE_PATH = PRIVATE_UPDATE_STATE_RELATIVE_PATH / "manifest.json"
PRIVATE_UPDATE_POLICY_RELATIVE_PATH = PRIVATE_UPDATE_STATE_RELATIVE_PATH / "policy.json"
PRIVATE_UPDATE_RECEIPTS_RELATIVE_PATH = PRIVATE_UPDATE_STATE_RELATIVE_PATH / "receipts"
PRIVATE_RUNTIME_RELATIVE_PATH = Path("runtime")


class PrivateUpdateConfigError(ValueError):
    """The update selector is malformed or attempts to widen the adapter."""


@dataclass(frozen=True)
class PrivateUpdatePaths:
    default_root: Path
    state_dir: Path
    request_path: Path
    helper_path: Path
    lock_path: Path
    manifest_path: Path
    policy_path: Path
    receipts_dir: Path
    runtime_root: Path



def _default_root() -> Path:
    from hermes_constants import get_default_hermes_root

    root = get_default_hermes_root()
    if not root.is_absolute() or ".." in root.parts:
        raise PrivateUpdateConfigError("default Hermes root is not a safe absolute path")
    return root


def _updates_section(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if config is None:
        from hermes_cli.config import load_config

        config = load_config()
    if not isinstance(config, Mapping):
        raise PrivateUpdateConfigError("Hermes config must be an object")
    updates = config.get("updates", {})
    if not isinstance(updates, Mapping):
        raise PrivateUpdateConfigError("updates must be an object")
    return updates


def private_immutable_external_enabled(config: Mapping[str, Any] | None = None) -> bool:
    """Return whether the exact private immutable mode is selected."""
    updates = _updates_section(config)
    mode = updates.get("mode", NATIVE_UPDATE_MODE)
    if not isinstance(mode, str) or mode not in {NATIVE_UPDATE_MODE, PRIVATE_IMMUTABLE_EXTERNAL}:
        raise PrivateUpdateConfigError("updates.mode is unsupported")
    block = updates.get(PRIVATE_IMMUTABLE_EXTERNAL, {})
    if not isinstance(block, Mapping):
        raise PrivateUpdateConfigError(f"updates.{PRIVATE_IMMUTABLE_EXTERNAL} must be an object")
    unexpected = set(block) - {"enabled"}
    if unexpected:
        raise PrivateUpdateConfigError(
            f"updates.{PRIVATE_IMMUTABLE_EXTERNAL} has unsupported fields: {', '.join(sorted(unexpected))}"
        )
    if "enabled" not in block:
        enabled = None
    else:
        enabled = block["enabled"]
        if type(enabled) is not bool:
            raise PrivateUpdateConfigError(f"updates.{PRIVATE_IMMUTABLE_EXTERNAL}.enabled must be boolean")
    if mode == PRIVATE_IMMUTABLE_EXTERNAL and enabled is False:
        return False
    if mode == NATIVE_UPDATE_MODE and enabled:
        raise PrivateUpdateConfigError(
            f"updates.mode must be {PRIVATE_IMMUTABLE_EXTERNAL!r} when its block is enabled"
        )
    return mode == PRIVATE_IMMUTABLE_EXTERNAL


# Alias reads naturally at the call site and is kept alongside the descriptive name.
is_private_immutable_external_enabled = private_immutable_external_enabled


def resolve_private_update_paths(
    config: Mapping[str, Any] | None = None,
    *,
    default_root: Path | None = None,
) -> PrivateUpdatePaths:
    """Resolve fixed request/helper/lock paths without accepting path config."""
    private_immutable_external_enabled(config)
    root = _default_root() if default_root is None else Path(default_root)
    if not root.is_absolute() or ".." in root.parts:
        raise PrivateUpdateConfigError("default_root must be a safe absolute path")
    return PrivateUpdatePaths(
        root,
        root / PRIVATE_UPDATE_STATE_RELATIVE_PATH,
        root / PRIVATE_UPDATE_REQUEST_RELATIVE_PATH,
        root / PRIVATE_UPDATE_HELPER_RELATIVE_PATH,
        root / PRIVATE_UPDATE_LOCK_RELATIVE_PATH,
        root / PRIVATE_UPDATE_MANIFEST_RELATIVE_PATH,
        root / PRIVATE_UPDATE_POLICY_RELATIVE_PATH,
        root / PRIVATE_UPDATE_RECEIPTS_RELATIVE_PATH,
        root / PRIVATE_RUNTIME_RELATIVE_PATH,
    )


_MAX_PRIVATE_UPDATE_FILE_BYTES = 64 * 1024 * 1024


def _validate_file_stat(info: os.stat_result, path: Path, kind: str, mode: int | None) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise PrivateUpdateConfigError(f"{kind} must be an owner-owned regular file: {path}")
    uid = getattr(os, "getuid", lambda: info.st_uid)()
    if info.st_uid != uid:
        raise PrivateUpdateConfigError(f"{kind} has the wrong owner: {path}")
    actual_mode = stat.S_IMODE(info.st_mode)
    if mode is not None and actual_mode != mode:
        raise PrivateUpdateConfigError(f"{kind} must have mode {mode:04o}: {path}")
    if mode is None and actual_mode & 0o022:
        raise PrivateUpdateConfigError(f"{kind} is group/other writable: {path}")


def _read_owned_file(path: Path, kind: str, *, mode: int | None = None) -> bytes:
    """Read and validate one file through the descriptor that was opened."""
    path = Path(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                raise PrivateUpdateConfigError(f"{kind} must not be a symlink: {path}")
        except OSError as exc:
            raise PrivateUpdateConfigError(f"{kind} is not readable: {path}") from exc
    try:
        fd = os.open(path, os.O_RDONLY | nofollow)
    except OSError as exc:
        raise PrivateUpdateConfigError(f"{kind} is not readable: {path}") from exc
    try:
        before = os.fstat(fd)
        _validate_file_stat(before, path, kind, mode)
        if before.st_size > _MAX_PRIVATE_UPDATE_FILE_BYTES:
            raise PrivateUpdateConfigError(f"{kind} is too large: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, _MAX_PRIVATE_UPDATE_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_PRIVATE_UPDATE_FILE_BYTES:
                raise PrivateUpdateConfigError(f"{kind} is too large: {path}")
        after = os.fstat(fd)
        _validate_file_stat(after, path, kind, mode)
        if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size):
            raise PrivateUpdateConfigError(f"{kind} changed while being read: {path}")
        return b"".join(chunks)
    except OSError as exc:
        raise PrivateUpdateConfigError(f"{kind} could not be read: {path}") from exc
    finally:
        os.close(fd)


def read_private_update_file(
    path: Path,
    *,
    kind: str,
    expected_sha256: str | None = None,
) -> tuple[bytes, str]:
    """Return validated bytes and their digest from the same open descriptor."""
    mode = 0o600 if kind in {"request", "lock", "manifest", "policy", "receipt"} else None
    data = _read_owned_file(Path(path), kind, mode=mode)
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise PrivateUpdateConfigError(f"{kind} hash does not match its sealed identity: {path}")
    return data, digest


def validate_private_update_file(
    path: Path,
    *,
    kind: str,
    expected_sha256: str | None = None,
) -> str:
    """Validate a fixed adapter file and return its descriptor-bound digest."""
    return read_private_update_file(path, kind=kind, expected_sha256=expected_sha256)[1]


def validate_private_update_helper(paths: PrivateUpdatePaths, *, expected_sha256: str) -> str:
    """Verify the staged helper is the fixed host-state artifact, not source."""
    if paths.helper_path.parent != paths.state_dir:
        raise PrivateUpdateConfigError("private update helper is outside the fixed host state directory")
    return validate_private_update_file(paths.helper_path, kind="helper", expected_sha256=expected_sha256)
