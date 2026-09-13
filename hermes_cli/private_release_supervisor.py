#!/usr/bin/env python3
"""Host-owned supervisor for one fixed six-service immutable release transaction.

The installed copy is stdlib-only. Candidate requests carry identity and
immutable evidence digests, never paths, units, commands, or release topology.
"""

from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.request import Request, urlopen

REQUEST_SCHEMA = "hri.private_immutable_release_request"
POLICY_SCHEMA = "hri.private_immutable_release_policy"
INSTALLATION_SCHEMA = "hri.private_immutable_release_installation"
STAGE_SCHEMA = "hri.private_immutable_stage_manifest"
PUBLICATION_SCHEMA = "hri.private_publication_receipt"
BACKUP_SCHEMA = "hri.private_immutable_rollback_bundle"
RECEIPT_SCHEMA = "hri.private_immutable_release_receipt"
SCHEMA_VERSION = 1
REQUEST_MODE = 0o600
RECEIPT_MODE = 0o600
TARGET_COUNT = 6
ACTIVE_FIELDS = ("active_agents", "active_cron_jobs", "active_api_runs")
_HEX_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MAX_FILE_BYTES = 64 * 1024 * 1024
_PRIVATE_REPOSITORY = "stigrunar/hermes-agent-review"
_PRIVATE_RELEASE_REF_RE = re.compile(r"release/stig-tested[A-Za-z0-9._/-]*")
_HOST_GIT = "/usr/bin/git"
_GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "GIT_TERMINAL_PROMPT": "0",
}
_SAFE_ENV = frozenset({
    "HERMES_HOME", "HERMES_REPO", "PYTHONPATH", "VIRTUAL_ENV", "HERMES_PROFILE",
    "HERMES_PYTHON", "HERMES_WEB_DIST", "HERMES_TUI_DIR", "HERMES_SAFE_DISPATCH_BOARDS",
    "HERMES_KANBAN_DB", "HERMES_BIN", "HOME", "PATH",
})


class PrivateReleaseError(RuntimeError):
    """Base fail-closed release error."""


class RequestValidationError(PrivateReleaseError, ValueError):
    """Host policy, request, or immutable evidence is invalid."""


class ConcurrentActivationError(PrivateReleaseError):
    """Another fixed private activation owns the lock."""


@dataclass(frozen=True)
class CandidateIdentity:
    commit: str
    tree: str


@dataclass(frozen=True)
class FixedTarget:
    unit: str
    kind: str
    binding: Path
    profile_home: Path | None


@dataclass(frozen=True)
class HostPolicy:
    default_root: Path
    os_home: Path
    runtime_root: Path
    gateway_wrapper: Path
    dispatcher_wrapper: Path
    gateway_wrapper_sha256: str
    dispatcher_wrapper_sha256: str
    python_path: Path
    python_sha256: str
    targets: tuple[FixedTarget, ...]
    dashboard_status_url: str
    dashboard_root_url: str
    drain_timeout_seconds: int
    health_timeout_seconds: int
    sustained_seconds: int

    @property
    def gateway_targets(self) -> tuple[FixedTarget, ...]:
        return tuple(target for target in self.targets if target.kind == "gateway")


@dataclass(frozen=True)
class PrivateReleaseRequest:
    request_id: str
    project_id: str
    outcome_id: str
    execution_id: str
    correlation_id: str
    candidate: CandidateIdentity
    private_publication_sha256: str
    artifact_manifest_sha256: str
    state_dir: Path
    runtime: Path
    publication_receipt: Path
    artifact_manifest: Path

    @property
    def receipt_dir(self) -> Path:
        return self.state_dir / "receipts"

    @property
    def terminal_receipt_path(self) -> Path:
        return self.receipt_dir / f"{self.request_id}.terminal.json"

    @property
    def prestate_path(self) -> Path:
        return self.receipt_dir / f"{self.request_id}.prestate.json"

    @property
    def journal_path(self) -> Path:
        return self.receipt_dir / f"{self.request_id}.journal.json"

    @property
    def backup_path(self) -> Path:
        return self.receipt_dir / f"{self.request_id}.rollback.json"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / ".private-immutable-release.lock"


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise RequestValidationError(f"value is not canonical JSON: {exc}") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(_read_owned_bytes(path, "hashed file"))


def _exact(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RequestValidationError(f"{label} must be an object")
    missing, extra = sorted(expected - set(value)), sorted(set(value) - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise RequestValidationError(f"{label} has invalid fields ({'; '.join(details)})")
    return value


def _string(value: Any, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if type(value) is not str or not value or (pattern is not None and pattern.fullmatch(value) is None):
        raise RequestValidationError(f"{label} has an invalid format")
    return value


def _digest(value: Any, label: str, *, sha256: bool = False) -> str:
    return _string(value, label, _SHA256_RE if sha256 else _HEX_RE)


def _safe_absolute(path: Path, label: str) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise RequestValidationError(f"{label} is not a safe absolute path")
    return path


def _validate_stat(
    info: os.stat_result, path: Path, label: str, *, mode: int | None, directory: bool
) -> None:
    expected_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected_kind:
        noun = "directory" if directory else "regular file"
        raise RequestValidationError(f"{label} must be an owner-owned {noun}: {path}")
    uid = getattr(os, "getuid", lambda: info.st_uid)()
    if info.st_uid != uid:
        raise RequestValidationError(f"{label} has the wrong owner: {path}")
    actual = stat.S_IMODE(info.st_mode)
    if mode is not None and actual != mode:
        raise RequestValidationError(f"{label} has mode {actual:04o}; expected {mode:04o}: {path}")
    if mode is None and actual & 0o022:
        raise RequestValidationError(f"{label} is group/other writable: {path}")


def _read_owned(path: Path, label: str, *, mode: int | None = None) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RequestValidationError(f"{label} is not readable: {path}") from exc
    try:
        before = os.fstat(fd)
        _validate_stat(before, path, label, mode=mode, directory=False)
        if before.st_size > _MAX_FILE_BYTES:
            raise RequestValidationError(f"{label} is too large: {path}")
        chunks, total = [], 0
        while True:
            block = os.read(fd, min(1024 * 1024, _MAX_FILE_BYTES + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > _MAX_FILE_BYTES:
                raise RequestValidationError(f"{label} is too large: {path}")
        after = os.fstat(fd)
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
        )
        if identity(before) != identity(after):
            raise RequestValidationError(f"{label} changed while being read: {path}")
        return b"".join(chunks), after
    finally:
        os.close(fd)


def _read_owned_bytes(path: Path, label: str, *, mode: int | None = None) -> bytes:
    return _read_owned(path, label, mode=mode)[0]


def _read_boundary_bytes(path: Path, label: str) -> bytes:
    """Read an executable boundary owned by this account or the host root."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RequestValidationError(f"{label} is not readable: {path}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RequestValidationError(f"{label} is not a regular file: {path}")
        if (
            before.st_uid not in {0, os.getuid()}
            or before.st_mode & 0o002
            or (before.st_uid != os.getuid() and before.st_mode & 0o020)
        ):
            raise RequestValidationError(f"{label} owner/mode validation failed: {path}")
        if before.st_size > _MAX_FILE_BYTES:
            raise RequestValidationError(f"{label} is too large: {path}")
        chunks, total = [], 0
        while True:
            block = os.read(fd, min(1024 * 1024, _MAX_FILE_BYTES + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > _MAX_FILE_BYTES:
                raise RequestValidationError(f"{label} is too large: {path}")
        after = os.fstat(fd)
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
        )
        if identity(before) != identity(after):
            raise RequestValidationError(f"{label} changed while being read: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _hash_staged_payload(path: Path, label: str, *, boundary: bool = False) -> str:
    """Hash payloads at constant memory cost, binding the digest to a stable file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RequestValidationError(f"{label} is not readable: {path}") from exc
    try:
        before = os.fstat(fd)
        if boundary:
            if not stat.S_ISREG(before.st_mode):
                raise RequestValidationError(f"{label} is not a regular file: {path}")
            if (
                before.st_uid not in {0, os.getuid()}
                or before.st_mode & 0o002
                or (before.st_uid != os.getuid() and before.st_mode & 0o020)
            ):
                raise RequestValidationError(f"{label} owner/mode validation failed: {path}")
        else:
            _validate_stat(before, path, label, mode=None, directory=False)
        digest = hashlib.sha256()
        while block := os.read(fd, 1024 * 1024):
            digest.update(block)
        after = os.fstat(fd)
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_mode, value.st_uid,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns,
        )
        try:
            current = path.lstat()
        except OSError as exc:
            raise RequestValidationError(f"{label} changed while being hashed: {path}") from exc
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise RequestValidationError(f"{label} changed while being hashed: {path}")
        return digest.hexdigest()
    finally:
        os.close(fd)


def _read_json(
    path: Path, label: str, *, mode: int | None = None, expected_sha256: str | None = None
) -> Mapping[str, Any]:
    raw = _read_owned_bytes(path, label, mode=mode)
    if expected_sha256 is not None and sha256_bytes(raw) != expected_sha256:
        raise RequestValidationError(f"{label} digest mismatch: {path}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestValidationError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, Mapping):
        raise RequestValidationError(f"{label} must be an object")
    return value


def _secure_directory(path: Path, label: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RequestValidationError(f"{label} is not readable: {path}") from exc
    try:
        _validate_stat(os.fstat(fd), path, label, mode=None, directory=True)
    finally:
        os.close(fd)


def _expected_targets(default_root: Path, os_home: Path) -> tuple[FixedTarget, ...]:
    unit_root = os_home / ".config/systemd/user"
    definitions = (
        ("hermes-gateway.service", "gateway", default_root),
        ("hermes-gateway-dollydesign.service", "gateway", default_root / "profiles/dollydesign"),
        ("hermes-gateway-dollyops.service", "gateway", default_root / "profiles/dollyops"),
        ("hermes-gateway-dollyprivate.service", "gateway", default_root / "profiles/dollyprivate"),
        ("hermes-dashboard.service", "dashboard", None),
        ("hermes-kanban-safe-dispatcher.service", "dispatcher", None),
    )
    return tuple(
        FixedTarget(unit, kind, unit_root / f"{unit}.d/99-z-downstream-main.conf", profile)
        for unit, kind, profile in definitions
    )


def parse_host_policy(
    source: Path | Mapping[str, Any], *, default_root: Path, expected_os_home: Path | None = None
) -> HostPolicy:
    default_root = _safe_absolute(Path(default_root), "default Hermes root")
    value = (
        _read_json(Path(source), "host policy", mode=REQUEST_MODE)
        if isinstance(source, (str, Path)) else source
    )
    policy = _exact(value, {
        "schema", "version", "os_home", "runtime_root", "gateway_wrapper",
        "dispatcher_wrapper", "activation_files", "default_state_db", "targets", "health",
        "timeouts",
    }, "host policy")
    if policy["schema"] != POLICY_SCHEMA or policy["version"] != SCHEMA_VERSION:
        raise RequestValidationError("host policy schema/version is unsupported")
    os_home = _safe_absolute(Path(_string(policy["os_home"], "host policy os_home")), "os home")
    owner_home = Path(pwd.getpwuid(os.getuid()).pw_dir) if expected_os_home is None else Path(expected_os_home)
    if os_home != owner_home:
        raise RequestValidationError("host policy os_home does not match the owning OS account")
    runtime_root = _safe_absolute(Path(_string(policy["runtime_root"], "runtime root")), "runtime root")
    gateway_wrapper = _safe_absolute(
        Path(_string(policy["gateway_wrapper"], "gateway wrapper")), "gateway wrapper"
    )
    dispatcher_wrapper = _safe_absolute(
        Path(_string(policy["dispatcher_wrapper"], "dispatcher wrapper")), "dispatcher wrapper"
    )
    if (
        runtime_root != default_root / "runtime"
        or gateway_wrapper != default_root / "scripts/hermes_gateway_with_x11.sh"
        or dispatcher_wrapper != default_root / "scripts/kanban_safe_dispatch_loop.py"
    ):
        raise RequestValidationError("host policy fixed runtime/wrapper topology mismatch")
    activation_files = _exact(
        policy["activation_files"],
        {
            "gateway_wrapper_sha256", "dispatcher_wrapper_sha256",
            "python_path", "python_sha256",
        },
        "activation_files",
    )
    gateway_wrapper_sha256 = _digest(
        activation_files["gateway_wrapper_sha256"], "gateway wrapper digest", sha256=True
    )
    dispatcher_wrapper_sha256 = _digest(
        activation_files["dispatcher_wrapper_sha256"], "dispatcher wrapper digest", sha256=True
    )
    python_path = _safe_absolute(
        Path(_string(activation_files["python_path"], "host Python path")),
        "host Python path",
    ).resolve(strict=True)
    python_sha256 = _digest(
        activation_files["python_sha256"], "host Python digest", sha256=True
    )
    state_db = _exact(
        policy["default_state_db"], {"path", "backup_integrity", "restore"}, "default_state_db"
    )
    expected_state_db = {
        "path": str(default_root / "state.db"),
        "backup_integrity": "excluded",
        "restore": "forbidden",
    }
    if dict(state_db) != expected_state_db:
        raise RequestValidationError("default-profile state DB exclusion policy mismatch")
    expected = _expected_targets(default_root, os_home)
    raw_targets = policy["targets"]
    if type(raw_targets) is not list or len(raw_targets) != TARGET_COUNT:
        raise RequestValidationError(f"host policy must contain exactly {TARGET_COUNT} targets")
    actual = []
    for index, raw in enumerate(raw_targets):
        item = _exact(raw, {"unit", "kind", "binding", "profile_home"}, f"targets[{index}]")
        profile = item["profile_home"]
        actual.append(FixedTarget(
            _string(item["unit"], f"targets[{index}].unit"),
            _string(item["kind"], f"targets[{index}].kind"),
            _safe_absolute(Path(_string(item["binding"], f"targets[{index}].binding")), "binding"),
            None if profile is None else _safe_absolute(
                Path(_string(profile, f"targets[{index}].profile_home")), "profile home"
            ),
        ))
    if tuple(actual) != expected:
        raise RequestValidationError("host policy target topology is not the fixed six-target contract")
    health = _exact(
        policy["health"], {"dashboard_status_url", "dashboard_root_url"}, "health"
    )
    expected_health = {
        "dashboard_status_url": "http://127.0.0.1:9120/api/status",
        "dashboard_root_url": "http://127.0.0.1:9120/",
    }
    if dict(health) != expected_health:
        raise RequestValidationError("host policy health topology mismatch")
    timeouts = _exact(
        policy["timeouts"], {"drain_seconds", "health_seconds", "sustained_seconds"}, "timeouts"
    )
    values = tuple(timeouts[key] for key in ("drain_seconds", "health_seconds", "sustained_seconds"))
    if any(type(value) is not int or value <= 0 for value in values):
        raise RequestValidationError("host policy timeouts must be positive integers")
    return HostPolicy(
        default_root, os_home, runtime_root, gateway_wrapper, dispatcher_wrapper,
        gateway_wrapper_sha256, dispatcher_wrapper_sha256, python_path, python_sha256, expected,
        health["dashboard_status_url"], health["dashboard_root_url"], *values,
    )


def seal_private_release_request(
    *, request_id: str, project_id: str, outcome_id: str, execution_id: str,
    correlation_id: str, commit: str, tree: str, private_publication_sha256: str,
    artifact_manifest_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": REQUEST_SCHEMA,
        "version": SCHEMA_VERSION,
        "sealed": True,
        "request_id": _string(request_id, "request_id", _ID_RE),
        "project_id": _string(project_id, "project_id", _ID_RE),
        "outcome_id": _string(outcome_id, "outcome_id", _ID_RE),
        "execution_id": _string(execution_id, "execution_id", _ID_RE),
        "correlation_id": _string(correlation_id, "correlation_id", _ID_RE),
        "candidate": {
            "commit": _digest(commit, "candidate.commit"),
            "tree": _digest(tree, "candidate.tree"),
        },
        "private_publication_sha256": _digest(
            private_publication_sha256, "private_publication_sha256", sha256=True
        ),
        "artifact_manifest_sha256": _digest(
            artifact_manifest_sha256, "artifact_manifest_sha256", sha256=True
        ),
    }


def _staged_symlink_snapshot(path: Path) -> tuple[tuple[int, ...], str]:
    identity = lambda value: (
        value.st_dev, value.st_ino, value.st_mode, value.st_uid,
        value.st_size, value.st_mtime_ns, value.st_ctime_ns,
    )
    try:
        before = path.lstat()
        target = os.readlink(path)
        after = path.lstat()
    except OSError as exc:
        raise RequestValidationError(f"staged symlink changed while being validated: {path}") from exc
    if not stat.S_ISLNK(before.st_mode) or identity(before) != identity(after):
        raise RequestValidationError(f"staged symlink changed while being validated: {path}")
    if before.st_uid != os.getuid():
        raise RequestValidationError(f"staged symlink has the wrong owner: {path}")
    return identity(before), target


def _validate_evidence(request: PrivateReleaseRequest) -> None:
    publication = _exact(
        _read_json(
            request.publication_receipt, "private publication receipt", mode=REQUEST_MODE,
            expected_sha256=request.private_publication_sha256,
        ),
        {
            "schema", "version", "repository", "ref", "prior_ref", "prior_commit",
            "commit", "tree", "merge_base", "fast_forward", "forced",
        },
        "private publication receipt",
    )
    if (
        publication["schema"] != PUBLICATION_SCHEMA
        or publication["version"] != SCHEMA_VERSION
        or publication["repository"] != _PRIVATE_REPOSITORY
        or type(publication["ref"]) is not str
        or _PRIVATE_RELEASE_REF_RE.fullmatch(publication["ref"]) is None
        or publication["prior_ref"] != publication["ref"]
        or publication["fast_forward"] is not True
        or publication["forced"] is not False
    ):
        raise RequestValidationError("private publication receipt does not prove the protected private destination")
    if (publication["commit"], publication["tree"]) != (
        request.candidate.commit, request.candidate.tree
    ):
        raise RequestValidationError("private publication receipt identity mismatch")
    prior_commit = _digest(publication["prior_commit"], "private publication prior_commit")
    if publication["merge_base"] != prior_commit or prior_commit == request.candidate.commit:
        raise RequestValidationError("private publication receipt does not prove normal history from its prior base")
    manifest = _exact(
        _read_json(
            request.artifact_manifest, "staged artifact manifest", mode=REQUEST_MODE,
            expected_sha256=request.artifact_manifest_sha256,
        ),
        {"schema", "version", "commit", "tree", "artifacts"},
        "staged artifact manifest",
    )
    if (
        manifest["schema"] != STAGE_SCHEMA
        or manifest["version"] != SCHEMA_VERSION
        or (manifest["commit"], manifest["tree"]) != (
            request.candidate.commit, request.candidate.tree
        )
    ):
        raise RequestValidationError("staged artifact manifest identity mismatch")
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, Mapping) or not artifacts:
        raise RequestValidationError("staged artifact manifest has no artifacts")
    _secure_directory(request.runtime, "staged runtime")
    artifact_bytes: dict[str, bytes] = {}
    for relative, expected_digest in artifacts.items():
        relative = _string(relative, "staged artifact path")
        rel_path = Path(relative)
        if (
            rel_path.is_absolute() or not rel_path.parts or "." in rel_path.parts
            or ".." in rel_path.parts or "\\" in relative
        ):
            raise RequestValidationError(f"unsafe staged artifact path: {relative}")
        artifact_path = request.runtime / relative
        if isinstance(expected_digest, Mapping):
            link = _exact(
                expected_digest, {"type", "target", "target_sha256"},
                f"symlink artifact {relative}",
            )
            if link["type"] != "symlink" or not artifact_path.is_symlink():
                raise RequestValidationError(f"staged symlink artifact mismatch: {relative}")
            link_snapshot = _staged_symlink_snapshot(artifact_path)
            target = link_snapshot[1]
            if target != _string(link["target"], f"symlink target {relative}"):
                raise RequestValidationError(f"staged symlink target mismatch: {relative}")
            try:
                resolved_target = artifact_path.resolve(strict=True)
            except OSError as exc:
                raise RequestValidationError(f"staged symlink target is unavailable: {relative}") from exc
            try:
                resolved_target.relative_to(request.runtime.resolve(strict=True))
            except ValueError:
                if relative != "venv/bin/python":
                    raise RequestValidationError(
                        f"staged symlink crosses an unapproved runtime boundary: {relative}"
                    )
            # Bind the policy-checked link to the resolved path before hashing it.
            if _staged_symlink_snapshot(artifact_path) != link_snapshot:
                raise RequestValidationError(f"staged symlink changed before hashing: {relative}")
            target_digest = _hash_staged_payload(
                resolved_target, f"resolved staged symlink target {relative}", boundary=True
            )
            if _staged_symlink_snapshot(artifact_path) != link_snapshot:
                raise RequestValidationError(f"staged symlink changed while being hashed: {relative}")
            if target_digest != _digest(
                link["target_sha256"], f"symlink target digest {relative}", sha256=True
            ):
                raise RequestValidationError(f"staged symlink target digest mismatch: {relative}")
        else:
            if relative == "private-release-identity.json":
                data = _read_owned_bytes(artifact_path, f"staged artifact {relative}")
                artifact_bytes[relative] = data
                actual_digest = sha256_bytes(data)
            else:
                actual_digest = _hash_staged_payload(artifact_path, f"staged artifact {relative}")
            if actual_digest != _digest(
                expected_digest, f"artifact digest {relative}", sha256=True
            ):
                raise RequestValidationError(f"staged artifact digest mismatch: {relative}")
    identity_digest = artifacts.get("private-release-identity.json")
    if identity_digest is None:
        raise RequestValidationError("staged artifact manifest omits private-release-identity.json")
    try:
        identity = json.loads(artifact_bytes["private-release-identity.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestValidationError("staged identity is not valid UTF-8 JSON") from exc
    if dict(identity) != {"commit": request.candidate.commit, "tree": request.candidate.tree}:
        raise RequestValidationError("staged runtime identity mismatch")
    actual_artifacts: set[str] = set()
    manifest_name = request.artifact_manifest.relative_to(request.runtime).as_posix()
    for root, directories, files in os.walk(request.runtime, followlinks=False):
        root_path = Path(root)
        _secure_directory(root_path, f"staged runtime directory {root_path}")
        traversable = []
        for name in directories:
            child = root_path / name
            if child.is_symlink():
                actual_artifacts.add(child.relative_to(request.runtime).as_posix())
            else:
                traversable.append(name)
        directories[:] = traversable
        for name in files:
            child = root_path / name
            relative = child.relative_to(request.runtime).as_posix()
            if relative == manifest_name:
                continue
            if not child.is_symlink() and not child.is_file():
                raise RequestValidationError(f"staged runtime contains a non-regular file: {child}")
            actual_artifacts.add(relative)
    if actual_artifacts != set(artifacts):
        missing = sorted(actual_artifacts - set(artifacts))
        stale = sorted(set(artifacts) - actual_artifacts)
        raise RequestValidationError(
            "staged artifact manifest is incomplete"
            + (f" (unmanifested: {', '.join(missing[:5])})" if missing else "")
            + (f" (missing: {', '.join(stale[:5])})" if stale else "")
        )


def _validate_activation_boundaries(request: PrivateReleaseRequest, policy: HostPolicy) -> None:
    wrappers = (
        (policy.gateway_wrapper, policy.gateway_wrapper_sha256, "gateway wrapper"),
        (policy.dispatcher_wrapper, policy.dispatcher_wrapper_sha256, "dispatcher wrapper"),
    )
    for path, expected, label in wrappers:
        if sha256_bytes(_read_boundary_bytes(path, label)) != expected:
            raise RequestValidationError(f"{label} digest does not match host policy")
    manifest = _read_json(
        request.artifact_manifest,
        "staged artifact manifest",
        mode=REQUEST_MODE,
        expected_sha256=request.artifact_manifest_sha256,
    )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping) or "venv/bin/python" not in artifacts:
        raise RequestValidationError("staged manifest does not bind the candidate interpreter")
    candidate_python = request.runtime / "venv/bin/python"
    if candidate_python.is_symlink():
        resolved = candidate_python.resolve(strict=True)
        try:
            resolved.relative_to(request.runtime.resolve(strict=True))
        except ValueError:
            if resolved != policy.python_path:
                raise RequestValidationError("candidate interpreter symlink escaped host policy")
            if sha256_bytes(_read_boundary_bytes(resolved, "host Python interpreter")) != policy.python_sha256:
                raise RequestValidationError("host Python interpreter digest mismatch")


def _validate_bootstrap_boundary(policy: HostPolicy) -> None:
    try:
        executing_python = Path(sys.executable).resolve(strict=True)
    except OSError as exc:
        raise RequestValidationError("executing Python interpreter identity is unavailable") from exc
    if executing_python != policy.python_path:
        raise RequestValidationError("executing Python interpreter escaped host policy")
    if sha256_bytes(_read_boundary_bytes(executing_python, "executing Python interpreter")) != policy.python_sha256:
        raise RequestValidationError("executing Python interpreter digest mismatch")


def _git_output(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        [_HOST_GIT, "-C", str(repository), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=30,
    )
    if completed.returncode:
        raise RequestValidationError("fixed private Git evidence is unavailable")
    return completed.stdout.strip()


def _validate_private_git_state(request: PrivateReleaseRequest) -> None:
    """Ground the publication receipt in the fixed host checkout's local Git state."""
    publication = _read_json(
        request.publication_receipt,
        "private publication receipt",
        mode=REQUEST_MODE,
        expected_sha256=request.private_publication_sha256,
    )
    repository = request.state_dir.parent / "hermes-agent"
    _secure_directory(repository, "fixed private Git checkout")
    remote_url = _git_output(repository, "remote", "get-url", "private-review")
    if remote_url not in {
        "git@github.com:stigrunar/hermes-agent-review.git",
        "https://github.com/stigrunar/hermes-agent-review.git",
    }:
        raise RequestValidationError("fixed private Git remote identity mismatch")
    release_ref = _string(publication.get("ref"), "private publication ref")
    if _PRIVATE_RELEASE_REF_RE.fullmatch(release_ref) is None:
        raise RequestValidationError("fixed private Git release ref is not allowed")
    tracking_ref = f"refs/remotes/private-review/{release_ref}"
    published = _git_output(repository, "rev-parse", "--verify", tracking_ref)
    if published != request.candidate.commit:
        raise RequestValidationError("fixed private Git ref does not resolve to the candidate")
    tree = _git_output(repository, "rev-parse", "--verify", f"{published}^{{tree}}")
    if tree != request.candidate.tree:
        raise RequestValidationError("fixed private Git candidate tree mismatch")
    prior = _digest(publication.get("prior_commit"), "private publication prior_commit")
    ancestry = subprocess.run(
        [_HOST_GIT, "-C", str(repository), "merge-base", "--is-ancestor", prior, published],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_GIT_ENV,
        timeout=30,
    )
    if ancestry.returncode != 0:
        raise RequestValidationError("fixed private Git update is not normal history")
    reflog = _git_output(repository, "reflog", "show", "-2", "--format=%H", tracking_ref).splitlines()
    if len(reflog) < 2 or reflog[:2] != [published, prior]:
        raise RequestValidationError("fixed private Git prior ref transition is not proven")


def parse_sealed_request(
    source: Path | str | bytes | Mapping[str, Any], *, state_dir: Path,
    validate_evidence: bool = True,
) -> PrivateReleaseRequest:
    state_dir = _safe_absolute(Path(state_dir), "private update state directory")
    if isinstance(source, (str, Path)):
        value = _read_json(Path(source), "private release request", mode=REQUEST_MODE)
    elif isinstance(source, bytes):
        try:
            value = json.loads(source.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestValidationError("private release request is not UTF-8 JSON") from exc
    else:
        value = source
    request = _exact(value, {
        "schema", "version", "sealed", "request_id", "project_id", "outcome_id",
        "execution_id", "correlation_id", "candidate", "private_publication_sha256",
        "artifact_manifest_sha256",
    }, "request")
    if (
        request["schema"] != REQUEST_SCHEMA
        or request["version"] != SCHEMA_VERSION
        or request["sealed"] is not True
    ):
        raise RequestValidationError("request schema/version/seal is unsupported")
    candidate_raw = _exact(request["candidate"], {"commit", "tree"}, "candidate")
    candidate = CandidateIdentity(
        _digest(candidate_raw["commit"], "candidate.commit"),
        _digest(candidate_raw["tree"], "candidate.tree"),
    )
    runtime = state_dir.parent / "runtime" / f"downstream-{candidate.commit[:10]}"
    parsed = PrivateReleaseRequest(
        _string(request["request_id"], "request_id", _ID_RE),
        _string(request["project_id"], "project_id", _ID_RE),
        _string(request["outcome_id"], "outcome_id", _ID_RE),
        _string(request["execution_id"], "execution_id", _ID_RE),
        _string(request["correlation_id"], "correlation_id", _ID_RE),
        candidate,
        _digest(
            request["private_publication_sha256"], "private_publication_sha256", sha256=True
        ),
        _digest(request["artifact_manifest_sha256"], "artifact_manifest_sha256", sha256=True),
        state_dir,
        runtime,
        state_dir / "receipts" / f"{candidate.commit}.private-publication.json",
        runtime / "private-release-manifest.json",
    )
    _secure_directory(parsed.receipt_dir, "private release receipt directory")
    if validate_evidence:
        _validate_evidence(parsed)
    return parsed


validate_sealed_request = parse_sealed_request


def _write_json_atomic(path: Path, value: Mapping[str, Any], *, mode: int = RECEIPT_MODE) -> None:
    _secure_directory(path.parent, "receipt parent")
    if path.exists() or path.is_symlink():
        _read_owned_bytes(path, "existing receipt", mode=mode)
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_replace_binding(path: Path, data: bytes, mode: int) -> None:
    _secure_directory(path.parent, "binding parent")
    if path.exists() or path.is_symlink():
        _read_owned_bytes(path, "binding")
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_rollback_bundle(
    request: PrivateReleaseRequest,
    policy: HostPolicy,
    snapshot: Mapping[str, tuple[bytes, int]],
) -> str:
    """Persist every pre-mutation binding byte before a journal can name it changed."""
    bindings = {}
    for target in policy.targets:
        data, mode = snapshot[target.unit]
        bindings[target.unit] = {
            "sha256": sha256_bytes(data),
            "mode": mode,
            "bytes_base64": base64.b64encode(data).decode("ascii"),
        }
    bundle = {
        "schema": BACKUP_SCHEMA,
        "version": SCHEMA_VERSION,
        "request_id": request.request_id,
        "candidate": {"commit": request.candidate.commit, "tree": request.candidate.tree},
        "bindings": bindings,
    }
    _write_json_atomic(request.backup_path, bundle)
    return sha256_file(request.backup_path)


def _read_rollback_bundle(
    request: PrivateReleaseRequest,
    policy: HostPolicy,
    expected_sha256: str,
) -> dict[str, tuple[bytes, int]]:
    bundle = _exact(
        _read_json(
            request.backup_path, "rollback bundle", mode=RECEIPT_MODE,
            expected_sha256=_digest(expected_sha256, "rollback bundle digest", sha256=True),
        ),
        {"schema", "version", "request_id", "candidate", "bindings"},
        "rollback bundle",
    )
    if (
        bundle["schema"] != BACKUP_SCHEMA
        or bundle["version"] != SCHEMA_VERSION
        or bundle["request_id"] != request.request_id
        or dict(bundle["candidate"]) != {
            "commit": request.candidate.commit, "tree": request.candidate.tree,
        }
    ):
        raise RequestValidationError("rollback bundle identity mismatch")
    raw_bindings = bundle["bindings"]
    expected_units = {target.unit for target in policy.targets}
    if not isinstance(raw_bindings, Mapping) or set(raw_bindings) != expected_units:
        raise RequestValidationError("rollback bundle target set mismatch")
    snapshot = {}
    for unit in expected_units:
        entry = _exact(
            raw_bindings[unit], {"sha256", "mode", "bytes_base64"},
            f"rollback bundle binding {unit}",
        )
        if type(entry["mode"]) is not int or entry["mode"] & ~0o777:
            raise RequestValidationError(f"rollback bundle mode is invalid: {unit}")
        try:
            data = base64.b64decode(_string(entry["bytes_base64"], "rollback bytes"), validate=True)
        except (ValueError, TypeError) as exc:
            raise RequestValidationError(f"rollback bundle bytes are invalid: {unit}") from exc
        if sha256_bytes(data) != _digest(entry["sha256"], "rollback binding digest", sha256=True):
            raise RequestValidationError(f"rollback bundle binding digest mismatch: {unit}")
        snapshot[unit] = (data, entry["mode"])
    return snapshot


@contextmanager
def _exclusive_lock(path: Path):
    _secure_directory(path.parent, "lock parent")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), REQUEST_MODE)
    try:
        os.fchmod(fd, REQUEST_MODE)
        _validate_stat(os.fstat(fd), path, "activation lock", mode=REQUEST_MODE, directory=False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise ConcurrentActivationError(
                    "another private immutable activation is running"
                ) from exc
            raise
        yield
    finally:
        os.close(fd)


@dataclass
class SupervisorOperations:
    prepare: Callable[
        [PrivateReleaseRequest, HostPolicy],
        tuple[dict[str, tuple[bytes, int]], Mapping[str, Any]],
    ]
    quiesce: Callable[[PrivateReleaseRequest, HostPolicy, str], Any]
    replace_binding: Callable[[FixedTarget, bytes, int], Any]
    restart: Callable[[Sequence[FixedTarget], str], Any]
    health_probe: Callable[[PrivateReleaseRequest, HostPolicy, str], Any]
    canary_probe: Callable[[PrivateReleaseRequest, HostPolicy], Any]
    release_quiescence: Callable[[PrivateReleaseRequest, HostPolicy], Any]
    verify_publication: Callable[[PrivateReleaseRequest], Any]
    resume: Callable[[PrivateReleaseRequest, HostPolicy, Mapping[str, Any]], Any] | None = None


def _probe_ok(value: Any, label: str) -> None:
    if value is True or isinstance(value, Mapping) and value.get("ok") is True:
        return
    raise PrivateReleaseError(f"{label} did not prove success")


def _receipt(request: PrivateReleaseRequest, policy: HostPolicy, status: str) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "version": SCHEMA_VERSION,
        "terminal": True,
        "status": status,
        "request_id": request.request_id,
        "project_id": request.project_id,
        "outcome_id": request.outcome_id,
        "execution_id": request.execution_id,
        "correlation_id": request.correlation_id,
        "candidate": {"commit": request.candidate.commit, "tree": request.candidate.tree},
        "targets": [target.unit for target in policy.targets],
        "mutated_units": [],
        "rollback": {"attempted": False, "restored_units": [], "errors": []},
        "default_profile_state_db": {
            "backup_integrity": "excluded", "restore": "forbidden", "verified": False,
        },
        "finished_at": time.time(),
    }


def _journal_payload(
    request: PrivateReleaseRequest, phase: str, changed: Sequence[FixedTarget], backup_sha256: str
) -> dict[str, Any]:
    return {
        "phase": phase,
        "changed": [target.unit for target in changed],
        "candidate": request.candidate.commit,
        "rollback_bundle_sha256": backup_sha256,
    }


def _recover_interrupted_release(
    request: PrivateReleaseRequest, policy: HostPolicy, operations: SupervisorOperations
) -> dict[str, Any]:
    journal = _exact(
        _read_json(request.journal_path, "activation journal", mode=RECEIPT_MODE),
        {"phase", "changed", "candidate", "rollback_bundle_sha256"},
        "activation journal",
    )
    phase = _string(journal["phase"], "activation journal phase")
    if phase not in {"quiescing", "binding", "committed_before_drain_release", "committed_after_drain_release"}:
        raise RequestValidationError("activation journal phase is unsupported")
    if journal["candidate"] != request.candidate.commit:
        raise RequestValidationError("activation journal candidate mismatch")
    changed_units = journal["changed"]
    if type(changed_units) is not list or any(type(unit) is not str for unit in changed_units):
        raise RequestValidationError("activation journal changed target list is invalid")
    targets = {target.unit: target for target in policy.targets}
    if len(set(changed_units)) != len(changed_units) or not set(changed_units) <= set(targets):
        raise RequestValidationError("activation journal changed target set is invalid")
    if phase.startswith("committed_"):
        receipt = _receipt(request, policy, "reconciliation_required")
        receipt["recovery"] = "committed_activation"
        receipt["mutated_units"] = changed_units
        try:
            snapshot = _read_rollback_bundle(
                request, policy, _string(journal["rollback_bundle_sha256"], "rollback bundle digest")
            )
            prestate = _read_json(request.prestate_path, "activation prestate", mode=RECEIPT_MODE)
            if operations.resume is not None:
                operations.resume(request, policy, prestate)
            _validate_evidence(request)
            _validate_activation_boundaries(request, policy)
            operations.verify_publication(request)
            for target in policy.targets:
                current, current_stat = _read_owned(target.binding, f"binding for {target.unit}")
                original, mode = snapshot[target.unit]
                if (
                    current != _render_binding(target, original, request.runtime, policy)
                    or stat.S_IMODE(current_stat.st_mode) != mode
                ):
                    raise PrivateReleaseError(f"committed binding mismatch: {target.unit}")
            # The durable commit records successful health and held canary proof.
            # Re-prove runtime identity; never re-run the drain-dependent canary
            # after a crash that may already have released those drains.
            _probe_ok(operations.health_probe(request, policy, "candidate"), "committed health")
            _probe_ok(operations.release_quiescence(request, policy), "committed drain release")
            receipt["status"] = "succeeded"
        except Exception as exc:
            receipt["error"] = f"committed candidate requires explicit reconciliation: {exc}"
        receipt["finished_at"] = time.time()
        _write_json_atomic(request.terminal_receipt_path, receipt)
        receipt["receipt_path"] = str(request.terminal_receipt_path)
        return receipt
    snapshot = _read_rollback_bundle(
        request, policy, _string(journal["rollback_bundle_sha256"], "rollback bundle digest")
    )
    prestate = _read_json(request.prestate_path, "activation prestate", mode=RECEIPT_MODE)
    if operations.resume is not None:
        operations.resume(request, policy, prestate)

    receipt = _receipt(request, policy, "rollback_failed")
    receipt["recovery"] = "interrupted_activation"
    restored, errors = [], []
    try:
        _probe_ok(operations.quiesce(request, policy, "rollback"), "recovery quiescence")
    except Exception as exc:
        errors.append(f"rollback quiescence: {exc}")
    for unit in reversed(changed_units):
        try:
            data, mode = snapshot[unit]
            operations.replace_binding(targets[unit], data, mode)
            restored.append(unit)
        except Exception as exc:
            errors.append(f"{unit}: {exc}")
    if not errors:
        try:
            if changed_units:
                operations.restart(policy.targets, "rollback")
                _probe_ok(operations.health_probe(request, policy, "rollback"), "rollback health")
            _probe_ok(operations.release_quiescence(request, policy), "rollback drain release")
            receipt["status"] = "rolled_back"
        except Exception as exc:
            errors.append(f"rollback runtime: {exc}")
    receipt["rollback"] = {
        "attempted": bool(changed_units),
        "restored_units": restored,
        "errors": errors,
    }
    if errors:
        receipt["error"] = "; ".join(errors)
    receipt["finished_at"] = time.time()
    _write_json_atomic(request.terminal_receipt_path, receipt)
    receipt["receipt_path"] = str(request.terminal_receipt_path)
    return receipt


def execute_private_release(
    request_source: Path | str | bytes | Mapping[str, Any] | PrivateReleaseRequest, *, policy: HostPolicy,
    operations: SupervisorOperations, state_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute one fixed-policy transaction with journal-before-byte rollback ownership."""
    state_dir = policy.default_root / "private-update" if state_dir is None else Path(state_dir)
    try:
        request = (
            request_source
            if isinstance(request_source, PrivateReleaseRequest)
            else parse_sealed_request(
                request_source, state_dir=state_dir, validate_evidence=False
            )
        )
    except Exception as exc:
        request_id = request_source.get("request_id", "invalid") if isinstance(request_source, Mapping) else "invalid"
        safe_id = request_id if isinstance(request_id, str) and _ID_RE.fullmatch(request_id) else "invalid"
        receipt = {
            "schema": RECEIPT_SCHEMA, "version": SCHEMA_VERSION, "terminal": True,
            "status": "rejected", "request_id": safe_id, "mutated_units": [],
            "error": str(exc), "finished_at": time.time(),
        }
        path = state_dir / "receipts" / f"{safe_id}.terminal.json"
        try:
            _write_json_atomic(path, receipt)
            receipt["receipt_path"] = str(path)
        except Exception as write_error:
            receipt["receipt_write_error"] = str(write_error)
        return receipt
    try:
        with _exclusive_lock(request.lock_path):
            if request.terminal_receipt_path.exists():
                existing = dict(_read_json(
                    request.terminal_receipt_path, "terminal receipt", mode=RECEIPT_MODE
                ))
                existing["admission"] = "replay_rejected"
                return existing
            if request.journal_path.exists():
                return _recover_interrupted_release(request, policy, operations)
            # Fresh activation validates mutable publication/runtime evidence
            # only after durable crash recovery has had first ownership.
            receipt = _receipt(request, policy, "failed")
            try:
                _validate_evidence(request)
                _validate_activation_boundaries(request, policy)
                operations.verify_publication(request)
            except Exception as exc:
                receipt["status"] = "rejected"
                receipt["error"] = str(exc)
                _write_json_atomic(request.terminal_receipt_path, receipt)
                receipt["receipt_path"] = str(request.terminal_receipt_path)
                return receipt
            snapshot: dict[str, tuple[bytes, int]] = {}
            changed: list[FixedTarget] = []
            committed = False
            quiesced = False
            try:
                snapshot, prestate = operations.prepare(request, policy)
                if all(
                    _render_binding(target, snapshot[target.unit][0], request.runtime, policy)
                    == snapshot[target.unit][0]
                    for target in policy.targets
                ):
                    _probe_ok(operations.health_probe(request, policy, "no_change"), "current candidate health")
                    receipt["status"] = "no_change"
                    receipt["finished_at"] = time.time()
                    _write_json_atomic(request.terminal_receipt_path, receipt)
                    receipt["receipt_path"] = str(request.terminal_receipt_path)
                    return receipt
                backup_sha256 = _write_rollback_bundle(request, policy, snapshot)
                _write_json_atomic(request.prestate_path, prestate)
                _write_json_atomic(
                    request.journal_path,
                    _journal_payload(request, "quiescing", [], backup_sha256),
                )
                _probe_ok(operations.quiesce(request, policy, "candidate"), "candidate quiescence")
                quiesced = True
                # Re-walk and re-hash the complete staged runtime at the last
                # boundary before the first persistent service binding byte.
                _validate_evidence(request)
                _validate_activation_boundaries(request, policy)
                operations.verify_publication(request)
                for target in policy.targets:
                    desired = _render_binding(target, snapshot[target.unit][0], request.runtime, policy)
                    if desired == snapshot[target.unit][0]:
                        continue
                    changed.append(target)
                    _write_json_atomic(
                        request.journal_path,
                        _journal_payload(request, "binding", changed, backup_sha256),
                    )
                    operations.replace_binding(target, desired, snapshot[target.unit][1])
                receipt["mutated_units"] = [target.unit for target in changed]
                operations.restart(policy.targets, "candidate")
                _probe_ok(
                    operations.health_probe(request, policy, "candidate"), "candidate health"
                )
                _probe_ok(operations.canary_probe(request, policy), "candidate canary")
                _write_json_atomic(
                    request.journal_path,
                    _journal_payload(
                        request, "committed_before_drain_release", changed, backup_sha256
                    ),
                )
                committed = True
                _probe_ok(operations.release_quiescence(request, policy), "drain release")
                _write_json_atomic(
                    request.journal_path,
                    _journal_payload(request, "committed_after_drain_release", changed, backup_sha256),
                )
                receipt["status"] = "succeeded"
            except Exception as failure:
                errors, restored = [], []
                if committed:
                    errors.append("candidate committed before drain release; automatic rollback forbidden")
                elif changed:
                    try:
                        _probe_ok(
                            operations.quiesce(request, policy, "rollback"), "rollback quiescence"
                        )
                    except Exception as exc:
                        errors.append(f"rollback quiescence: {exc}")
                    for target in reversed(changed):
                        try:
                            data, mode = snapshot[target.unit]
                            operations.replace_binding(target, data, mode)
                            restored.append(target.unit)
                        except Exception as exc:
                            errors.append(f"{target.unit}: {exc}")
                    if not errors:
                        try:
                            operations.restart(policy.targets, "rollback")
                            _probe_ok(
                                operations.health_probe(request, policy, "rollback"),
                                "rollback health",
                            )
                            _probe_ok(
                                operations.release_quiescence(request, policy),
                                "rollback drain release",
                            )
                        except Exception as exc:
                            errors.append(f"rollback runtime: {exc}")
                elif quiesced:
                    try:
                        _probe_ok(
                            operations.release_quiescence(request, policy),
                            "failed pre-mutation drain release",
                        )
                    except Exception as exc:
                        errors.append(f"drain release: {exc}")
                receipt["status"] = (
                    "reconciliation_required" if committed
                    else "rollback_failed" if errors else "rolled_back" if changed else "rejected"
                )
                receipt["error"] = str(failure)
                receipt["rollback"] = {
                    "attempted": bool(changed) and not committed,
                    "restored_units": restored, "errors": errors,
                }
            receipt["finished_at"] = time.time()
            _write_json_atomic(request.terminal_receipt_path, receipt)
            receipt["receipt_path"] = str(request.terminal_receipt_path)
            return receipt
    except ConcurrentActivationError as exc:
        receipt = _receipt(request, policy, "concurrent")
        receipt["error"] = str(exc)
        path = request.receipt_dir / f"{request.request_id}.concurrent.terminal.json"
        _write_json_atomic(path, receipt)
        receipt["receipt_path"] = str(path)
        return receipt


run_private_release = execute_private_release
run_transaction = execute_private_release


def _run(
    *args: object, timeout: int = 180, cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    proc = subprocess.run(
        [str(arg) for arg in args], capture_output=True, text=True, timeout=timeout,
        cwd=cwd, env=env,
    )
    if proc.returncode:
        command = " ".join(map(str, args[:4]))
        raise PrivateReleaseError(
            f"command failed rc={proc.returncode}: {command}: {proc.stderr[-500:]}"
        )
    return proc.stdout.strip()


def _show(unit: str) -> dict[str, str]:
    raw = _run(
        "systemctl", "--user", "show", unit, "--property=MainPID",
        "--property=ActiveState", "--property=SubState", "--property=WorkingDirectory",
        "--property=Environment", "--property=NRestarts", "--property=ControlGroup", timeout=60,
    )
    return {
        key: value for line in raw.splitlines() if "=" in line
        for key, value in [line.split("=", 1)]
    }


def _proc_start(pid: int) -> int:
    try:
        return int((Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError) as exc:
        raise PrivateReleaseError(f"cannot read process start identity for pid {pid}") from exc


def _proc_cmdline(pid: int) -> list[str]:
    raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    return [item.decode(errors="replace") for item in raw.split(b"\0") if item]


def _proc_env(pid: int) -> dict[str, str]:
    result = {}
    raw = (Path("/proc") / str(pid) / "environ").read_bytes()
    for item in raw.split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            result[key.decode(errors="replace")] = value.decode(errors="replace")
    return result


def _safe_env(value: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value[key] for key in sorted(value)
        if key in _SAFE_ENV or key.startswith("HERMES_SAFE_DISPATCH_")
    }


def _expected_command(target: FixedTarget, runtime: Path, policy: HostPolicy) -> list[str]:
    python = runtime / "venv/bin/python"
    if target.kind == "gateway":
        command = [str(python), "-m", "hermes_cli.main"]
        if target.unit != "hermes-gateway.service":
            profile = target.unit.removeprefix("hermes-gateway-").removesuffix(".service")
            command += ["--profile", profile]
        return command + ["gateway", "run"]
    if target.kind == "dashboard":
        return [
            str(python), "-m", "hermes_cli.main", "dashboard", "--host", "0.0.0.0",
            "--port", "9120", "--tui", "--insecure", "--no-open",
        ]
    return [str(python), str(policy.dispatcher_wrapper), "--interval", "60"]


def _render_binding(
    target: FixedTarget, original: bytes, runtime: Path, policy: HostPolicy
) -> bytes:
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrivateReleaseError(f"binding is not UTF-8: {target.unit}") from exc
    runtime_pattern = re.compile(
        re.escape(str(policy.runtime_root)) + r"/downstream-[A-Za-z0-9._-]+"
    )
    if runtime_pattern.search(text) is None and str(runtime) not in text:
        raise PrivateReleaseError(f"binding has no attributable runtime source: {target.unit}")
    lines = runtime_pattern.sub(str(runtime), text).splitlines()
    existing_path = None
    kept = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Environment=PATH="):
            existing_path = stripped.split("=", 2)[2]
        elif stripped.startswith('Environment="PATH='):
            existing_path = stripped[len('Environment="PATH='):].rstrip('"')
        owned = (
            stripped.startswith("ExecStart=")
            or stripped.startswith("Environment=PATH=")
            or stripped.startswith('Environment="PATH=')
            or stripped.startswith("Environment=VIRTUAL_ENV=")
            or stripped.startswith("Environment=PYTHONDONTWRITEBYTECODE=")
            or stripped.startswith("Environment=HERMES_PYTHON=")
        )
        if not owned:
            kept.append(line)
    service = next(
        (index for index, line in enumerate(kept) if line.strip() == "[Service]"), None
    )
    if service is None or existing_path is None:
        raise PrivateReleaseError(f"binding lacks fixed [Service]/PATH seam: {target.unit}")
    path_parts = [
        part for part in existing_path.split(":")
        if part and not part.startswith(f"{policy.runtime_root}/downstream-")
    ]
    command = _expected_command(target, runtime, policy)
    if target.kind == "gateway":
        command = [str(policy.gateway_wrapper), *command]
    additions = [
        "ExecStart=", "ExecStart=" + " ".join(command),
        f"Environment=PATH={runtime / 'venv/bin'}:{runtime / 'node_modules/.bin'}:{':'.join(path_parts)}",
        f"Environment=VIRTUAL_ENV={runtime / 'venv'}", "Environment=PYTHONDONTWRITEBYTECODE=1",
    ]
    if target.kind == "dispatcher":
        additions.append(f"Environment=HERMES_PYTHON={runtime / 'venv/bin/python'}")
    insert = next(
        (index for index in range(service + 1, len(kept)) if kept[index].startswith("[")),
        len(kept),
    )
    return ("\n".join(kept[:insert] + additions + kept[insert:]).rstrip() + "\n").encode()


class ProductionOperations:
    """Fixed Linux/systemd implementation; no request-derived operation exists here."""

    def __init__(self) -> None:
        self.baseline: dict[str, Any] = {}
        self.marker_principal = ""

    def prepare(self, request: PrivateReleaseRequest, policy: HostPolicy):
        own_cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
        if any(f"/{target.unit}" in own_cgroup for target in policy.targets):
            raise PrivateReleaseError("supervisor is inside an affected target cgroup")
        bus = Path(f"/run/user/{os.getuid()}/bus")
        if not bus.exists() or bus.lstat().st_uid != os.getuid():
            raise PrivateReleaseError("systemd user-manager identity is unavailable")
        snapshot, baseline = {}, {}
        for target in policy.targets:
            data, binding_stat = _read_owned(target.binding, f"binding for {target.unit}")
            mode = stat.S_IMODE(binding_stat.st_mode)
            status = _show(target.unit)
            if status.get("ActiveState") != "active" or status.get("SubState") != "running":
                raise PrivateReleaseError(f"{target.unit} is not active/running")
            pid = int(status.get("MainPID", "0"))
            if pid <= 0:
                raise PrivateReleaseError(f"{target.unit} has no live PID")
            environment = _proc_env(pid)
            baseline[target.unit] = {
                "pid": pid, "start_tick": _proc_start(pid),
                "cwd": str((Path("/proc") / str(pid) / "cwd").resolve()),
                "argv": _proc_cmdline(pid), "env": _safe_env(environment),
                "source": environment.get("HERMES_REPO"),
                "binding_sha256": sha256_bytes(data), "binding_mode": mode,
            }
            snapshot[target.unit] = (data, mode)
        self.baseline = baseline
        self.marker_principal = f"private-release:{request.request_id}"
        prestate = {
            "request_id": request.request_id,
            "candidate": {"commit": request.candidate.commit, "tree": request.candidate.tree},
            "baseline": baseline,
            "bindings": {
                unit: {"sha256": sha256_bytes(data), "mode": mode}
                for unit, (data, mode) in snapshot.items()
            },
            "no_database_restore": True,
            "default_profile_state_db": {
                "path": str(policy.default_root / "state.db"),
                "backup_integrity": "excluded", "verified": False,
            },
        }
        return snapshot, prestate

    def verify_publication(self, request: PrivateReleaseRequest) -> None:
        _validate_private_git_state(request)

    def resume(
        self, request: PrivateReleaseRequest, policy: HostPolicy, prestate: Mapping[str, Any]
    ) -> None:
        """Rehydrate process identity needed by crash-time rollback/commit recovery."""
        baseline = prestate.get("baseline")
        if not isinstance(baseline, Mapping) or set(baseline) != {target.unit for target in policy.targets}:
            raise RequestValidationError("activation prestate baseline is incomplete")
        self.baseline = dict(baseline)
        self.marker_principal = f"private-release:{request.request_id}"

    def _candidate_python(self, request: PrivateReleaseRequest) -> Path:
        return request.runtime / "venv/bin/python"

    def _drain(
        self, request: PrivateReleaseRequest, target: FixedTarget, *, clear: bool = False
    ) -> None:
        action = "clear_drain_request" if clear else "write_drain_request"
        if clear:
            kwargs = f"home=Path({str(target.profile_home)!r})"
        else:
            kwargs = (
                f"home=Path({str(target.profile_home)!r}), "
                f"principal={self.marker_principal!r}, suppress_notification=True"
            )
        code = (
            f"from pathlib import Path; from gateway.drain_control import {action}; "
            f"{action}({kwargs})"
        )
        env = dict(
            os.environ, HERMES_HOME=str(target.profile_home), HERMES_REPO=str(request.runtime),
            PYTHONPATH=str(request.runtime), PYTHONDONTWRITEBYTECODE="1",
        )
        _run(self._candidate_python(request), "-c", code, cwd=request.runtime, env=env, timeout=60)

    def _idle_sample(
        self, request: PrivateReleaseRequest, policy: HostPolicy, since: float
    ) -> dict[str, Any] | None:
        result = {}
        for target in policy.gateway_targets:
            status = _show(target.unit)
            pid = int(status.get("MainPID", "0"))
            state = _read_json(
                target.profile_home / "gateway_state.json", f"gateway state for {target.unit}"
            )
            marker = _read_json(
                target.profile_home / ".drain_request.json", f"drain marker for {target.unit}"
            )
            try:
                updated = datetime.fromisoformat(
                    str(state["updated_at"]).replace("Z", "+00:00")
                ).timestamp()
            except (KeyError, ValueError, TypeError):
                return None
            if (
                marker.get("principal") != self.marker_principal
                or state.get("pid") != pid
                or state.get("start_time") != _proc_start(pid)
                or state.get("gateway_state") != "draining"
                or updated < since
                or any(
                    type(state.get(field)) is not int or state[field] != 0
                    for field in ACTIVE_FIELDS
                )
            ):
                return None
            result[target.unit] = {
                "pid": pid, "start_tick": _proc_start(pid), "code_sha": state.get("code_sha"),
                "updated_at": state.get("updated_at"),
            }
        return result

    def _child_scopes_empty(self, status: Mapping[str, str]) -> bool:
        control = status.get("ControlGroup", "")
        if not control:
            raise PrivateReleaseError("service has no cgroup identity")
        root = Path("/sys/fs/cgroup") / control.lstrip("/")
        if not root.is_dir():
            raise PrivateReleaseError(f"service cgroup is unreadable: {root}")
        for directory, _subdirs, _files in os.walk(root):
            if Path(directory) == root:
                continue
            processes = Path(directory) / "cgroup.procs"
            if processes.is_file() and processes.read_text(encoding="utf-8").strip():
                return False
        return True

    def _verify_prestate(self, policy: HostPolicy) -> None:
        for target in policy.targets:
            status = _show(target.unit)
            pid = int(status.get("MainPID", "0"))
            before = self.baseline[target.unit]
            environment = _proc_env(pid)
            if (
                pid != before["pid"]
                or _proc_start(pid) != before["start_tick"]
                or _proc_cmdline(pid) != before["argv"]
                or str((Path("/proc") / str(pid) / "cwd").resolve()) != before["cwd"]
                or _safe_env(environment) != before["env"]
                or environment.get("HERMES_REPO") != before["source"]
            ):
                raise PrivateReleaseError(f"{target.unit} prestate identity drifted")
            if not self._child_scopes_empty(status):
                raise PrivateReleaseError(f"{target.unit} child scope is not empty")

    def quiesce(self, request: PrivateReleaseRequest, policy: HostPolicy, phase: str):
        since = time.time() - 1
        for target in policy.gateway_targets:
            marker = target.profile_home / ".drain_request.json"
            if marker.exists():
                body = _read_json(marker, f"existing drain marker for {target.unit}")
                if body.get("principal") != self.marker_principal:
                    raise PrivateReleaseError(f"foreign drain marker exists for {target.unit}")
            self._drain(request, target)
        deadline, previous = time.monotonic() + policy.drain_timeout_seconds, None
        while time.monotonic() < deadline:
            sample = self._idle_sample(request, policy, since)
            if (
                sample is not None and previous is not None
                and all(sample[unit]["updated_at"] != previous[unit]["updated_at"] for unit in sample)
            ):
                if phase == "candidate":
                    self._verify_prestate(policy)
                return {"ok": True, "phase": phase, "samples": 2, "last": sample}
            previous = sample
            time.sleep(3)
        raise PrivateReleaseError(
            f"{phase} quiescence did not receive two fresh PID/start-bound samples"
        )

    def replace_binding(self, target: FixedTarget, data: bytes, mode: int):
        atomic_replace_binding(target.binding, data, mode)

    def restart(self, targets: Sequence[FixedTarget], phase: str):
        _run("systemctl", "--user", "daemon-reload", timeout=60)
        errors = []
        for target in targets:
            try:
                _run("systemctl", "--user", "restart", target.unit, timeout=180)
            except Exception as exc:
                errors.append(f"{target.unit}: {exc}")
        if errors:
            raise PrivateReleaseError(f"{phase} restart failures: {'; '.join(errors)}")

    def _health_once(
        self, request: PrivateReleaseRequest, policy: HostPolicy, phase: str
    ) -> dict[str, Any]:
        services = {}
        for target in policy.targets:
            status = _show(target.unit)
            pid = int(status.get("MainPID", "0"))
            if status.get("ActiveState") != "active" or status.get("SubState") != "running" or pid <= 0:
                raise PrivateReleaseError(f"{target.unit} is not active/running")
            if phase in {"candidate", "no_change"}:
                old = self.baseline[target.unit]
                if phase == "candidate" and pid == old["pid"] and _proc_start(pid) == old["start_tick"]:
                    raise PrivateReleaseError(f"{target.unit} did not restart")
                if (Path("/proc") / str(pid) / "cwd").resolve() != request.runtime.resolve():
                    raise PrivateReleaseError(f"{target.unit} working directory escaped candidate")
                environment = _proc_env(pid)
                if (
                    environment.get("HERMES_REPO") != str(request.runtime)
                    or environment.get("PYTHONPATH") != str(request.runtime)
                ):
                    raise PrivateReleaseError(f"{target.unit} source environment escaped candidate")
                if _proc_cmdline(pid) != _expected_command(target, request.runtime, policy):
                    raise PrivateReleaseError(f"{target.unit} command identity mismatch")
                if target.kind == "gateway":
                    state = _read_json(
                        target.profile_home / "gateway_state.json",
                        f"gateway state for {target.unit}",
                    )
                    if (
                        state.get("pid") != pid
                        or state.get("start_time") != _proc_start(pid)
                        or state.get("code_sha") != request.candidate.commit
                    ):
                        raise PrivateReleaseError(
                            f"{target.unit} gateway source/PID/start identity mismatch"
                        )
                if not self._child_scopes_empty(status):
                    raise PrivateReleaseError(f"{target.unit} child scope is not empty")
            else:
                old = self.baseline[target.unit]
                environment = _proc_env(pid)
                if (
                    str((Path("/proc") / str(pid) / "cwd").resolve()) != old["cwd"]
                    or _proc_cmdline(pid) != old["argv"]
                    or environment.get("HERMES_REPO") != old["source"]
                ):
                    raise PrivateReleaseError(f"{target.unit} rollback source identity mismatch")
            services[target.unit] = {
                "pid": pid, "start_tick": _proc_start(pid),
                "NRestarts": status.get("NRestarts"),
            }
        if phase in {"candidate", "no_change"}:
            status_request = Request(policy.dashboard_status_url, headers={"Accept": "application/json"})
            with urlopen(status_request, timeout=10) as response:
                payload = json.loads(response.read().decode())
                if response.status != 200 or payload.get("gateway_running") is not True:
                    raise PrivateReleaseError("dashboard status health failed")
            with urlopen(policy.dashboard_root_url, timeout=10) as response:
                if response.status != 200 or b"<html" not in response.read(256 * 1024).lower():
                    raise PrivateReleaseError("dashboard root canary failed")
        return services

    def health_probe(self, request: PrivateReleaseRequest, policy: HostPolicy, phase: str):
        deadline, first = time.monotonic() + policy.health_timeout_seconds, None
        last_error = None
        while time.monotonic() < deadline:
            try:
                first = self._health_once(request, policy, phase)
                break
            except Exception as exc:
                last_error = exc
                time.sleep(3)
        if first is None:
            raise PrivateReleaseError(f"{phase} health deadline expired: {last_error}")
        if phase in {"candidate", "no_change"}:
            time.sleep(policy.sustained_seconds)
            second = self._health_once(request, policy, phase)
            if any(first[unit] != second[unit] for unit in first):
                raise PrivateReleaseError("candidate PID/start/restart counters were not sustained")
        return {"ok": True, "services": first}

    def canary_probe(self, request: PrivateReleaseRequest, policy: HostPolicy):
        env = dict(
            os.environ, HERMES_HOME=str(policy.default_root), HERMES_REPO=str(request.runtime),
            PYTHONPATH=str(request.runtime), HERMES_PYTHON=str(self._candidate_python(request)),
        )
        output = _run(
            self._candidate_python(request), policy.dispatcher_wrapper, "--once", "--dry-run",
            cwd=request.runtime, env=env, timeout=120,
        )
        if "gateway_drain_requested" not in output:
            raise PrivateReleaseError("held dispatcher dry-run did not acknowledge gateway drain")
        return {"ok": True, "dispatcher": "non-mutating dry-run"}

    def release_quiescence(self, request: PrivateReleaseRequest, policy: HostPolicy):
        errors = []
        for target in policy.gateway_targets:
            marker = target.profile_home / ".drain_request.json"
            if not marker.exists():
                continue
            try:
                body = _read_json(marker, f"drain marker for {target.unit}")
                if body.get("principal") != self.marker_principal:
                    raise PrivateReleaseError("foreign marker preserved")
                self._drain(request, target, clear=True)
                if marker.exists():
                    raise PrivateReleaseError("marker remained")
            except Exception as exc:
                errors.append(f"{target.unit}: {exc}")
        if errors:
            raise PrivateReleaseError("drain cleanup failed: " + "; ".join(errors))
        return {"ok": True}


def _installed_state_dir() -> Path:
    executable = Path(__file__)
    if str(executable).startswith("/proc/self/fd/"):
        target = os.readlink(executable)
        if target.endswith(" (deleted)"):
            raise RequestValidationError("installed helper was replaced after descriptor pinning")
        executable = Path(target)
    resolved = executable.resolve(strict=True)
    if resolved.name != "private_release_supervisor.py" or resolved.parent.name != "private-update":
        raise RequestValidationError("helper is not installed at the fixed private-update path")
    return resolved.parent


def _verify_installation(
    state_dir: Path, helper_digest: str, policy_digest: str
) -> HostPolicy:
    manifest = _exact(
        _read_json(state_dir / "manifest.json", "installation manifest", mode=REQUEST_MODE),
        {"schema", "version", "helper_sha256", "policy_sha256"},
        "installation manifest",
    )
    expected = {
        "schema": INSTALLATION_SCHEMA, "version": SCHEMA_VERSION,
        "helper_sha256": helper_digest, "policy_sha256": policy_digest,
    }
    if dict(manifest) != expected:
        raise RequestValidationError("installation manifest does not match adapter-pinned digests")
    if str(__file__).startswith("/proc/self/fd/"):
        with open(__file__, "rb") as handle:
            executing = handle.read(_MAX_FILE_BYTES + 1)
    else:
        executing = _read_owned_bytes(Path(__file__), "executing helper")
    if len(executing) > _MAX_FILE_BYTES or sha256_bytes(executing) != helper_digest:
        raise RequestValidationError("executing helper digest mismatch")
    policy_path = state_dir / "policy.json"
    policy_bytes = _read_owned_bytes(policy_path, "host policy", mode=REQUEST_MODE)
    if sha256_bytes(policy_bytes) != policy_digest:
        raise RequestValidationError("host policy digest mismatch")
    try:
        policy_value = json.loads(policy_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestValidationError("host policy is not valid UTF-8 JSON") from exc
    return parse_host_policy(policy_value, default_root=state_dir.parent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-helper-sha256", required=True)
    parser.add_argument("--expected-policy-sha256", required=True)
    parser.add_argument("--expected-request-sha256", required=True)
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-tree")
    parser.add_argument("--expected-request-id")
    args = parser.parse_args(argv)
    helper_digest = _digest(args.expected_helper_sha256, "expected helper digest", sha256=True)
    policy_digest = _digest(args.expected_policy_sha256, "expected policy digest", sha256=True)
    state_dir = _installed_state_dir()
    policy = _verify_installation(state_dir, helper_digest, policy_digest)
    _validate_bootstrap_boundary(policy)
    request_path = state_dir / "request.json"
    request_bytes = _read_owned_bytes(request_path, "private release request", mode=REQUEST_MODE)
    if sha256_bytes(request_bytes) != _digest(
        args.expected_request_sha256, "expected request digest", sha256=True
    ):
        raise RequestValidationError("private release request changed after adapter validation")
    request = parse_sealed_request(
        request_bytes, state_dir=state_dir, validate_evidence=False
    )
    expected = (
        (args.expected_commit, request.candidate.commit, "commit"),
        (args.expected_tree, request.candidate.tree, "tree"),
        (args.expected_request_id, request.request_id, "request id"),
    )
    for requested, actual, label in expected:
        if requested is not None and requested != actual:
            raise RequestValidationError(f"sealed request {label} does not match invocation")
    implementation = ProductionOperations()
    operations = SupervisorOperations(
        implementation.prepare, implementation.quiesce, implementation.replace_binding,
        implementation.restart, implementation.health_probe, implementation.canary_probe,
        implementation.release_quiescence, implementation.verify_publication,
        implementation.resume,
    )
    result = execute_private_release(request, policy=policy, operations=operations)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") in {"succeeded", "no_change"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
