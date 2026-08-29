"""Fail-closed Codex host routing for Kanban implementation workers.

This module owns the complete edge protocol used by the dispatcher.  The
normal worker path never imports it unless native capacity is full and the
router is explicitly enabled.  Remote commands are fixed argv prefixes; task
prose and the worker context cross the boundary only as protocol stdin.

The protocol has four operations, all bound to ``KANBAN-CODEX-HOST-ROUTER-R1:v1``
and an attempt token:

``prepare``
    Atomically leases a unique remote workspace, installs and verifies the
    exact local bundle/base/tree/branch, probes git/Codex, and round-trips the
    immutable marker.
``run``
    Runs one fixed ``codex exec`` argv in that workspace with the complete
    worker context on stdin, then reports a bounded mutation/check receipt.
``collect``
    Returns a bounded binary diff and safe untracked files only after the run
    receipt proves mutation and the lease/token still match.
``cleanup``
    Removes the leased workspace only when the caller positively proves that
    recovery completed or no mutation occurred.

The helper is intentionally usable as a local fake-host process in tests:
``python -m hermes_cli.kanban_codex_host helper``.  Production transport is
the same helper command behind the configured SSH argv prefix.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

PROTOCOL = "KANBAN-CODEX-HOST-ROUTER-R1:v1"
HELPER_MARKER = "KANBAN_CODEX_HOST_HELPER_V1"
VALID_ROUTES = frozenset({"local_codex", "wsl_codex", "mac_codex", "defer"})
MUTATING_ACTIONS = frozenset({"write", "implement", "implementation"})
MAX_SELECTOR_OUTPUT = 64 * 1024
MAX_PROTOCOL_INPUT = 32 * 1024 * 1024
MAX_PROTOCOL_OUTPUT = 32 * 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_ARGV_ITEMS = 128
MAX_ARG_BYTES = 4096
MAX_CHECK_COMMANDS = 32
DEFAULT_SELECTOR_TIMEOUT = 5.0
DEFAULT_REMOTE_TIMEOUT = 900.0
DEFAULT_HEARTBEAT_SECONDS = 15.0
HELPER_COMMAND = (
    "python3", "-m", "hermes_cli.kanban_codex_host", "helper",
)
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,199}$")
_SAFE_ARG_RE = re.compile(r"^[^\x00\r\n]*$")
_SECRET_ENV_RE = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE)",
    re.IGNORECASE,
)
_HELPER_EXPECTED_TARGET: Optional[str] = None


def _kill_process_group(process: subprocess.Popen) -> None:
    """Terminate a bounded child and its descendants where supported."""
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def _safe_reason(value: Any, default: str = "selector") -> str:
    """Return a stable reason label, never selector/task prose."""
    if not isinstance(value, str):
        return default
    lowered = value.strip().lower()
    if lowered in {"local_codex", "wsl_codex", "mac_codex", "defer"}:
        return lowered
    # Selector output is an untrusted process boundary. Keep only a stable
    # token instead of copying arbitrary stderr/task text into events.
    if lowered.startswith("selector"):
        return "selector"
    return default


def _safe_subprocess_env() -> dict[str, str]:
    """Keep runtime/SSH identity while excluding credential-shaped env vars."""
    allowed: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in {"HOME", "PATH", "USER", "LOGNAME", "PYTHONPATH", "PYTHONHOME", "SSH_AUTH_SOCK", "SSH_AGENT_PID"} or key.startswith(("LANG", "LC_")):
            if key in {"SSH_AUTH_SOCK", "SSH_AGENT_PID"} or not _SECRET_ENV_RE.search(key):
                allowed[key] = value
    return allowed


def _bounded_process(
    argv: Sequence[str],
    *,
    input_bytes: Optional[bytes],
    timeout: float,
    output_limit: int,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[int, bytes]:
    """Run one fixed-argv process while retaining at most ``limit + 1`` bytes.

    ``subprocess.run(..., capture_output=True)`` is deliberately not used at
    any router boundary: a selector/SSH wrapper is untrusted and can otherwise
    make the dispatcher accumulate an unbounded stdout buffer.  A reader thread
    continuously drains the pipe (so the child cannot deadlock on a full pipe)
    while retaining only enough bytes to distinguish an exact limit from an
    oversized response.
    """
    if output_limit < 0:
        raise ValueError("output_limit must be non-negative")
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(env) if env is not None else None,
        close_fds=True,
        start_new_session=(os.name == "posix"),
    )
    retained = bytearray()

    def _drain() -> None:
        stream = process.stdout
        if stream is None:
            return
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            remaining = output_limit + 1 - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])

    reader = threading.Thread(target=_drain, name="kanban-router-output", daemon=True)
    reader.start()
    writer: Optional[threading.Thread] = None
    if input_bytes is not None:
        def _write() -> None:
            try:
                if process.stdin is not None:
                    process.stdin.write(input_bytes)
                    process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        writer = threading.Thread(target=_write, name="kanban-router-input", daemon=True)
        writer.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        process.wait()
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        reader.join(timeout=5)
        if writer is not None:
            writer.join(timeout=5)
        raise
    finally:
        reader.join(timeout=5)
        if writer is not None:
            writer.join(timeout=5)
    return int(process.returncode), bytes(retained)


def _bounded_capture_process(
    argv: Sequence[str],
    *,
    input_bytes: Optional[bytes],
    cwd: Path,
    timeout: float,
    output_limit: int,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[Optional[int], bytes, bytes, bool]:
    """Run the remote writer with independently bounded stdout/stderr.

    The helper must drain both streams even though neither is persisted.  This
    prevents a noisy Codex process from blocking before it can write its final
    protocol receipt, while retaining only ``limit + 1`` bytes for the bounded
    status decision.
    """
    if output_limit < 0:
        raise ValueError("output_limit must be non-negative")
    process = subprocess.Popen(
        list(argv),
        cwd=str(cwd),
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env) if env is not None else None,
        close_fds=True,
        start_new_session=(os.name == "posix"),
    )
    stdout_retained = bytearray()
    stderr_retained = bytearray()

    def _drain(stream: Any, retained: bytearray) -> None:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            remaining = output_limit + 1 - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])

    threads = []
    for stream, retained in (
        (process.stdout, stdout_retained), (process.stderr, stderr_retained)
    ):
        if stream is not None:
            thread = threading.Thread(
                target=_drain, args=(stream, retained),
                name="kanban-codex-output", daemon=True,
            )
            thread.start()
            threads.append(thread)
    writer: Optional[threading.Thread] = None
    if input_bytes is not None:
        def _write() -> None:
            try:
                if process.stdin is not None:
                    process.stdin.write(input_bytes)
                    process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        writer = threading.Thread(target=_write, name="kanban-codex-input", daemon=True)
        writer.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(process)
        process.wait()
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        for thread in threads:
            thread.join(timeout=5)
        if writer is not None:
            writer.join(timeout=5)
    return (
        None if timed_out else int(process.returncode),
        bytes(stdout_retained),
        bytes(stderr_retained),
        timed_out,
    )


class ProtocolError(ValueError):
    """Sanitized protocol/capability failure."""


class PreparationError(ProtocolError):
    """Pre-claim failure carrying whether a remote lease was positively cleaned."""

    def __init__(self, message: str, *, cleanup_proven: bool):
        super().__init__(message)
        self.cleanup_proven = bool(cleanup_proven)


class SupervisorPid(int):
    """Integer-compatible PID carrying the remote launch identity."""

    def __new__(cls, pid: int, *, verification_status: str = "remote-running"):
        value = int.__new__(cls, int(pid))
        value.launch_mode = "remote-codex-supervisor"
        value.verification_status = verification_status
        return value


@dataclass(frozen=True)
class RouteConfig:
    ssh_command: tuple[str, ...]
    ssh_target: str
    workspace_root: str
    codex_command: tuple[str, ...]
    timeout_seconds: float = DEFAULT_REMOTE_TIMEOUT
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    artifact_max_bytes: int = MAX_ARTIFACT_BYTES
    check_commands: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class HostRouterConfig:
    enabled: bool = False
    allowed_assignees: frozenset[str] = frozenset()
    selector_command: tuple[str, ...] = ()
    selector_timeout_seconds: float = DEFAULT_SELECTOR_TIMEOUT
    selector_max_output_bytes: int = MAX_SELECTOR_OUTPUT
    max_routes_per_tick: int = 0
    max_total_routes: int = 0
    routes: Mapping[str, RouteConfig] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, config: Optional[Mapping[str, Any]]) -> "HostRouterConfig":
        section: Any = (config or {}).get("kanban", {}) if isinstance(config, Mapping) else {}
        section = section.get("codex_host_router", {}) if isinstance(section, Mapping) else {}
        if section in (None, {}):
            return cls()
        if not isinstance(section, Mapping):
            raise ValueError("kanban.codex_host_router must be a mapping")
        enabled = section.get("enabled", False)
        if type(enabled) is not bool:
            raise ValueError("kanban.codex_host_router.enabled must be a boolean")
        raw_allow = section.get("allowed_assignees", section.get("allowed_worker_profiles", []))
        if not isinstance(raw_allow, (list, tuple)) or any(type(item) is not str or not item.strip() for item in raw_allow):
            raise ValueError("kanban.codex_host_router.allowed_assignees must be a list of non-empty strings")
        allowed = frozenset(item.strip() for item in raw_allow)
        selector = _argv(section.get("selector_command", ()), "selector_command")
        timeout = _positive_float(section.get("selector_timeout_seconds", DEFAULT_SELECTOR_TIMEOUT), "selector_timeout_seconds", 60.0)
        selector_max = _bounded_int(section.get("selector_max_output_bytes", MAX_SELECTOR_OUTPUT), "selector_max_output_bytes", 1, MAX_SELECTOR_OUTPUT)
        per_tick = _bounded_int(section.get("max_routes_per_tick", 0), "max_routes_per_tick", 0, 100000)
        total = _bounded_int(section.get("max_total_routes", 0), "max_total_routes", 0, 100000)
        routes_value = section.get("routes", {})
        if not isinstance(routes_value, Mapping):
            raise ValueError("kanban.codex_host_router.routes must be a mapping")
        routes: dict[str, RouteConfig] = {}
        for name, value in routes_value.items():
            if name not in {"wsl_codex", "mac_codex"} or not isinstance(value, Mapping):
                raise ValueError("kanban.codex_host_router.routes contains an invalid route")
            routes[name] = _parse_route_config(value, name)
        if enabled and (not allowed or not selector or per_tick <= 0 or total <= 0):
            raise ValueError("enabled Codex host routing requires allowlist, selector, and positive route limits")
        return cls(enabled, allowed, selector, timeout, selector_max, per_tick, total, routes)


@dataclass(frozen=True)
class PreparedRoute:
    """In-memory pre-claim receipt; remote path/token never enter the DB."""

    route: str
    task_id: str
    token: str
    remote_workspace: str
    remote_lease: str
    local_workspace: Path
    base: str
    tree: str
    branch: str
    marker: str
    selection_reason: str
    prepared_at_ms: int

    def receipt(self, *, run_id: Optional[int] = None, mutation_state: str = "prepared", artifact_status: str = "not_started", check_status: str = "not_started") -> dict[str, Any]:
        return {
            "contract": PROTOCOL,
            "task_id": self.task_id,
            "run_id": int(run_id) if run_id is not None else None,
            "route": self.route,
            "reason": _safe_reason(self.selection_reason),
            "base": self.base[:80],
            "tree": self.tree[:80],
            "branch": self.branch[:160],
            "workspace_marker": self.marker[:64],
            "mutation_state": mutation_state,
            "artifact_status": artifact_status,
            "check_status": check_status,
            "latency_ms": max(0, int(self.prepared_at_ms)),
        }

    def cleanup(self, cfg: "HostRouterConfig", *, allow: str) -> bool:
        """Release this exact remote lease only after a positive decision."""
        route_cfg = cfg.routes.get(self.route)
        if route_cfg is None:
            return False
        request = {
            "protocol": PROTOCOL,
            "marker": HELPER_MARKER,
            "op": "cleanup",
            "token": self.token,
            "task_id": self.task_id,
            "remote_workspace": self.remote_workspace,
            "workspace_root": route_cfg.workspace_root,
            "workspace_root_marker": _workspace_root_marker(route_cfg.workspace_root),
            "ssh_target": route_cfg.ssh_target,
            "base": self.base,
            "tree": self.tree,
            "branch": self.branch,
            "workspace_marker": self.marker,
            "allow": allow,
        }
        try:
            response = _remote_call(
                route_cfg, request, timeout=min(route_cfg.timeout_seconds, 30.0),
                output_limit=4096,
            )
        except ProtocolError:
            return False
        return response.get("cleaned") is True


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise ValueError(f"kanban.codex_host_router.{name} must be an integer in [{minimum},{maximum}]")
    return int(value)


def _positive_float(value: Any, name: str, maximum: float) -> float:
    if type(value) not in (int, float) or value <= 0 or value > maximum:
        raise ValueError(f"kanban.codex_host_router.{name} must be in (0,{maximum}]")
    return float(value)


def _argv(value: Any, name: str, *, required: bool = False) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"kanban.codex_host_router.{name} must be an argv list")
    if required and not value:
        raise ValueError(f"kanban.codex_host_router.{name} must not be empty")
    if len(value) > MAX_ARGV_ITEMS:
        raise ValueError(f"kanban.codex_host_router.{name} has too many argv items")
    result: list[str] = []
    for item in value:
        if (
            type(item) is not str
            or not item
            or len(item.encode("utf-8")) > MAX_ARG_BYTES
            or not _SAFE_ARG_RE.fullmatch(item)
        ):
            raise ValueError(f"kanban.codex_host_router.{name} contains an unsafe argv value")
        result.append(item)
    return tuple(result)


def _parse_route_config(value: Mapping[str, Any], name: str) -> RouteConfig:
    unknown = set(value) - {
        "ssh_command", "ssh_target", "workspace_root", "codex_command",
        "timeout_seconds", "heartbeat_seconds",
        "artifact_max_bytes", "check_commands",
    }
    if unknown:
        raise ValueError(f"route {name} contains unknown settings")
    ssh = _argv(value.get("ssh_command", ()), f"routes.{name}.ssh_command", required=True)
    target = value.get("ssh_target")
    root = value.get("workspace_root")
    if not _safe_target(target):
        raise ValueError(f"route {name}.ssh_target is unsafe")
    if type(root) is not str or not root or not Path(root).expanduser().is_absolute() or "\x00" in root or "\r" in root or "\n" in root:
        raise ValueError(f"route {name}.workspace_root is unsafe")
    root_path = Path(root).expanduser()
    if any(part in {".", ".."} for part in root_path.parts) or root_path == Path("/"):
        raise ValueError(f"route {name}.workspace_root is unsafe")
    codex = _argv(value.get("codex_command", ()), f"routes.{name}.codex_command", required=True)
    timeout = _positive_float(value.get("timeout_seconds", DEFAULT_REMOTE_TIMEOUT), f"routes.{name}.timeout_seconds", 3600.0)
    heartbeat = _positive_float(value.get("heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS), f"routes.{name}.heartbeat_seconds", 300.0)
    artifact_max = _bounded_int(value.get("artifact_max_bytes", MAX_ARTIFACT_BYTES), f"routes.{name}.artifact_max_bytes", 1, MAX_ARTIFACT_BYTES)
    checks_value = value.get("check_commands", ())
    if isinstance(checks_value, str) or not isinstance(checks_value, (list, tuple)):
        raise ValueError(f"route {name}.check_commands must be a list of argv lists")
    if len(checks_value) > MAX_CHECK_COMMANDS:
        raise ValueError(f"route {name}.check_commands has too many commands")
    checks = tuple(_argv(item, f"routes.{name}.check_commands", required=True) for item in checks_value)
    return RouteConfig(ssh, target, str(root_path), codex, timeout, heartbeat, artifact_max, checks)


def load_host_router_config(config: Optional[Mapping[str, Any]] = None) -> HostRouterConfig:
    if config is not None:
        return HostRouterConfig.from_mapping(config)
    try:
        from hermes_cli.config import load_config_readonly
        return HostRouterConfig.from_mapping(load_config_readonly() or {})
    except ValueError:
        raise
    except Exception as exc:
        return HostRouterConfig()


def task_is_eligible(task: Any, cfg: HostRouterConfig) -> bool:
    if not cfg.enabled or getattr(task, "assignee", None) not in cfg.allowed_assignees:
        return False
    if getattr(task, "status", None) != "ready" or getattr(task, "workspace_kind", None) != "worktree":
        return False
    if not getattr(task, "project_id", None):
        return False
    preflight = getattr(task, "execution_preflight", None)
    resolved = preflight.get("resolved") if isinstance(preflight, Mapping) else None
    return isinstance(resolved, Mapping) and resolved.get("action") in MUTATING_ACTIONS


def _selector_receipt(value: Mapping[str, Any], *, latency_ms: int) -> dict[str, Any]:
    route = value.get("route")
    return {
        "route": route if route in VALID_ROUTES else "defer",
        "reason": _safe_reason(value.get("reason")),
        "latency_ms": max(0, latency_ms),
    }


def select_route(cfg: HostRouterConfig, *, task_id: str, assignee: str) -> dict[str, Any]:
    if not cfg.enabled or not cfg.selector_command:
        return {"route": "defer", "reason": "disabled"}
    started = time.monotonic()
    payload = json.dumps({"task_id": str(task_id), "assignee": str(assignee)}, separators=(",", ":"))
    try:
        returncode, raw = _bounded_process(
            cfg.selector_command,
            input_bytes=payload.encode("utf-8"),
            timeout=cfg.selector_timeout_seconds,
            output_limit=cfg.selector_max_output_bytes,
            env=_safe_subprocess_env(),
        )
        latency = int((time.monotonic() - started) * 1000)
        if returncode != 0:
            return {"route": "defer", "reason": "selector_nonzero", "latency_ms": latency}
        if len(raw) > cfg.selector_max_output_bytes:
            return {"route": "defer", "reason": "selector_output_oversized", "latency_ms": latency}
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, Mapping) or value.get("route") not in VALID_ROUTES:
            return {"route": "defer", "reason": "selector_unknown_route", "latency_ms": latency}
        return _selector_receipt(value, latency_ms=latency)
    except subprocess.TimeoutExpired:
        return {"route": "defer", "reason": "selector_timeout", "latency_ms": int((time.monotonic() - started) * 1000)}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"route": "defer", "reason": "selector_failed", "latency_ms": int((time.monotonic() - started) * 1000)}


def _git(path: Path, *args: str, timeout: float = 30.0, stdout=subprocess.PIPE) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args], stdout=stdout, stderr=subprocess.DEVNULL, timeout=timeout, check=False)


def workspace_identity(path: Path) -> dict[str, str]:
    resolved = path.resolve(strict=True)
    def rev(*args: str) -> str:
        completed = _git(resolved, "rev-parse", *args, timeout=10)
        if completed.returncode != 0:
            raise ProtocolError("local git identity unavailable")
        return (completed.stdout or b"").decode("utf-8", "replace").strip() if isinstance(completed.stdout, bytes) else str(completed.stdout or "").strip()
    base = rev("HEAD")
    tree = rev("HEAD^{tree}")
    branch = rev("--abbrev-ref", "HEAD")
    if not _safe_branch(branch):
        raise ProtocolError("local branch identity is unsafe")
    marker = hashlib.sha256(f"{base}\0{tree}\0{branch}".encode()).hexdigest()[:32]
    return {"base": base, "tree": tree, "branch": branch, "workspace_marker": marker}


def _bundle(path: Path, maximum: int) -> bytes:
    with tempfile.TemporaryDirectory(prefix="hermes-codex-bundle-") as tmp:
        target = Path(tmp) / "repo.bundle"
        result = _git(path, "bundle", "create", str(target), "HEAD", timeout=120, stdout=subprocess.PIPE)
        if result.returncode != 0 or not target.is_file() or target.stat().st_size > maximum:
            raise ProtocolError("local git bundle unavailable or oversized")
        verify = _git(path, "bundle", "verify", str(target), timeout=30)
        if verify.returncode != 0:
            raise ProtocolError("local git bundle verification failed")
        return target.read_bytes()


def _safe_relative(path: str) -> bool:
    p = Path(path)
    return (
        bool(path)
        and path == path.strip()
        and not p.is_absolute()
        and not path.startswith(("/", "\\"))
        and "\\" not in path
        and "//" not in path
        and "\x00" not in path
        and all(part not in {"", ".", ".."} for part in p.parts)
    )


def _safe_branch(value: Any) -> bool:
    return (
        isinstance(value, str)
        and _BRANCH_RE.fullmatch(value) is not None
        and ".." not in value
        and "//" not in value
        and "@{" not in value
        and not value.startswith((".", "/"))
        and not value.endswith((".", "/"))
    )


def _workspace_root_marker(root: str) -> str:
    """Hash the configured canonical root without persisting the path."""
    resolved = str(Path(root).expanduser().resolve(strict=False))
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:32]


def _safe_target(value: Any) -> bool:
    return (
        type(value) is str
        and bool(value)
        and not value.startswith("-")
        and _SAFE_ARG_RE.fullmatch(value) is not None
        and not any(ch.isspace() for ch in value)
    )


def _remote_argv(route: RouteConfig) -> list[str]:
    """Build the only remote command this module is allowed to execute.

    The configured target is present both in the fixed SSH destination slot and
    in the helper's signed-by-protocol handshake.  A wrapper that silently
    redirects the SSH destination therefore cannot return a receipt bound to a
    different target without failing the helper's identity check.
    """
    return [
        *route.ssh_command,
        route.ssh_target,
        *HELPER_COMMAND,
        "--target",
        route.ssh_target,
    ]


def _remote_call(route: RouteConfig, request: Mapping[str, Any], *, timeout: float, output_limit: int) -> dict[str, Any]:
    argv = _remote_argv(route)
    encoded = json.dumps(dict(request), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PROTOCOL_INPUT:
        raise ProtocolError("remote protocol request oversized")
    try:
        returncode, raw = _bounded_process(
            argv,
            input_bytes=encoded,
            timeout=timeout,
            output_limit=min(output_limit, MAX_PROTOCOL_OUTPUT),
            env=_safe_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProtocolError("remote transport unavailable") from exc
    if len(raw) > min(output_limit, MAX_PROTOCOL_OUTPUT):
        raise ProtocolError("remote protocol response oversized")
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ProtocolError("remote protocol response malformed") from exc
    if not isinstance(response, Mapping) or response.get("protocol") != PROTOCOL or response.get("marker") != HELPER_MARKER:
        raise ProtocolError("remote protocol identity mismatch")
    if response.get("token") != request.get("token"):
        raise ProtocolError("remote protocol token mismatch")
    for identity_key in (
        "ssh_target", "workspace_root_marker", "task_id", "remote_workspace",
        "base", "tree", "branch", "workspace_marker",
    ):
        if identity_key in request and response.get(identity_key) != request.get(identity_key):
            raise ProtocolError("remote route identity mismatch")
    if returncode != 0 or response.get("ok") is not True:
        raise ProtocolError(str(response.get("reason") or "remote operation failed")[:120])
    return dict(response)


def _cleanup_prepare_attempt(route: RouteConfig, request: Mapping[str, Any]) -> bool:
    cleanup = {
        "protocol": PROTOCOL,
        "marker": HELPER_MARKER,
        "op": "cleanup",
        "token": request.get("token"),
        "task_id": request.get("task_id"),
        "remote_workspace": request.get("remote_workspace"),
        "workspace_root": route.workspace_root,
        "workspace_root_marker": _workspace_root_marker(route.workspace_root),
        "ssh_target": route.ssh_target,
        "base": request.get("base"),
        "tree": request.get("tree"),
        "branch": request.get("branch"),
        "workspace_marker": request.get("workspace_marker"),
        "allow": "no_mutation",
    }
    try:
        response = _remote_call(route, cleanup, timeout=min(route.timeout_seconds, 30.0), output_limit=4096)
    except (OSError, ProtocolError, subprocess.TimeoutExpired):
        return False
    return response.get("cleaned") is True


def _token() -> str:
    return hashlib.sha256(f"{os.getpid()}\0{time.monotonic_ns()}".encode()).hexdigest()[:32]


def prepare_route(task: Any, workspace: Path, cfg: HostRouterConfig, selection: Mapping[str, Any]) -> PreparedRoute:
    route_name = selection.get("route")
    route = cfg.routes.get(str(route_name))
    if route_name not in {"wsl_codex", "mac_codex"} or route is None:
        raise ProtocolError("remote route is not configured")
    if not _ID_RE.fullmatch(str(task.id)):
        raise ProtocolError("task identity is unsafe")
    identity = workspace_identity(workspace)
    bundle = _bundle(workspace, route.artifact_max_bytes)
    token = _token()
    remote_name = f"{task.id}-{token[:12]}"
    if not _ID_RE.fullmatch(remote_name):
        raise ProtocolError("remote workspace identity is unsafe")
    request = {
        "protocol": PROTOCOL,
        "marker": HELPER_MARKER,
        "op": "prepare",
        "token": token,
        "task_id": str(task.id),
        "remote_workspace": remote_name,
        "workspace_root": route.workspace_root,
        "workspace_root_marker": _workspace_root_marker(route.workspace_root),
        "ssh_target": route.ssh_target,
        "base": identity["base"],
        "tree": identity["tree"],
        "branch": identity["branch"],
        "workspace_marker": identity["workspace_marker"],
        "bundle_b64": base64.b64encode(bundle).decode("ascii"),
        "codex_command": list(route.codex_command),
        "check_commands": [list(command) for command in route.check_commands],
    }
    started = time.monotonic()
    try:
        response = _remote_call(route, request, timeout=min(route.timeout_seconds, 180.0), output_limit=route.artifact_max_bytes)
        if response.get("base") != identity["base"] or response.get("tree") != identity["tree"] or response.get("branch") != identity["branch"] or response.get("workspace_marker") != identity["workspace_marker"]:
            raise ProtocolError("remote identity round-trip mismatch")
        remote_workspace = response.get("remote_workspace")
        remote_lease = response.get("lease")
        if (
            response.get("ssh_target") != route.ssh_target
            or response.get("workspace_root_marker")
            != _workspace_root_marker(route.workspace_root)
        ):
            raise ProtocolError("remote route identity mismatch")
        if (
            not isinstance(remote_workspace, str)
            or not _safe_relative(remote_workspace)
            or remote_workspace != remote_name
            or not isinstance(remote_lease, str)
            or remote_lease != token
        ):
            raise ProtocolError("remote lease receipt malformed")
    except ProtocolError as exc:
        if _cleanup_prepare_attempt(route, request):
            raise PreparationError(str(exc), cleanup_proven=True) from exc
        raise PreparationError("remote preparation cleanup unproven", cleanup_proven=False) from exc
    return PreparedRoute(
        str(route_name), str(task.id), token, remote_workspace, remote_lease,
        workspace.resolve(), identity["base"], identity["tree"], identity["branch"],
        identity["workspace_marker"], _safe_reason(selection.get("reason")),
        int((time.monotonic() - started) * 1000),
    )


def _run_check_commands(path: Path, commands: Sequence[Sequence[str]], timeout: float) -> dict[str, Any]:
    statuses: list[int] = []
    started = time.monotonic()
    deadline = started + max(0.0, float(timeout))
    for command in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "failed", "count": len(statuses), "failed_index": len(statuses), "latency_ms": int((time.monotonic() - started) * 1000)}
        try:
            result = subprocess.run(
                list(command), cwd=str(path), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=remaining, check=False, env=_safe_subprocess_env(),
            )
            statuses.append(int(result.returncode))
            if result.returncode != 0:
                return {"status": "failed", "count": len(statuses), "failed_index": len(statuses) - 1, "latency_ms": int((time.monotonic() - started) * 1000)}
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "failed", "count": len(statuses), "failed_index": len(statuses), "latency_ms": int((time.monotonic() - started) * 1000)}
    return {"status": "passed", "count": len(statuses), "latency_ms": int((time.monotonic() - started) * 1000)}


def _helper_response(request: Mapping[str, Any], **fields: Any) -> dict[str, Any]:
    result = {"protocol": PROTOCOL, "marker": HELPER_MARKER, "token": request.get("token")}
    for identity_key in (
        "ssh_target", "workspace_root_marker", "task_id", "remote_workspace",
        "base", "tree", "branch", "workspace_marker",
    ):
        if identity_key in request:
            result[identity_key] = request.get(identity_key)
    result.update(fields)
    return result


def _receipt_int(value: Any, name: str, *, minimum: int = 0, maximum: int = MAX_PROTOCOL_OUTPUT) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise ProtocolError(f"remote {name} receipt malformed")
    return int(value)


def _helper_fail(request: Mapping[str, Any], reason: str, *, code: int = 2) -> int:
    print(json.dumps(_helper_response(request, ok=False, reason=reason[:120]), separators=(",", ":")))
    return code


def _helper_path(root: str, relative: str) -> Path:
    if type(root) is not str or type(relative) is not str or not _safe_relative(relative):
        raise ProtocolError("remote workspace path is unsafe")
    root_path = Path(root).expanduser().resolve(strict=False)
    candidate = (root_path / relative).resolve(strict=False)
    if candidate.parent != root_path and root_path not in candidate.parents:
        raise ProtocolError("remote workspace escapes configured root")
    return candidate


def _workspace_marker_matches(workspace: Path, marker: str) -> bool:
    marker_path = workspace / ".hermes-codex-marker"
    try:
        return (
            not marker_path.is_symlink()
            and marker_path.is_file()
            and marker_path.read_text(encoding="ascii") == marker
        )
    except (OSError, UnicodeError):
        return False


def _helper_require(request: Mapping[str, Any]) -> tuple[str, str, str, Path]:
    if request.get("protocol") != PROTOCOL or request.get("marker") != HELPER_MARKER:
        raise ProtocolError("protocol identity mismatch")
    token = request.get("token")
    task_id = request.get("task_id")
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token) or not isinstance(task_id, str) or not _ID_RE.fullmatch(task_id):
        raise ProtocolError("protocol binding is unsafe")
    root = request.get("workspace_root")
    relative = request.get("remote_workspace")
    target = request.get("ssh_target")
    if not _safe_target(target):
        raise ProtocolError("protocol target is unsafe")
    if _HELPER_EXPECTED_TARGET is None or target != _HELPER_EXPECTED_TARGET:
        raise ProtocolError("protocol target handshake mismatch")
    if request.get("workspace_root_marker") != _workspace_root_marker(root):
        raise ProtocolError("protocol workspace root mismatch")
    root_path = Path(root).expanduser()
    if root_path.exists() and (root_path.is_symlink() or not root_path.is_dir()):
        raise ProtocolError("protocol workspace root is unsafe")
    workspace = _helper_path(root, relative)
    lease_root = root_path.resolve(strict=False) / ".hermes-codex-leases"
    if lease_root.exists() and (lease_root.is_symlink() or not lease_root.is_dir()):
        raise ProtocolError("protocol lease root is unsafe")
    lease = lease_root / token
    return token, task_id, str(relative), lease


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".receipt-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _helper_prepare(request: Mapping[str, Any]) -> int:
    token, task_id, relative, lease = _helper_require(request)
    if (
        not isinstance(request.get("base"), str)
        or _OID_RE.fullmatch(request["base"]) is None
        or not isinstance(request.get("tree"), str)
        or _OID_RE.fullmatch(request["tree"]) is None
        or not _safe_branch(request.get("branch"))
        or not isinstance(request.get("workspace_marker"), str)
        or _TOKEN_RE.fullmatch(request["workspace_marker"]) is None
    ):
        raise ProtocolError("prepare identity is unsafe")
    if lease.exists():
        raise ProtocolError("remote lease already held")
    root = Path(request["workspace_root"]).expanduser().resolve(strict=False)
    if root == Path("/"):
        raise ProtocolError("remote workspace root is unsafe")
    lease_root = root / ".hermes-codex-leases"
    if lease_root.exists() and (lease_root.is_symlink() or not lease_root.is_dir()):
        raise ProtocolError("remote lease root is unsafe")
    lease_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if lease_root.is_symlink() or not lease_root.is_dir():
        raise ProtocolError("remote lease root is unsafe")
    workspace = _helper_path(str(root), relative)
    if workspace.exists():
        raise ProtocolError("remote workspace already exists")
    lease.mkdir(parents=True, exist_ok=False, mode=0o700)
    try:
        workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
        bundle = base64.b64decode(str(request.get("bundle_b64") or ""), validate=True)
        if not bundle or len(bundle) > MAX_ARTIFACT_BYTES:
            raise ProtocolError("remote bundle malformed or oversized")
        bundle_path = lease / "repo.bundle"
        bundle_path.write_bytes(bundle)
        codex = request.get("codex_command")
        if not isinstance(codex, list) or not codex:
            raise ProtocolError("remote Codex argv malformed")
        _argv(codex, "remote Codex command", required=True)
        if shutil.which(codex[0]) is None and not (os.path.isabs(codex[0]) and os.access(codex[0], os.X_OK)):
            raise ProtocolError("remote Codex runtime unavailable")
        if subprocess.run(["git", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False).returncode != 0:
            raise ProtocolError("remote git runtime unavailable")
        for command in (("git", "init", "--quiet", str(workspace)), ("git", "-C", str(workspace), "fetch", str(bundle_path), str(request["base"])), ("git", "-C", str(workspace), "checkout", "-q", "-B", str(request["branch"]), str(request["base"]))):
            if subprocess.run(list(command), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False).returncode != 0:
                raise ProtocolError("remote git bundle checkout failed")
        remote_base = subprocess.check_output(["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
        remote_tree = subprocess.check_output(["git", "-C", str(workspace), "rev-parse", "HEAD^{tree}"], text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
        remote_branch = subprocess.check_output(["git", "-C", str(workspace), "symbolic-ref", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
        if remote_base != request.get("base") or remote_tree != request.get("tree") or remote_branch != request.get("branch"):
            raise ProtocolError("remote git identity mismatch")
        marker = workspace / ".hermes-codex-marker"
        marker.write_text(str(request["workspace_marker"]), encoding="ascii")
        if marker.read_text(encoding="ascii") != request["workspace_marker"]:
            raise ProtocolError("remote marker round-trip failed")
        checks = request.get("check_commands")
        if not isinstance(checks, list) or any(
            not isinstance(command, list) for command in checks
        ):
            raise ProtocolError("remote check argv malformed")
        normalized_checks = [
            list(_argv(command, "remote check command", required=True))
            for command in checks
        ]
        _write_json_atomic(
            lease / "state.json",
            {
                "protocol": PROTOCOL,
                "marker": HELPER_MARKER,
                "token": token,
                "task_id": task_id,
                "workspace": relative,
                "base": request["base"],
                "tree": request["tree"],
                "branch": request["branch"],
                "workspace_marker": request["workspace_marker"],
                "codex_command": list(codex),
                "check_commands": normalized_checks,
                "mutation_state": "no_mutation",
                "run_complete": False,
            },
        )
        print(json.dumps(_helper_response(request, ok=True, op="prepare", lease=token, remote_workspace=relative, base=remote_base, tree=remote_tree, branch=remote_branch, workspace_marker=request["workspace_marker"]), separators=(",", ":")))
        return 0
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(lease, ignore_errors=True)
        raise


def _helper_state(request: Mapping[str, Any]) -> tuple[str, str, str, Path, dict[str, Any]]:
    token, task_id, relative, lease = _helper_require(request)
    state_path = lease / "state.json"
    workspace = _helper_path(str(request["workspace_root"]), relative)
    if (
        lease.is_symlink() or not lease.is_dir()
        or state_path.is_symlink() or not state_path.is_file()
        or workspace.is_symlink() or not workspace.is_dir()
    ):
        raise ProtocolError("remote lease is not held")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProtocolError("remote lease receipt malformed") from exc
    if (
        not isinstance(state, dict)
        or state.get("protocol") != PROTOCOL
        or state.get("marker") != HELPER_MARKER
        or state.get("token") != token
        or state.get("task_id") != task_id
        or state.get("workspace") != relative
    ):
        raise ProtocolError("remote lease binding mismatch")
    return token, task_id, relative, workspace, state


def _mutation_state(workspace: Path) -> str:
    try:
        returncode, output = _bounded_process(
            ["git", "-C", str(workspace), "status", "--porcelain=v1", "--untracked-files=all"],
            input_bytes=None, timeout=30, output_limit=256 * 1024,
            env=_safe_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return "ambiguous"
    if returncode != 0 or len(output) > 256 * 1024:
        return "ambiguous"
    lines = output.splitlines()
    lines = [line for line in lines if not line.decode("utf-8", "replace").endswith(".hermes-codex-marker")]
    return "mutated" if lines else "no_mutation"


def _helper_run(request: Mapping[str, Any]) -> int:
    token, task_id, relative, workspace, state = _helper_state(request)
    if state.get("mutation_state") != "no_mutation" or state.get("run_complete") is not False:
        raise ProtocolError("remote run already started")
    run_lock = Path(request["workspace_root"]).expanduser().resolve(strict=False) / ".hermes-codex-leases" / token / "run.lock"
    try:
        lock_fd = os.open(
            run_lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
        )
        os.close(lock_fd)
    except FileExistsError as exc:
        raise ProtocolError("remote run already started") from exc
    except OSError as exc:
        raise ProtocolError("remote run lock unavailable") from exc
    if (
        request.get("base") != state.get("base")
        or request.get("tree") != state.get("tree")
        or request.get("branch") != state.get("branch")
        or request.get("workspace_marker") != state.get("workspace_marker")
    ):
        raise ProtocolError("run identity mismatch")
    if not _workspace_marker_matches(workspace, str(state.get("workspace_marker") or "")):
        raise ProtocolError("run marker mismatch")
    codex = request.get("codex_command")
    context = request.get("context_b64")
    if not isinstance(codex, list) or not codex or not isinstance(context, str):
        raise ProtocolError("run payload malformed")
    _argv(codex, "remote Codex command", required=True)
    if state.get("codex_command") != codex:
        raise ProtocolError("run Codex command does not match preparation")
    checks = request.get("check_commands")
    if not isinstance(checks, list) or any(not isinstance(command, list) for command in checks):
        raise ProtocolError("remote check argv malformed")
    normalized_checks = [
        list(_argv(command, "remote check command", required=True))
        for command in checks
    ]
    if state.get("check_commands") != normalized_checks:
        raise ProtocolError("run checks do not match preparation")
    timeout_value = request.get("timeout_seconds")
    if type(timeout_value) not in (int, float) or timeout_value <= 0 or timeout_value > 3600:
        raise ProtocolError("run timeout is unsafe")
    try:
        context_bytes = base64.b64decode(context, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ProtocolError("worker context malformed") from exc
    if len(context_bytes) > MAX_PROTOCOL_INPUT:
        raise ProtocolError("worker context oversized")
    started = time.monotonic()
    state["mutation_state"] = "running"
    _write_json_atomic(
        Path(request["workspace_root"]).expanduser().resolve(strict=False)
        / ".hermes-codex-leases" / token / "state.json",
        state,
    )
    try:
        return_code, stdout, stderr, timed_out = _bounded_capture_process(
            codex,
            input_bytes=context_bytes,
            cwd=workspace,
            timeout=float(timeout_value),
            output_limit=MAX_PROTOCOL_OUTPUT,
            env=_safe_subprocess_env(),
        )
    except OSError as exc:
        raise ProtocolError("remote Codex could not start") from exc
    output_status = "bounded" if len(stdout) <= MAX_PROTOCOL_OUTPUT and len(stderr) <= MAX_PROTOCOL_OUTPUT else "oversized"
    if output_status == "oversized":
        # The bytes themselves are intentionally discarded from the receipt;
        # an oversized stream is a capability failure, never a success proof.
        return_code = None
    if timed_out:
        # A timeout is deliberately distinct from a normal non-zero Codex
        # result, but both are non-success.  Keep the receipt scalar-only; the
        # supervisor decides whether the mutation state is safe to clean.
        return_code = None
    checks_result = _run_check_commands(workspace, normalized_checks, timeout=min(float(timeout_value), 300.0))
    # Checks are part of the remote writer's receipt.  Classify mutation after
    # them as well, so an unexpected check-side write can never be reported as
    # a clean no-mutation lease.
    mutation = (
        _mutation_state(workspace)
        if _workspace_marker_matches(workspace, str(state.get("workspace_marker") or ""))
        else "ambiguous"
    )
    state["mutation_state"] = mutation
    state["run_complete"] = True
    state["codex_returncode"] = return_code
    state["check_status"] = checks_result["status"]
    _write_json_atomic(Path(request["workspace_root"]).expanduser().resolve(strict=False) / ".hermes-codex-leases" / token / "state.json", state)
    print(json.dumps(_helper_response(request, ok=True, op="run", task_id=task_id, remote_workspace=relative, mutation_state=mutation, codex_returncode=return_code, output_status=output_status, check_status=checks_result["status"], check_count=checks_result.get("count", 0), check_latency_ms=checks_result.get("latency_ms", 0), elapsed_ms=int((time.monotonic() - started) * 1000), stdout_bytes=min(len(stdout or b""), MAX_PROTOCOL_OUTPUT), stderr_bytes=min(len(stderr or b""), MAX_PROTOCOL_OUTPUT)), separators=(",", ":")))
    return 0


def _artifact(workspace: Path, base: str, maximum: int) -> dict[str, Any]:
    try:
        diff_returncode, diff_output = _bounded_process(
            ["git", "-C", str(workspace), "diff", "--binary", base],
            input_bytes=None, timeout=120, output_limit=maximum,
            env=_safe_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProtocolError("remote diff unavailable") from exc
    if diff_returncode != 0:
        raise ProtocolError("remote diff unavailable")
    if len(diff_output) > maximum:
        raise ProtocolError("remote artifact oversized")
    total = len(diff_output)
    files: list[dict[str, Any]] = []
    try:
        result_returncode, untracked_output = _bounded_process(
            ["git", "-C", str(workspace), "ls-files", "--others", "--exclude-standard", "-z"],
            input_bytes=None, timeout=30, output_limit=maximum,
            env=_safe_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProtocolError("remote status unavailable") from exc
    if result_returncode != 0 or len(untracked_output) > maximum:
        raise ProtocolError("remote status unavailable")
    for raw_path in untracked_output.split(b"\0"):
        if not raw_path:
            continue
        try:
            relative = raw_path.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ProtocolError("remote untracked path is not valid text") from exc
        if relative == ".hermes-codex-marker":
            continue
        if not _safe_relative(relative):
            raise ProtocolError("remote untracked path is unsafe")
        target = (workspace / relative).resolve(strict=False)
        parent = workspace.resolve()
        for part in Path(relative).parts[:-1]:
            parent = parent / part
            if parent.is_symlink():
                raise ProtocolError("remote untracked path is unsafe")
        if workspace.resolve() not in target.parents or not target.is_file() or target.is_symlink() or not stat.S_ISREG(target.stat().st_mode):
            raise ProtocolError("remote untracked path is unsafe")
        data = target.read_bytes()
        total += len(data)
        if total > maximum:
            raise ProtocolError("remote artifact oversized")
        files.append({"path": relative, "mode": stat.S_IMODE(target.stat().st_mode), "data_b64": base64.b64encode(data).decode("ascii")})
    if total > maximum:
        raise ProtocolError("remote artifact oversized")
    return {"diff_b64": base64.b64encode(diff_output).decode("ascii"), "untracked": files, "bytes": total}


def _helper_collect(request: Mapping[str, Any]) -> int:
    token, task_id, relative, workspace, state = _helper_state(request)
    if (
        request.get("base") != state.get("base")
        or request.get("tree") != state.get("tree")
        or request.get("branch") != state.get("branch")
        or request.get("workspace_marker") != state.get("workspace_marker")
    ):
        raise ProtocolError("collect identity mismatch")
    if not _workspace_marker_matches(workspace, str(state.get("workspace_marker") or "")):
        raise ProtocolError("collect marker mismatch")
    if state.get("mutation_state") != "mutated":
        raise ProtocolError("remote mutation is not positively proven")
    artifact = _artifact(workspace, str(state["base"]), min(int(request.get("artifact_max_bytes") or MAX_ARTIFACT_BYTES), MAX_ARTIFACT_BYTES))
    print(json.dumps(_helper_response(request, ok=True, op="collect", task_id=task_id, remote_workspace=relative, artifact=artifact), separators=(",", ":")))
    return 0


def _helper_cleanup(request: Mapping[str, Any]) -> int:
    allow = request.get("allow")
    if allow not in {"no_mutation", "recovered"}:
        raise ProtocolError("cleanup requires a positive mutation decision")
    token, task_id, relative, lease = _helper_require(request)
    workspace = _helper_path(str(request["workspace_root"]), relative)
    # A prepare operation may have created the lease and then removed it while
    # unwinding a capability failure before returning its error receipt.  An
    # exact, positively absent token/workspace is therefore a successful
    # idempotent no-mutation cleanup; a half-present pair remains ambiguous.
    if not lease.exists() and not workspace.exists():
        if allow == "no_mutation":
            print(json.dumps(_helper_response(request, ok=True, op="cleanup", task_id=task_id, cleaned=True, already_clean=True), separators=(",", ":")))
            return 0
        raise ProtocolError("remote lease is not held")
    token, task_id, relative, workspace, state = _helper_state(request)
    run_lock = lease / "run.lock"
    if (
        allow == "no_mutation"
        and run_lock.exists()
        and not (
            state.get("run_complete") is True
            and state.get("mutation_state") == "no_mutation"
        )
    ):
        # The run lock is created before the remote writer starts.  A completed
        # no-mutation receipt is the sole safe exception; every in-progress,
        # stale, or malformed state remains fenced against cleanup.
        raise ProtocolError("remote run has started")
    if any(
        request.get(key) != state.get(key)
        for key in ("base", "tree", "branch", "workspace_marker")
    ):
        raise ProtocolError("cleanup identity mismatch")
    marker = workspace / ".hermes-codex-marker"
    try:
        if marker.is_symlink() or marker.read_text(encoding="ascii") != state["workspace_marker"]:
            raise ProtocolError("cleanup marker mismatch")
    except (OSError, UnicodeError) as exc:
        raise ProtocolError("cleanup marker unavailable") from exc
    if allow == "no_mutation" and state.get("mutation_state") != "no_mutation":
        raise ProtocolError("cleanup mutation state is not no_mutation")
    if allow == "recovered" and state.get("mutation_state") != "mutated":
        raise ProtocolError("cleanup recovery state is not mutated")
    if state.get("run_complete") is not True and not (
        allow == "no_mutation" and state.get("mutation_state") == "no_mutation"
    ):
        raise ProtocolError("cleanup run is not complete")
    lease = Path(request["workspace_root"]).expanduser().resolve(strict=False) / ".hermes-codex-leases" / token
    shutil.rmtree(workspace, ignore_errors=False)
    shutil.rmtree(lease, ignore_errors=False)
    print(json.dumps(_helper_response(request, ok=True, op="cleanup", task_id=task_id, cleaned=True), separators=(",", ":")))
    return 0


def helper_main() -> int:
    global _HELPER_EXPECTED_TARGET
    helper_parser = argparse.ArgumentParser(add_help=False)
    helper_parser.add_argument("--target", required=True)
    try:
        helper_args, unknown = helper_parser.parse_known_args(sys.argv[2:])
    except SystemExit:
        return _helper_fail({}, "helper target handshake malformed")
    if unknown or not _safe_target(helper_args.target):
        return _helper_fail({}, "helper target handshake malformed")
    _HELPER_EXPECTED_TARGET = helper_args.target
    raw = sys.stdin.buffer.read(MAX_PROTOCOL_INPUT + 1)
    if len(raw) > MAX_PROTOCOL_INPUT:
        _HELPER_EXPECTED_TARGET = None
        return _helper_fail({}, "protocol input oversized")
    try:
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, Mapping):
            raise ProtocolError("protocol input malformed")
        op = request.get("op")
        if op == "prepare":
            return _helper_prepare(request)
        if op == "run":
            return _helper_run(request)
        if op == "collect":
            return _helper_collect(request)
        if op == "cleanup":
            return _helper_cleanup(request)
        raise ProtocolError("unknown helper operation")
    except ProtocolError as exc:
        return _helper_fail(request if isinstance(locals().get("request"), Mapping) else {}, str(exc))
    except Exception:
        return _helper_fail(request if isinstance(locals().get("request"), Mapping) else {}, "helper operation failed")
    finally:
        _HELPER_EXPECTED_TARGET = None


def prepared_receipt(*, task_id: str, run_id: Any, selection: Mapping[str, Any], identity: Mapping[str, Any], mutation_state: str = "prepared") -> dict[str, Any]:
    """Compatibility receipt constructor retained for edge callers/tests."""
    return {
        "contract": PROTOCOL, "task_id": str(task_id),
        "run_id": int(run_id) if str(run_id).isdigit() else None,
        "route": selection.get("route", "defer"), "reason": _safe_reason(selection.get("reason")),
        "base": str(identity.get("base", ""))[:80], "tree": str(identity.get("tree", ""))[:80],
        "branch": str(identity.get("branch", ""))[:160],
        "workspace_marker": str(identity.get("workspace_marker", ""))[:64],
        "mutation_state": mutation_state, "artifact_status": "not_started", "check_status": "not_started", "latency_ms": 0,
    }


def _route_config_payload(route: RouteConfig) -> dict[str, Any]:
    return {
        "ssh_command": list(route.ssh_command),
        "ssh_target": route.ssh_target,
        "workspace_root": route.workspace_root,
        "codex_command": list(route.codex_command),
        "timeout_seconds": route.timeout_seconds,
        "heartbeat_seconds": route.heartbeat_seconds,
        "artifact_max_bytes": route.artifact_max_bytes,
        "check_commands": [list(command) for command in route.check_commands],
    }


def launch_supervisor(
    task: Any,
    workspace: Path,
    prepared: PreparedRoute,
    *,
    cfg: HostRouterConfig,
    db_path: Path,
    board: Optional[str],
    claim_lock: str,
) -> SupervisorPid:
    """Launch the shipped local supervisor; no operator command is accepted."""
    route_cfg = cfg.routes.get(prepared.route)
    if route_cfg is None:
        raise ProtocolError("remote route disappeared before launch")
    argv = [sys.executable, "-m", "hermes_cli.kanban_codex_host", "supervisor", "--db", str(db_path), "--task-id", str(task.id), "--run-id", str(task.current_run_id), "--claim-lock", claim_lock, "--route", prepared.route, "--workspace", str(workspace), "--board", board or "default"]
    # The supervisor needs only interpreter/SSH identity.  In particular,
    # never inherit model/API keys or task-shaped environment variables into
    # the remote helper boundary.
    env = _safe_subprocess_env()
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, close_fds=True)
    init = {
        "protocol": PROTOCOL,
        "marker": HELPER_MARKER,
        "task_id": prepared.task_id,
        "run_id": int(task.current_run_id),
        "token": prepared.token,
        "remote_workspace": prepared.remote_workspace,
        "remote_lease": prepared.remote_lease,
        "ssh_target": route_cfg.ssh_target,
        "workspace_root_marker": _workspace_root_marker(route_cfg.workspace_root),
        "base": prepared.base,
        "tree": prepared.tree,
        "branch": prepared.branch,
        "workspace_marker": prepared.marker,
        "reason": _safe_reason(prepared.selection_reason),
        "route_config": _route_config_payload(route_cfg),
    }
    try:
        assert process.stdin is not None
        process.stdin.write(json.dumps(init, separators=(",", ":")).encode("utf-8"))
        process.stdin.close()
    except Exception:
        process.kill()
        raise
    return SupervisorPid(process.pid)


def _load_supervisor_db(path: str, board: str):
    from hermes_cli import kanban_db as kb
    os.environ["HERMES_KANBAN_DB"] = path
    os.environ["HERMES_KANBAN_BOARD"] = board
    return kb, kb.connect(board=board)


def _supervisor_cleanup(kb: Any, route: RouteConfig, prepared: Mapping[str, Any], *, allow: str) -> bool:
    request = {"protocol": PROTOCOL, "marker": HELPER_MARKER, "op": "cleanup", "token": prepared["token"], "task_id": prepared["task_id"], "remote_workspace": prepared["remote_workspace"], "workspace_root": route.workspace_root, "workspace_root_marker": _workspace_root_marker(route.workspace_root), "ssh_target": route.ssh_target, "base": prepared["base"], "tree": prepared["tree"], "branch": prepared["branch"], "workspace_marker": prepared["workspace_marker"], "allow": allow}
    try:
        _remote_call(route, request, timeout=30, output_limit=4096)
        return True
    except ProtocolError:
        return False


def _verify_artifact(artifact: Mapping[str, Any], maximum: int) -> tuple[bytes, list[dict[str, Any]]]:
    try:
        diff = base64.b64decode(str(artifact.get("diff_b64") or ""), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ProtocolError("artifact diff malformed") from exc
    files = artifact.get("untracked")
    if not isinstance(files, list):
        raise ProtocolError("artifact files malformed")
    total = len(diff)
    safe: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, Mapping) or not _safe_relative(str(item.get("path") or "")):
            raise ProtocolError("artifact path unsafe")
        if str(item["path"]) == ".hermes-codex-marker":
            raise ProtocolError("artifact marker path is reserved")
        try:
            data = base64.b64decode(str(item.get("data_b64") or ""), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ProtocolError("artifact file malformed") from exc
        if len(data) > maximum or total + len(data) > maximum:
            raise ProtocolError("artifact oversized")
        mode = item.get("mode", 0o644)
        if type(mode) is not int or mode < 0 or mode > 0o777:
            raise ProtocolError("artifact mode unsafe")
        total += len(data)
        safe.append({"path": str(item["path"]), "data": data, "mode": mode})
    if total > maximum:
        raise ProtocolError("artifact oversized")
    reported_bytes = artifact.get("bytes")
    if type(reported_bytes) is not int or reported_bytes != total:
        raise ProtocolError("artifact byte receipt mismatch")
    return diff, safe


def _validate_patch_paths(diff: bytes) -> None:
    """Reject patch paths that could escape the exact local worktree."""
    # ``git diff --binary`` may contain arbitrary binary payloads.  Surrogate
    # escaping keeps the header/path lines inspectable without rejecting a
    # valid binary patch solely because its body is not UTF-8.
    text = diff.decode("utf-8", "surrogateescape")
    for line in text.splitlines():
        value = None
        if line.startswith("diff --git a/"):
            fields = line.split()
            if len(fields) >= 4:
                for field in fields[2:4]:
                    value = field[2:] if field[:2] in {"a/", "b/"} else field
                    if not _safe_relative(value):
                        raise ProtocolError("artifact patch path is unsafe")
        elif line.startswith(("--- ", "+++ ")):
            value = line[4:].split("\t", 1)[0]
            if value == "/dev/null":
                continue
            if value.startswith(("a/", "b/")):
                value = value[2:]
            if not _safe_relative(value):
                raise ProtocolError("artifact patch path is unsafe")
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            value = line.split(" ", 2)[-1]
            if not _safe_relative(value):
                raise ProtocolError("artifact patch path is unsafe")
    if "new file mode 120000" in text or "old mode 120000" in text or "mode 160000" in text:
        raise ProtocolError("artifact symlink or submodule is unsafe")


def _preflight_artifact_files(workspace: Path, files: Sequence[Mapping[str, Any]]) -> None:
    root = workspace.resolve(strict=True)
    if workspace.is_symlink() or not workspace.is_dir():
        raise ProtocolError("local workspace is not a regular directory")
    for item in files:
        relative = str(item["path"])
        target = (root / relative).resolve(strict=False)
        parent = root
        for part in Path(relative).parts[:-1]:
            parent = parent / part
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise ProtocolError("local artifact parent is unsafe")
        if root not in target.parents or target == root:
            raise ProtocolError("local artifact path escapes workspace")
        if target.exists() and (target.is_symlink() or target.is_dir()):
            raise ProtocolError("local artifact path is unsafe")


def _ensure_safe_artifact_parent(root: Path, relative: str) -> Path:
    """Create/check untracked-file parents one component at a time."""
    parent = root
    for part in Path(relative).parts[:-1]:
        parent = parent / part
        if parent.exists():
            if parent.is_symlink() or not parent.is_dir():
                raise ProtocolError("local artifact parent is unsafe")
        else:
            parent.mkdir()
            if parent.is_symlink() or not parent.is_dir():
                raise ProtocolError("local artifact parent is unsafe")
    return parent


def _apply_artifact(workspace: Path, artifact: Mapping[str, Any], maximum: int) -> None:
    diff, files = _verify_artifact(artifact, maximum)
    _preflight_artifact_files(workspace, files)
    if diff:
        _validate_patch_paths(diff)
    root = workspace.resolve(strict=True)
    staged: list[tuple[Path, Path]] = []
    backups: dict[Path, tuple[bytes, int]] = {}
    for item in files:
        relative = str(item["path"])
        parent = _ensure_safe_artifact_parent(root, relative)
        target = (root / relative).resolve(strict=False)
        if root not in target.parents or target == root:
            raise ProtocolError("local artifact path escapes workspace")
        if target.exists() and (target.is_symlink() or target.is_dir()):
            raise ProtocolError("local artifact path is unsafe")
        if target.parent != parent.resolve(strict=False):
            raise ProtocolError("local artifact parent is unsafe")
        if target.exists():
            target_stat = target.stat()
            if not stat.S_ISREG(target_stat.st_mode) or target_stat.st_size > maximum:
                raise ProtocolError("local artifact replacement target is unsafe")
            backups[target] = (target.read_bytes(), stat.S_IMODE(target_stat.st_mode))
        fd, temp_name = tempfile.mkstemp(prefix=".hermes-artifact-", dir=str(target.parent))
        try:
            os.fchmod(fd, item["mode"] & 0o777)
            with os.fdopen(fd, "wb") as handle:
                handle.write(item["data"])
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((Path(temp_name), target))
        finally:
            try:
                if not any(path == Path(temp_name) for path, _ in staged):
                    os.unlink(temp_name)
            except FileNotFoundError:
                pass

    patch_path: Optional[Path] = None
    applied = False
    replaced: list[Path] = []
    try:
        if diff:
            with tempfile.NamedTemporaryFile(
                prefix="hermes-codex-apply-", suffix=".diff", delete=False,
            ) as handle:
                patch_path = Path(handle.name)
                handle.write(diff)
                handle.flush()
                os.fsync(handle.fileno())
            checked = subprocess.run(
                ["git", "-C", str(workspace), "apply", "--check", "--binary", str(patch_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60, check=False,
            )
            if checked.returncode != 0:
                raise ProtocolError("local artifact does not apply cleanly")
            applied_result = subprocess.run(
                ["git", "-C", str(workspace), "apply", "--binary", str(patch_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60, check=False,
            )
            if applied_result.returncode != 0:
                raise ProtocolError("local artifact apply failed")
            applied = True
        for staged_path, target in staged:
            if target.is_symlink() or target.is_dir():
                raise ProtocolError("local artifact path became unsafe")
            os.replace(staged_path, target)
            replaced.append(target)
    except Exception as exc:
        # A staged untracked write can fail after the tracked patch has been
        # applied (for example, a parent is replaced concurrently).  Roll back
        # both sides before surfacing the failure; if rollback itself cannot be
        # proved, the caller's run fence remains the final safety boundary.
        rollback_error: Optional[Exception] = None
        for target in reversed(replaced):
            try:
                if target in backups:
                    backup_data, backup_mode = backups[target]
                    fd, restore_name = tempfile.mkstemp(
                        prefix=".hermes-artifact-restore-", dir=str(target.parent),
                    )
                    os.fchmod(fd, backup_mode)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(backup_data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(restore_name, target)
                elif not target.is_symlink() and target.exists():
                    target.unlink()
            except Exception as restore_exc:  # pragma: no cover - race guard
                rollback_error = restore_exc
        if applied and patch_path is not None:
            try:
                reverted = subprocess.run(
                    ["git", "-C", str(workspace), "apply", "--reverse", "--binary", str(patch_path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=60, check=False,
                )
                if reverted.returncode != 0:
                    rollback_error = ProtocolError("local artifact rollback failed")
            except Exception as revert_exc:  # pragma: no cover - race guard
                rollback_error = revert_exc
        if rollback_error is not None:
            raise ProtocolError("local artifact rollback could not be proven") from rollback_error
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("local artifact apply failed") from exc
    finally:
        for staged_path, _target in staged:
            try:
                staged_path.unlink()
            except FileNotFoundError:
                pass
        if patch_path is not None:
            patch_path.unlink(missing_ok=True)


def _remote_context(kb: Any, conn: Any, task_id: str) -> str:
    return kb.build_worker_context(conn, task_id)


def _supervisor_main(argv: argparse.Namespace) -> int:
    kb = None
    conn = None
    task = None
    init: Optional[Mapping[str, Any]] = None
    try:
        init_raw = sys.stdin.buffer.read(MAX_PROTOCOL_INPUT + 1)
        init = json.loads(init_raw.decode("utf-8"))
        if not isinstance(init, Mapping) or init.get("protocol") != PROTOCOL or init.get("marker") != HELPER_MARKER:
            return 2
        if init.get("task_id") != argv.task_id or int(init.get("run_id")) != int(argv.run_id):
            return 2
        route_payload = init.get("route_config")
        if not isinstance(route_payload, Mapping):
            return 2
        route = _parse_route_config(route_payload, argv.route)
        if (
            init.get("ssh_target") != route.ssh_target
            or init.get("workspace_root_marker")
            != _workspace_root_marker(route.workspace_root)
            or not isinstance(init.get("token"), str)
            or not _TOKEN_RE.fullmatch(init["token"])
            or init.get("remote_lease") != init.get("token")
            or not _safe_relative(str(init.get("remote_workspace") or ""))
            or not isinstance(init.get("base"), str)
            or _OID_RE.fullmatch(init["base"]) is None
            or not isinstance(init.get("tree"), str)
            or _OID_RE.fullmatch(init["tree"]) is None
            or not _safe_branch(init.get("branch"))
            or not isinstance(init.get("workspace_marker"), str)
            or _TOKEN_RE.fullmatch(init["workspace_marker"]) is None
        ):
            return 2
        kb, conn = _load_supervisor_db(argv.db, argv.board)
        try:
            task = kb.get_task(conn, argv.task_id)
            expected_workspace = Path(argv.workspace).resolve(strict=False)
            task_workspace = (
                Path(task.workspace_path).resolve(strict=False)
                if task is not None and task.workspace_path else None
            )
            if (
                task is None
                or task.current_run_id != int(argv.run_id)
                or task.claim_lock != argv.claim_lock
                or task.status != "running"
                or task.workspace_kind != "worktree"
                or task_workspace != expected_workspace
                or task.branch_name != init["branch"]
            ):
                _fence_remote(kb, conn, argv.task_id, int(argv.run_id), "ownership_lost_before_supervisor")
                return 2
            context = _remote_context(kb, conn, argv.task_id)
            task_limit = getattr(task, "max_runtime_seconds", None)
            runtime_limit = route.timeout_seconds
            if type(task_limit) is int and task_limit > 0:
                runtime_limit = min(runtime_limit, float(task_limit))
            run_request = {"protocol": PROTOCOL, "marker": HELPER_MARKER, "op": "run", "token": init["token"], "task_id": argv.task_id, "remote_workspace": init["remote_workspace"], "workspace_root": route.workspace_root, "workspace_root_marker": _workspace_root_marker(route.workspace_root), "ssh_target": route.ssh_target, "base": init["base"], "tree": init["tree"], "branch": init["branch"], "workspace_marker": init["workspace_marker"], "codex_command": list(route.codex_command), "check_commands": [list(command) for command in route.check_commands], "timeout_seconds": runtime_limit, "context_b64": base64.b64encode(context.encode("utf-8")).decode("ascii")}
            try:
                remote_proc = _remote_process(route, run_request)
            except Exception:
                # Popen/write failure happens before a remote Codex receipt is
                # available.  Probe the exact lease anyway: an idempotent
                # no-mutation cleanup is safe, while any uncertainty becomes a
                # durable fence through the same failure path as disconnects.
                _handle_remote_failure(
                    kb, conn, task, init, route,
                    "remote_supervisor_launch_failed", ambiguous=True,
                )
                return 1
            started = time.monotonic()
            # The helper enforces ``runtime_limit`` around the Codex child and
            # then writes the authoritative mutation receipt.  Give that
            # bounded finalization a small grace window; killing the transport
            # at the exact same deadline races the no-mutation receipt and
            # turns a safely timed-out writer into a permanent ambiguity fence.
            supervisor_deadline = runtime_limit + max(
                1.0, min(5.0, route.heartbeat_seconds * 4),
            )
            response_raw = b""
            while remote_proc.poll() is None:
                if time.monotonic() - started > supervisor_deadline:
                    _kill_process_group(remote_proc)
                    remote_proc.wait(timeout=5)
                    _handle_remote_failure(kb, conn, task, init, route, "remote_timeout", ambiguous=True)
                    return 1
                current = kb.get_task(conn, argv.task_id)
                current_workspace = (
                    Path(current.workspace_path).resolve(strict=False)
                    if current is not None and current.workspace_path else None
                )
                if (
                    current is None
                    or current.current_run_id != int(argv.run_id)
                    or current.claim_lock != argv.claim_lock
                    or current_workspace != expected_workspace
                    or current.branch_name != init["branch"]
                ):
                    remote_proc.kill()
                    remote_proc.wait(timeout=5)
                    _handle_remote_failure(kb, conn, task, init, route, "ownership_lost", ambiguous=True)
                    return 1
                claim_alive = kb.heartbeat_claim(
                    conn, argv.task_id,
                    ttl_seconds=max(60, int(route.heartbeat_seconds * 4)),
                    claimer=argv.claim_lock,
                )
                worker_heartbeat = kb.heartbeat_worker(
                    conn, argv.task_id, expected_run_id=int(argv.run_id),
                )
                if not claim_alive or not worker_heartbeat:
                    remote_proc.kill()
                    remote_proc.wait(timeout=5)
                    _handle_remote_failure(kb, conn, task, init, route, "heartbeat_lost", ambiguous=True)
                    return 1
                time.sleep(min(route.heartbeat_seconds, 1.0))
            response_raw = remote_proc.stdout.read(MAX_PROTOCOL_OUTPUT + 1) if remote_proc.stdout else b""
            if remote_proc.returncode != 0 or len(response_raw) > MAX_PROTOCOL_OUTPUT:
                _handle_remote_failure(kb, conn, task, init, route, "remote_transport_failed", ambiguous=True)
                return 1
            response = json.loads(response_raw.decode("utf-8"))
            if (
                not isinstance(response, Mapping)
                or response.get("ok") is not True
                or response.get("protocol") != PROTOCOL
                or response.get("marker") != HELPER_MARKER
                or response.get("token") != init["token"]
                or response.get("task_id") != argv.task_id
                or response.get("remote_workspace") != init["remote_workspace"]
                or response.get("base") != init["base"]
                or response.get("tree") != init["tree"]
                or response.get("branch") != init["branch"]
                or response.get("workspace_marker") != init["workspace_marker"]
                or response.get("ssh_target") != route.ssh_target
                or response.get("workspace_root_marker") != init["workspace_root_marker"]
            ):
                _handle_remote_failure(kb, conn, task, init, route, "remote_receipt_invalid", ambiguous=True)
                return 1
            mutation = response.get("mutation_state")
            check_status = response.get("check_status")
            output_status = response.get("output_status")
            if mutation not in {"no_mutation", "mutated", "ambiguous"} or check_status not in {"passed", "failed"} or output_status not in {"bounded", "oversized"}:
                _handle_remote_failure(kb, conn, task, init, route, "remote_receipt_invalid", ambiguous=True)
                return 1
            codex_returncode = response.get("codex_returncode")
            if codex_returncode is not None:
                _receipt_int(codex_returncode, "Codex return code", minimum=-255, maximum=255)
            check_count = _receipt_int(response.get("check_count"), "check count", maximum=len(route.check_commands))
            check_latency_ms = _receipt_int(response.get("check_latency_ms"), "check latency")
            remote_elapsed_ms = _receipt_int(response.get("elapsed_ms"), "elapsed")
            _receipt_int(response.get("stdout_bytes"), "stdout bytes")
            _receipt_int(response.get("stderr_bytes"), "stderr bytes")
            if mutation == "no_mutation":
                cleaned = _supervisor_cleanup(kb, route, init, allow="no_mutation")
                if cleaned:
                    _mark_remote_direct(kb, conn, argv.task_id, int(argv.run_id))
                    reason = (
                        "remote Codex produced no mutation"
                        if codex_returncode == 0
                        and check_status == "passed"
                        else "remote Codex check or execution failed without mutation"
                    )
                    kb.block_task(
                        conn, argv.task_id, reason=reason, kind="capability",
                        expected_run_id=int(argv.run_id),
                    )
                else:
                    _fence_remote(kb, conn, argv.task_id, int(argv.run_id), "cleanup_unproven")
                return 1
            if mutation != "mutated":
                _handle_remote_failure(kb, conn, task, init, route, "remote_mutation_ambiguous", ambiguous=True)
                return 1
            if check_status != "passed" or codex_returncode != 0 or output_status != "bounded":
                # A mutated but unverified workspace is deliberately fenced;
                # applying it locally would make a failed check look like a
                # successful implementation and starting a local retry could
                # overlap the remote writer.
                _handle_remote_failure(kb, conn, task, init, route, "remote_check_failed", ambiguous=True)
                return 1
            collect_request = {"protocol": PROTOCOL, "marker": HELPER_MARKER, "op": "collect", "token": init["token"], "task_id": argv.task_id, "remote_workspace": init["remote_workspace"], "workspace_root": route.workspace_root, "workspace_root_marker": _workspace_root_marker(route.workspace_root), "ssh_target": route.ssh_target, "base": init["base"], "tree": init["tree"], "branch": init["branch"], "workspace_marker": init["workspace_marker"], "artifact_max_bytes": route.artifact_max_bytes}
            collected = _remote_call(
                route, collect_request, timeout=route.timeout_seconds,
                output_limit=min(
                    MAX_PROTOCOL_OUTPUT,
                    route.artifact_max_bytes * 2 + 64 * 1024,
                ),
            )
            current = kb.get_task(conn, argv.task_id)
            current_workspace = (
                Path(current.workspace_path).resolve(strict=False)
                if current is not None and current.workspace_path else None
            )
            if (
                current is None
                or current.current_run_id != int(argv.run_id)
                or current.claim_lock != argv.claim_lock
                or current_workspace != expected_workspace
                or current.branch_name != init["branch"]
            ):
                _fence_remote(kb, conn, argv.task_id, int(argv.run_id), "ownership_lost_before_apply")
                return 1
            identity = workspace_identity(Path(argv.workspace))
            if identity["base"] != init["base"] or identity["tree"] != init["tree"] or identity["branch"] != init["branch"] or identity["workspace_marker"] != init["workspace_marker"]:
                _fence_remote(kb, conn, argv.task_id, int(argv.run_id), "local_identity_changed")
                return 1
            _apply_artifact(Path(argv.workspace), collected.get("artifact") or {}, route.artifact_max_bytes)
            cleaned = _supervisor_cleanup(kb, route, init, allow="recovered")
            if not cleaned:
                _fence_remote(kb, conn, argv.task_id, int(argv.run_id), "cleanup_unproven")
                return 1
            artifact = collected.get("artifact")
            if not isinstance(artifact, Mapping):
                raise ProtocolError("remote artifact receipt malformed")
            artifact_bytes = _receipt_int(
                artifact.get("bytes"), "artifact bytes",
                maximum=route.artifact_max_bytes,
            )
            receipt = {
                "contract": PROTOCOL,
                "route": argv.route,
                "reason": _safe_reason(init.get("reason"), "remote_codex"),
                "mutation_state": "mutated",
                "artifact_status": "applied",
                "check_status": "passed",
                "workspace_marker": init["workspace_marker"],
                "base": init["base"],
                "tree": init["tree"],
                "branch": init["branch"],
                "task_id": argv.task_id,
                "run_id": int(argv.run_id),
                "artifact_bytes": artifact_bytes,
                "check_count": check_count,
                "check_latency_ms": check_latency_ms,
                "remote_elapsed_ms": remote_elapsed_ms,
                "latency_ms": remote_elapsed_ms,
            }
            _mark_remote_terminal(kb, conn, argv.task_id, int(argv.run_id), receipt, review=True)
            return 0
        finally:
            conn.close()
    except Exception:
        # Every failure after launch is treated as potentially mutating.  If
        # the helper did not return a positive no-mutation receipt, keep the
        # canonical run fenced; the dispatcher must never start a local writer
        # beside an unknown remote workspace.
        if kb is not None and conn is not None and init is not None and task is not None:
            try:
                _handle_remote_failure(
                    kb, conn, task, init, route,
                    "supervisor_failure", ambiguous=True,
                )
            except Exception:
                try:
                    _fence_remote(kb, conn, argv.task_id, int(argv.run_id), "supervisor_failure")
                except Exception:
                    pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return 1


def _remote_process(route: RouteConfig, request: Mapping[str, Any]) -> subprocess.Popen:
    argv = _remote_argv(route)
    encoded = json.dumps(dict(request), separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PROTOCOL_INPUT:
        raise ProtocolError("remote protocol request oversized")
    process = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=_safe_subprocess_env(), close_fds=True,
        start_new_session=(os.name == "posix"),
    )
    def _write() -> None:
        try:
            if process.stdin is not None:
                process.stdin.write(encoded)
                process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    writer = threading.Thread(target=_write, name="kanban-remote-input", daemon=True)
    writer.start()
    writer.join(timeout=min(route.timeout_seconds, 30.0))
    if writer.is_alive():
        _kill_process_group(process)
        process.wait()
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        raise ProtocolError("remote protocol input stalled")
    return process


def _fence_remote(kb: Any, conn: Any, task_id: str, run_id: int, reason: str) -> None:
    marker = f"remote-fence:{run_id}"
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked', block_kind='capability', claim_lock=?, claim_expires=NULL, worker_pid=NULL WHERE id=? AND status='running' AND current_run_id=?", (marker, task_id, run_id))
        closed = kb._end_run(conn, task_id, outcome="blocked", status="blocked", summary="remote mutation fenced", metadata={"contract": PROTOCOL, "reason": reason[:120], "mutation_state": "ambiguous"})
        kb._append_event(
            conn, task_id, "blocked",
            {
                "reason": reason[:120],
                "kind": "capability",
                "source_status": "running",
                "remote_fence": True,
            },
            run_id=closed or run_id,
        )
        kb._append_event(conn, task_id, "remote_mutation_fenced", {"contract": PROTOCOL, "reason": reason[:120], "mutation_state": "ambiguous", "fence": marker}, run_id=closed or run_id)


def _handle_remote_failure(kb: Any, conn: Any, task: Any, init: Mapping[str, Any], route: RouteConfig, reason: str, *, ambiguous: bool) -> None:
    if ambiguous:
        # A lost transport can still be recovered safely when the helper's
        # lease state positively proves that no remote run ever started.  The
        # helper refuses this cleanup while a run is in progress or after any
        # mutation, so this probe cannot create an overlapping writer.
        cleaned = _supervisor_cleanup(kb, route, init, allow="no_mutation")
        if cleaned:
            try:
                _mark_remote_direct(kb, conn, task.id, int(init["run_id"]))
                kb.block_task(
                    conn, task.id, reason=reason[:120], kind="capability",
                    expected_run_id=int(init["run_id"]),
                )
                return
            except Exception:
                pass
        _fence_remote(kb, conn, task.id, int(init["run_id"]), reason)
        return
    cleaned = _supervisor_cleanup(kb, route, init, allow="no_mutation")
    if cleaned:
        kb.block_task(conn, task.id, reason=reason[:120], kind="capability", expected_run_id=int(init["run_id"]))
    else:
        _fence_remote(kb, conn, task.id, int(init["run_id"]), "cleanup_unproven")


def _mark_remote_terminal(kb: Any, conn: Any, task_id: str, run_id: int, receipt: Mapping[str, Any], *, review: bool) -> None:
    # Convert the remote launch receipt back into the existing direct lifecycle
    # classification before invoking request_review; no new DB lifecycle is
    # introduced and all persisted metadata is an allowlisted projection.
    with kb.write_txn(conn):
        row = conn.execute("SELECT id FROM task_runs WHERE id=? AND task_id=? AND ended_at IS NULL", (run_id, task_id)).fetchone()
        if row is None:
            raise ProtocolError("remote run no longer active")
        conn.execute("UPDATE task_runs SET launch_mode='direct', verification_status='not-applicable', metadata=? WHERE id=?", (json.dumps(dict(receipt), separators=(",", ":")), run_id))
        kb._append_event(
            conn, task_id, "remote_route_completed",
            dict(receipt), run_id=run_id,
        )
    if review:
        ok = kb.request_review(conn, task_id, summary="Remote Codex mutation recovered; review required", metadata=dict(receipt), expected_run_id=run_id)
        if not ok:
            _fence_remote(kb, conn, task_id, run_id, "review_transition_failed")


def _mark_remote_direct(kb: Any, conn: Any, task_id: str, run_id: int) -> None:
    """Move a positively cleaned remote run to the legacy direct class."""
    with kb.write_txn(conn):
        current = conn.execute(
            "SELECT id FROM task_runs WHERE id=? AND task_id=? "
            "AND ended_at IS NULL",
            (run_id, task_id),
        ).fetchone()
        if current is None:
            raise ProtocolError("remote run no longer active")
        conn.execute(
            "UPDATE task_runs SET launch_mode='direct', "
            "verification_status='not-applicable', worker_pid=NULL "
            "WHERE id=? AND task_id=? AND ended_at IS NULL",
            (run_id, task_id),
        )


def supervisor_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--claim-lock", required=True)
    parser.add_argument("--route", required=True, choices=("wsl_codex", "mac_codex"))
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--board", required=True)
    # argv[1] is the ``supervisor`` dispatch word consumed by ``main``.
    return _supervisor_main(parser.parse_args(sys.argv[2:]))


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "helper":
        return helper_main()
    if len(sys.argv) > 1 and sys.argv[1] == "supervisor":
        return supervisor_main()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
