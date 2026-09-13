"""Production bridge from ``hermes update`` to the fixed private release helper."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from gateway.private_update_request import (
    PrivateUpdateConfigError,
    PrivateUpdatePaths,
    private_immutable_external_enabled,
    read_private_update_file,
    resolve_private_update_paths,
)


INSTALLATION_SCHEMA = "hri.private_immutable_release_installation"
INSTALLATION_VERSION = 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class PrivateUpdateAdapterError(RuntimeError):
    """The fixed external adapter could not safely admit the request."""


def _exact_object(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise PrivateUpdateAdapterError(f"{label} has invalid fields")
    return value


def _installation_manifest(paths: PrivateUpdatePaths) -> Mapping[str, Any]:
    raw, _digest = read_private_update_file(paths.manifest_path, kind="manifest")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateUpdateAdapterError("private update installation manifest is not valid UTF-8 JSON") from exc
    manifest = _exact_object(
        value,
        {"schema", "version", "helper_sha256", "policy_sha256"},
        "private update installation manifest",
    )
    if manifest["schema"] != INSTALLATION_SCHEMA or manifest["version"] != INSTALLATION_VERSION:
        raise PrivateUpdateAdapterError("private update installation manifest schema/version is unsupported")
    for field in ("helper_sha256", "policy_sha256"):
        if type(manifest[field]) is not str or _SHA256_RE.fullmatch(manifest[field]) is None:
            raise PrivateUpdateAdapterError(f"private update installation manifest {field} is invalid")
    return manifest


def _open_pinned_helper(path: Path, expected_sha256: str) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PrivateUpdateAdapterError(f"private release helper is not readable: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PrivateUpdateAdapterError("private release helper descriptor is not a regular file")
        uid = getattr(os, "getuid", lambda: info.st_uid)()
        if info.st_uid != uid or info.st_mode & 0o022:
            raise PrivateUpdateAdapterError("private release helper owner/mode validation failed")
        digest = hashlib.sha256()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise PrivateUpdateAdapterError("private release helper digest does not match installation manifest")
        os.lseek(fd, 0, os.SEEK_SET)
        return fd, actual
    except BaseException:
        os.close(fd)
        raise


def _identity_args(args: Any) -> list[str]:
    result: list[str] = []
    for attr, flag in (
        ("candidate_commit", "--expected-commit"),
        ("candidate_tree", "--expected-tree"),
        ("release_request_id", "--expected-request-id"),
    ):
        value = getattr(args, attr, None)
        if value:
            result.extend((flag, str(value)))
    return result


def run_private_update_adapter(args: Any, *, paths: PrivateUpdatePaths | None = None) -> int:
    """Validate the host installation and synchronously run its pinned helper."""
    paths = resolve_private_update_paths() if paths is None else paths
    manifest = _installation_manifest(paths)
    read_private_update_file(
        paths.policy_path,
        kind="policy",
        expected_sha256=str(manifest["policy_sha256"]),
    )
    _request, request_digest = read_private_update_file(paths.request_path, kind="request")
    helper_fd, helper_digest = _open_pinned_helper(paths.helper_path, str(manifest["helper_sha256"]))
    try:
        proc_path = Path(f"/proc/self/fd/{helper_fd}")
        if not proc_path.exists():
            raise PrivateUpdateAdapterError("descriptor-pinned helper launch requires procfs")
        command = [
            sys.executable,
            str(proc_path),
            "--expected-helper-sha256",
            helper_digest,
            "--expected-policy-sha256",
            str(manifest["policy_sha256"]),
            "--expected-request-sha256",
            request_digest,
            *_identity_args(args),
        ]
        completed = subprocess.run(
            command,
            pass_fds=(helper_fd,),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    finally:
        os.close(helper_fd)
    if completed.stdout:
        print(completed.stdout.rstrip())
    return int(completed.returncode)


def dispatch_private_immutable_update(args: Any) -> bool:
    """Dispatch selected external mode and own its normal update receipt lifecycle."""
    try:
        enabled = private_immutable_external_enabled()
    except PrivateUpdateConfigError as exc:
        enabled = True
        failure: Exception | None = exc
    else:
        failure = None
    if not enabled:
        return False

    from hermes_cli.update_receipt import begin_update_receipt, finalize_update_receipt, record_step

    begin_update_receipt()
    print("⚕ Dispatching fixed private immutable release...")
    try:
        if failure is not None:
            raise failure
        code = run_private_update_adapter(args)
        if code != 0:
            raise PrivateUpdateAdapterError(f"private release supervisor exited with status {code}")
    except Exception as exc:
        detail = str(exc)
        record_step("private_immutable_external", False, detail)
        finalize_update_receipt("failed", stop_reason=detail)
        print(f"✗ Private immutable release refused: {detail}")
        raise SystemExit(1) from exc
    record_step("private_immutable_external", True, "fixed host supervisor completed")
    finalize_update_receipt("success", stop_reason="fixed private release supervisor completed")
    return True
