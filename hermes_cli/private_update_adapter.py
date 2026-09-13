"""Production bridge from ``hermes update`` to the fixed private release helper."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
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
_HELPER_STDIN_BOOTSTRAP = (
    "import sys;"
    "source=sys.stdin.buffer.read();"
    "path=sys.argv[1];"
    "scope={'__name__':'__main__','__file__':path,'__package__':None};"
    "exec(compile(source,path,'exec'),scope)"
)


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


def _open_host_executable(path: Path, label: str) -> tuple[int, Path, str]:
    try:
        resolved = path.resolve(strict=True)
        if label == "systemd-run" and resolved not in {
            Path("/usr/bin/systemd-run"), Path("/bin/systemd-run")
        }:
            raise PrivateUpdateAdapterError("systemd-run escaped the fixed host path")
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise PrivateUpdateAdapterError(f"{label} is unavailable") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.getuid(), 65534}
            or info.st_mode & 0o002
            or (info.st_uid != os.getuid() and info.st_mode & 0o020)
            or not info.st_mode & 0o111
        ):
            raise PrivateUpdateAdapterError(f"{label} owner/mode validation failed")
        digest = hashlib.sha256()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        os.lseek(fd, 0, os.SEEK_SET)
        return fd, resolved, digest.hexdigest()
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
    """Validate the installation and run the helper in a host-owned user scope."""
    paths = resolve_private_update_paths() if paths is None else paths
    manifest = _installation_manifest(paths)
    read_private_update_file(
        paths.policy_path,
        kind="policy",
        expected_sha256=str(manifest["policy_sha256"]),
    )
    _request, request_digest = read_private_update_file(paths.request_path, kind="request")
    helper_fd, helper_digest = _open_pinned_helper(paths.helper_path, str(manifest["helper_sha256"]))
    boundary_fds: list[int] = []
    try:
        helper_chunks: list[bytes] = []
        while True:
            block = os.read(helper_fd, 1024 * 1024)
            if not block:
                break
            helper_chunks.append(block)
        helper_bytes = b"".join(helper_chunks)
        if hashlib.sha256(helper_bytes).hexdigest() != helper_digest:
            raise PrivateUpdateAdapterError("private release helper changed after validation")
        if sys.platform != "linux":
            raise PrivateUpdateAdapterError("private release supervision requires Linux systemd")
        systemd_run = shutil.which("systemd-run")
        if not systemd_run:
            raise PrivateUpdateAdapterError(
                "private release supervision requires the host systemd-run boundary"
            )
        systemd_fd, systemd_path, _systemd_digest = _open_host_executable(
            Path(systemd_run), "systemd-run"
        )
        python_fd, _python_path, _python_digest = _open_host_executable(
            Path(sys.executable), "Python interpreter"
        )
        boundary_fds.extend((systemd_fd, python_fd))
        python_descriptor = f"/proc/{os.getpid()}/fd/{python_fd}"
        systemd_descriptor = f"/proc/self/fd/{systemd_fd}"
        if not Path(python_descriptor).exists() or not Path(systemd_descriptor).exists():
            raise PrivateUpdateAdapterError("descriptor-bound external launch requires procfs")
        helper_command = [
            python_descriptor,
            "-c",
            _HELPER_STDIN_BOOTSTRAP,
            str(paths.helper_path),
            "--expected-helper-sha256",
            helper_digest,
            "--expected-policy-sha256",
            str(manifest["policy_sha256"]),
            "--expected-request-sha256",
            request_digest,
            *_identity_args(args),
        ]
        # The updater may have been launched by an affected gateway/dashboard
        # unit.  setsid does not escape that unit's cgroup; a transient user
        # scope does, and remains owned by the user manager if the caller dies.
        command = [
            str(systemd_path), "--user", "--scope", "--quiet", "--collect", "--",
            *helper_command,
        ]
        completed = subprocess.run(
            command,
            executable=systemd_descriptor,
            pass_fds=tuple(boundary_fds),
            input=helper_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    finally:
        for fd in boundary_fds:
            os.close(fd)
        os.close(helper_fd)
    if completed.stdout:
        output = completed.stdout.decode("utf-8", errors="replace") if isinstance(completed.stdout, bytes) else completed.stdout
        print(output.rstrip())
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
