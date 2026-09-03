"""Tracked direct-Codex execution under one Project/Outcome.

This module closes the control loop around the normal bounded FEATURE route:
register/admit in the root Outcome store, launch exactly one Codex CLI process,
heartbeat while it runs, and terminalize the same execution with a durable
receipt path. It does not create Projects, Outcomes, worktrees, or Kanban cards.
Callers must freeze those identities before launch.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import outcomes_db as odb


DEFAULT_HEARTBEAT_SECONDS = 15.0
DEFAULT_TIMEOUT_SECONDS = 30 * 60
_MAX_PROMPT_BYTES = 2 * 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class DirectCodexExecutionError(RuntimeError):
    """Raised when the bounded direct-Codex execution contract is invalid."""


def _resolve_codex_executable(explicit: Optional[str] = None) -> str:
    candidate = str(explicit or os.environ.get("HERMES_GPT_CODEX_EXE") or "").strip()
    if candidate:
        path = Path(candidate).expanduser().resolve(strict=False)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise DirectCodexExecutionError(f"Codex executable is not launchable: {path}")
        return str(path)
    discovered = shutil.which("codex")
    if not discovered:
        raise DirectCodexExecutionError("No launchable Codex CLI executable found on PATH")
    return discovered


def _git_output(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        shell=False,
    )
    if proc.returncode != 0:
        raise DirectCodexExecutionError(
            f"git {' '.join(args)} failed for {repo}: {(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return proc.stdout.strip()


def canonical_repo_identity(repo: Path) -> str:
    """Return the same portable repository identity used by mutation leases."""
    repo = Path(repo).expanduser().resolve(strict=False)
    if not repo.is_dir():
        raise DirectCodexExecutionError(f"repo does not exist: {repo}")
    _git_output(repo, "rev-parse", "--is-inside-work-tree")
    try:
        remote = _git_output(repo, "config", "--get", "remote.origin.url")
    except DirectCodexExecutionError:
        remote = ""
    return odb._normalize_repository(remote or str(repo))


def current_base_ref(repo: Path) -> str:
    """Return an exact immutable base reference for a clean isolated worktree."""
    repo = Path(repo).expanduser().resolve(strict=False)
    head = _git_output(repo, "rev-parse", "HEAD")
    branch = _git_output(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return f"{branch or 'HEAD'}@{head}"


def _verify_repo_identity(execution: dict[str, Any], repo: Path) -> None:
    if not repo.is_dir():
        raise DirectCodexExecutionError(f"repo does not exist: {repo}")
    _git_output(repo, "rev-parse", "--is-inside-work-tree")
    dirty = _git_output(repo, "status", "--porcelain")
    if dirty:
        raise DirectCodexExecutionError(
            "direct Codex runner requires a clean isolated repo/worktree"
        )

    expected = str(execution.get("repository") or "").strip()
    if not expected:
        raise DirectCodexExecutionError("direct_codex execution has no repository identity")
    actual = canonical_repo_identity(repo)
    if actual != expected:
        raise DirectCodexExecutionError(
            f"repo identity mismatch: execution={expected!r} actual={actual!r}"
        )

    base_ref = str(execution.get("base_ref") or "").strip()
    if not base_ref:
        raise DirectCodexExecutionError("direct_codex execution has no source/base reference")
    candidate_sha = base_ref.rsplit("@", 1)[-1]
    if _SHA_RE.fullmatch(candidate_sha):
        head = _git_output(repo, "rev-parse", "HEAD")
        if head.lower() != candidate_sha.lower():
            raise DirectCodexExecutionError(
                f"repo HEAD {head} does not match execution base {candidate_sha}"
            )


def _read_prompt(path: Path) -> bytes:
    if not path.is_file():
        raise DirectCodexExecutionError(f"prompt file does not exist: {path}")
    size = path.stat().st_size
    if size < 1:
        raise DirectCodexExecutionError("prompt file is empty")
    if size > _MAX_PROMPT_BYTES:
        raise DirectCodexExecutionError(
            f"prompt file exceeds {_MAX_PROMPT_BYTES} bytes: {size}"
        )
    return path.read_bytes()


def run_direct_codex_execution(
    conn,
    *,
    execution_id: str,
    repo: Path,
    prompt_file: Path,
    output_file: Path,
    stderr_file: Path,
    codex_executable: Optional[str] = None,
    codex_profile: str = "writer",
    sandbox: str = "workspace-write",
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one already-registered ``direct_codex`` execution to terminal state.

    ``ExecutionAdmissionBlocked`` is intentionally allowed to propagate. A
    ``waiting_resource`` result must be materialized through the durable Kanban
    path by the Outcome owner; no direct Codex process is launched while queued.
    """

    execution = odb.get_execution(conn, execution_id)
    if execution is None:
        raise DirectCodexExecutionError(f"unknown execution: {execution_id}")
    if execution.get("execution_mode") != "direct_codex":
        raise DirectCodexExecutionError("execution mode must be direct_codex")
    if not execution.get("mutating"):
        raise DirectCodexExecutionError("direct Codex runner requires a mutating execution")

    repo = Path(repo).expanduser().resolve(strict=False)
    prompt_file = Path(prompt_file).expanduser().resolve(strict=False)
    output_file = Path(output_file).expanduser().resolve(strict=False)
    stderr_file = Path(stderr_file).expanduser().resolve(strict=False)
    _verify_repo_identity(execution, repo)
    prompt = _read_prompt(prompt_file)
    executable = _resolve_codex_executable(codex_executable)

    if sandbox not in {"read-only", "workspace-write"}:
        raise DirectCodexExecutionError(
            "direct Codex runner only allows read-only or workspace-write sandbox"
        )
    if not str(codex_profile or "").strip():
        raise DirectCodexExecutionError("codex_profile is required")
    try:
        heartbeat_seconds = max(0.1, float(heartbeat_seconds))
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        raise DirectCodexExecutionError("heartbeat/timeout values are invalid") from None
    if timeout_seconds < 1:
        raise DirectCodexExecutionError("timeout_seconds must be positive")

    # Atomic root admission happens before any child process exists. If capacity
    # or a shared resource is unavailable, this raises and the execution remains
    # queued/waiting_resource; nothing untracked is launched.
    admitted = odb.admit_execution(conn, execution_id, require_feature_gate=True)
    if admitted.get("state") != "running":
        raise DirectCodexExecutionError("execution admission did not reach running state")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    stderr_file.parent.mkdir(parents=True, exist_ok=True)
    command = [
        executable,
        "exec",
        "-p",
        str(codex_profile),
        "-C",
        str(repo),
        "--sandbox",
        sandbox,
        "-o",
        str(output_file),
        "-",
    ]
    started = time.monotonic()
    process: Optional[subprocess.Popen] = None
    returncode: Optional[int] = None
    terminal_state = "failed"
    terminal_reason = "direct_codex_spawn_failed"
    runtime_error: Optional[str] = None
    try:
        with stderr_file.open("wb") as err:
            process = subprocess.Popen(
                command,
                cwd=str(repo),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=err,
                shell=False,
                start_new_session=True,
            )
            assert process.stdin is not None
            process.stdin.write(prompt)
            process.stdin.close()

            while True:
                returncode = process.poll()
                if returncode is not None:
                    break
                elapsed = time.monotonic() - started
                if elapsed >= timeout_seconds:
                    terminal_reason = "direct_codex_timeout"
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                    returncode = process.returncode
                    break
                odb.heartbeat_execution(conn, execution_id)
                time.sleep(min(heartbeat_seconds, max(0.1, timeout_seconds - elapsed)))

        if returncode == 0 and output_file.is_file() and output_file.stat().st_size > 0:
            terminal_state = "completed"
            terminal_reason = "direct_codex_completed"
        elif returncode == 0:
            terminal_reason = "direct_codex_missing_receipt"
        else:
            terminal_reason = f"direct_codex_exit_{returncode}"
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        terminal_reason = "direct_codex_interrupted"
        raise
    except Exception as exc:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        returncode = process.returncode if process is not None else None
        runtime_error = f"{type(exc).__name__}: {exc}"
        terminal_reason = f"direct_codex_exception_{type(exc).__name__}"
    finally:
        receipt_uri = output_file.as_uri() if output_file.exists() else None
        odb.terminalize_execution(
            conn,
            execution_id,
            state=terminal_state,
            receipt_uri=receipt_uri,
            reason=terminal_reason,
        )

    final_execution = odb.get_execution(conn, execution_id)
    return {
        "ok": terminal_state == "completed",
        "execution": final_execution,
        "returncode": returncode,
        "receipt_uri": final_execution.get("receipt_uri") if final_execution else None,
        "stderr_path": str(stderr_file),
        "error": runtime_error,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
