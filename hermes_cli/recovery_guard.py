#!/usr/bin/env python3
"""Gateway-independent candidate activation and rollback supervisor.

The guard is copied into a durable per-run directory and launched as a
systemd-user transient service.  It owns the complete transaction: request and
prove drain, run a hash-pinned candidate activation artifact, wait for exact
readiness/ownership proofs, and otherwise run a hash-pinned rollback artifact.
It uses only the Python standard library so it remains runnable while the
Hermes checkout/runtime is being replaced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PLAN_VERSION = 1
DEFAULT_HEALTH_URL = "http://127.0.0.1:8642/health"


class GuardError(RuntimeError):
    """A fail-closed guard validation or activation error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GuardError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(body, dict):
        raise GuardError(f"expected JSON object at {path}")
    return body


def _parse_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _nested_get(body: dict[str, Any], dotted: str) -> Any:
    current: Any = body
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            raise GuardError(f"proof field missing: {dotted}")
        current = current[part]
    return current


def _expect_fields(body: dict[str, Any], expected: dict[str, Any]) -> None:
    for dotted, wanted in expected.items():
        actual = _nested_get(body, dotted)
        if actual != wanted:
            raise GuardError(f"proof mismatch {dotted}: expected {wanted!r}, got {actual!r}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_start_time(pid: int) -> int | None:
    try:
        tail = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return int(tail[19])
    except (OSError, IndexError, ValueError):
        return None


def _process_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        ).strip()
    except OSError:
        return ""


def _instantiation_epoch() -> str:
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        boot_id = ""
    try:
        tail = Path("/proc/1/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        pid1_start = tail[19]
    except (OSError, IndexError):
        pid1_start = ""
    return f"{boot_id}:{pid1_start}" if boot_id or pid1_start else ""


def _http_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise GuardError(f"HTTP proof {url} returned {response.status}")
        body = json.loads(response.read())
    if not isinstance(body, dict):
        raise GuardError(f"HTTP proof {url} did not return an object")
    return body


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False)


@dataclass
class GuardOps:
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    run: Callable[[list[str], float], subprocess.CompletedProcess[str]] = _run
    http_json: Callable[[str, float], dict[str, Any]] = _http_json


class Receipt:
    def __init__(self, run_dir: Path, run_id: str, now: Callable[[], float]):
        self.run_dir = run_dir
        self.run_id = run_id
        self.now = now
        self.events_path = run_dir / "events.jsonl"
        self.final_path = run_dir / "result.json"

    def event(self, phase: str, **details: Any) -> None:
        payload = {
            "contract": "HRI-SELFHOST-OUTOFBAND-RECOVERY-GUARD-V1",
            "run_id": self.run_id,
            "phase": phase,
            "at": datetime.fromtimestamp(self.now(), timezone.utc).isoformat(),
            **details,
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def final(self, outcome: str, **details: Any) -> None:
        payload = {
            "contract": "HRI-SELFHOST-OUTOFBAND-RECOVERY-GUARD-V1",
            "run_id": self.run_id,
            "outcome": outcome,
            "at": datetime.fromtimestamp(self.now(), timezone.utc).isoformat(),
            **details,
        }
        _atomic_json(self.final_path, payload)
        self.event("final", outcome=outcome, result_path=str(self.final_path))


class RecoverySupervisor:
    def __init__(self, plan_path: Path, *, ops: GuardOps | None = None):
        self.plan_path = plan_path.resolve()
        self.plan = _read_json(self.plan_path)
        self.ops = ops or GuardOps()
        self.run_dir = self.plan_path.parent
        self.run_id = str(self.plan.get("run_id") or "")
        if self.plan.get("version") != PLAN_VERSION or not self.run_id:
            raise GuardError("invalid recovery plan version/run_id")
        self.receipt = Receipt(self.run_dir, self.run_id, self.ops.now)
        self.owner_token = str(self.plan.get("owner_token") or "")
        if not self.owner_token:
            raise GuardError("recovery plan has no owner token")
        _validate_proof_contract(self.plan)
        guard_sha256 = self.plan.get("guard_sha256")
        if guard_sha256 and _sha256(Path(__file__).resolve()) != guard_sha256:
            raise GuardError("sealed supervisor source hash mismatch")
        self.state_path = Path(self.plan["state_path"])
        self.drain_path = Path(self.plan["drain_path"])
        self.old_pid: int | None = None
        self.old_start_time: int | None = None
        self.old_service_processes: dict[int, int] | None = None

    def _verify_artifact(self, name: str) -> dict[str, Any]:
        spec = self.plan.get(name)
        if not isinstance(spec, dict):
            raise GuardError(f"missing {name} artifact")
        artifact = Path(str(spec.get("artifact", "")))
        expected = str(spec.get("sha256", ""))
        if not artifact.is_absolute() or not artifact.is_file() or len(expected) != 64:
            raise GuardError(f"invalid {name} artifact metadata")
        actual = _sha256(artifact)
        if actual != expected:
            raise GuardError(f"{name} artifact hash mismatch: {actual}")
        argv = spec.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
            raise GuardError(f"invalid {name} argv")
        return spec

    def _artifact_argv(self, spec: dict[str, Any]) -> list[str]:
        artifact = str(spec["artifact"])
        return [item.replace("{artifact}", artifact) for item in spec["argv"]]

    def _runtime_state(self, *, require_fresh: bool = True) -> dict[str, Any]:
        state = _read_json(self.state_path)
        if require_fresh:
            updated = _parse_time(state.get("updated_at"))
            max_age = float(self.plan.get("freshness_seconds", 15.0))
            age = self.ops.now() - updated if updated is not None else None
            if age is None or age > max_age or age < -5.0:
                raise GuardError("gateway runtime state is stale or lacks updated_at")
        pid = state.get("pid")
        start = state.get("start_time")
        if not isinstance(pid, int) or pid <= 0 or not _pid_alive(pid):
            raise GuardError("gateway runtime PID is not live")
        if isinstance(start, bool) or not isinstance(start, int) or start <= 0:
            raise GuardError("gateway runtime state lacks a valid positive start_time")
        observed_start = _process_start_time(pid)
        if isinstance(observed_start, bool) or not isinstance(observed_start, int) or observed_start <= 0:
            raise GuardError("gateway runtime PID live start_time is unavailable or invalid")
        if start != observed_start:
            raise GuardError("gateway runtime PID start_time does not match live process")
        return state

    def _write_owned_drain(self, pid: int) -> None:
        self.drain_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "action": "drain",
            "requested_at": _utc_now(),
            "principal": "recovery-guard",
            "epoch": _instantiation_epoch(),
            "suppress_notification": False,
            "owner_token": self.owner_token,
            "target_pid": pid,
        }
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(self.drain_path, flags, 0o600)
        except FileExistsError as exc:
            raise GuardError(f"pre-existing drain marker at {self.drain_path}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())

    def _clear_owned_drain(self) -> None:
        try:
            body = _read_json(self.drain_path)
        except GuardError:
            return
        if body.get("owner_token") == self.owner_token:
            self.drain_path.unlink(missing_ok=True)

    def _active_sessions(self) -> list[dict[str, Any]]:
        path_value = self.plan.get("active_sessions_path")
        if not path_value:
            raise GuardError("active_sessions_path is required for drain proof")
        path = Path(path_value)
        if not path.exists():
            raise GuardError("active session registry is unavailable")
        body = _read_json(path)
        entries = body.get("entries", [])
        if not isinstance(entries, list):
            raise GuardError("active session registry is malformed")
        active: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise GuardError("active session registry contains malformed entry")
            try:
                pid = int(entry.get("pid", 0))
            except (TypeError, ValueError):
                pid = 0
            start_time = entry.get("process_start_ticks")
            if pid <= 0 or isinstance(start_time, bool) or not isinstance(start_time, int):
                raise GuardError("active session registry contains uninspectable lease identity")
            if not _pid_alive(pid):
                raise GuardError("active session registry contains stale active session lease")
            observed_start = _process_start_time(pid)
            if observed_start is None or observed_start != start_time:
                raise GuardError("active session lease process identity is stale or foreign")
            active.append(entry)
        return active

    def _compression_locks(self) -> int:
        path_value = self.plan.get("state_db_path")
        if not path_value:
            raise GuardError("state_db_path is required for compression proof")
        path = Path(path_value)
        if not path.exists():
            raise GuardError("state database is unavailable for compression proof")
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
            try:
                row = connection.execute(
                    "SELECT COUNT(*) FROM compression_locks WHERE expires_at >= ?",
                    (self.ops.now(),),
                ).fetchone()
                return int(row[0]) if row else 0
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise GuardError(f"cannot prove compression locks are idle: {exc}") from exc

    def _service_pids(self, expected_main_pid: int) -> list[int]:
        unit = str(self.plan.get("gateway_unit") or "")
        if not unit:
            raise GuardError("gateway_unit is required for cgroup proof")
        result = self.ops.run(
            ["systemctl", "--user", "show", unit, "--property=MainPID", "--property=ControlGroup"],
            5.0,
        )
        if result.returncode != 0:
            raise GuardError(f"cannot inspect gateway service cgroup: {result.stderr.strip()}")
        props: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                props[key] = value
        try:
            main_pid = int(props.get("MainPID", "0"))
        except ValueError:
            main_pid = 0
        if main_pid != expected_main_pid:
            raise GuardError(f"service MainPID {main_pid} != runtime PID {expected_main_pid}")
        control_group = props.get("ControlGroup", "")
        if not control_group.startswith("/"):
            raise GuardError("gateway service has no inspectable ControlGroup")
        root = Path("/sys/fs/cgroup") / control_group.lstrip("/")
        if not root.is_dir():
            raise GuardError(f"gateway cgroup path not found: {root}")
        pids: set[int] = set()
        for procs in root.rglob("cgroup.procs"):
            try:
                pids.update(int(value) for value in procs.read_text().split())
            except (OSError, ValueError) as exc:
                raise GuardError(f"cannot inspect {procs}: {exc}") from exc
        return sorted(pid for pid in pids if _pid_alive(pid))

    def _service_processes(self, expected_main_pid: int) -> dict[int, int]:
        """Return exact live identities currently owned by the gateway unit."""
        identities: dict[int, int] = {}
        for pid in self._service_pids(expected_main_pid):
            start_time = _process_start_time(pid)
            if (
                isinstance(start_time, bool)
                or not isinstance(start_time, int)
                or start_time <= 0
            ):
                raise GuardError(f"gateway cgroup PID {pid} start_time is unavailable or invalid")
            identities[pid] = start_time
        if identities.get(expected_main_pid) != self.old_start_time:
            raise GuardError("gateway cgroup MainPID identity does not match runtime state")
        return identities

    def _drain_is_safe(self) -> dict[str, Any]:
        if self.old_pid is None:
            raise GuardError("old gateway identity was not captured")
        state = self._runtime_state()
        if state.get("pid") != self.old_pid or state.get("start_time") != self.old_start_time:
            raise GuardError("gateway identity changed during drain")
        expected = {
            "gateway_state": "draining",
            "drain_quiesced": True,
            "active_agents": 0,
            "active_cron_jobs": 0,
            "active_api_runs": 0,
            "active_work": 0,
        }
        _expect_fields(state, expected)
        sessions = self._active_sessions()
        locks = self._compression_locks()
        if self.old_service_processes is None:
            raise GuardError("gateway cgroup identities were not captured before drain")
        processes = self._service_processes(self.old_pid)
        unexpected = {
            pid: start_time
            for pid, start_time in processes.items()
            if self.old_service_processes.get(pid) != start_time
        }
        if sessions or locks or unexpected:
            raise GuardError(
                f"live drain evidence not idle: sessions={len(sessions)} "
                f"compression_locks={locks} unexpected_cgroup_processes={unexpected}"
            )
        return {
            "pid": self.old_pid,
            "start_time": self.old_start_time,
            "active_sessions": 0,
            "compression_locks": 0,
            "cgroup_processes": processes,
            "persistent_cgroup_processes": {
                pid: start_time
                for pid, start_time in processes.items()
                if pid != self.old_pid
            },
        }

    def _wait_for_drain(self) -> dict[str, Any]:
        deadline = self.ops.now() + float(self.plan.get("drain_timeout_seconds", 180.0))
        last_error = "drain not observed"
        while self.ops.now() < deadline:
            try:
                return self._drain_is_safe()
            except GuardError as exc:
                last_error = str(exc)
            self.ops.sleep(float(self.plan.get("poll_seconds", 1.0)))
        raise GuardError(f"drain deadline exceeded: {last_error}")

    def _check_proof(self, proof: dict[str, Any]) -> dict[str, Any]:
        kind = proof.get("type")
        role = str(proof.get("role") or "")
        if kind == "http_json":
            body = self.ops.http_json(str(proof.get("url") or DEFAULT_HEALTH_URL), 3.0)
            _expect_fields(body, proof.get("expected", {}))
            return {"type": kind, "role": role, "url": proof.get("url")}
        if kind == "json_file":
            path = Path(str(proof["path"]))
            body = _read_json(path)
            _expect_fields(body, proof.get("expected", {}))
            freshness_field = proof.get("freshness_field")
            if freshness_field:
                observed = _parse_time(_nested_get(body, str(freshness_field)))
                max_age = float(proof.get("max_age_seconds", self.plan.get("freshness_seconds", 15.0)))
                age = self.ops.now() - observed if observed is not None else None
                if age is None or age > max_age or age < -5.0:
                    raise GuardError(f"proof {path} is stale")
            return {"type": kind, "role": role, "path": str(path)}
        if kind == "command_text":
            argv = proof.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
                raise GuardError("invalid command_text proof argv")
            result = self.ops.run(argv, float(proof.get("timeout_seconds", 5.0)))
            if result.returncode != 0:
                raise GuardError(f"proof command failed: {result.stderr.strip()}")
            expected = str(proof.get("expected_stdout", ""))
            if result.stdout.strip() != expected:
                raise GuardError(
                    f"proof command stdout mismatch: expected {expected!r}, got {result.stdout.strip()!r}"
                )
            return {"type": kind, "role": role, "argv": argv}
        raise GuardError(f"unsupported proof type: {kind!r}")

    def _runtime_identity_proof(self) -> dict[str, Any]:
        state = self._runtime_state()
        pid = int(state["pid"])
        start = state.get("start_time")
        if pid == self.old_pid and start == self.old_start_time:
            raise GuardError("candidate runtime identity did not change")
        contains = self.plan.get("candidate_runtime_argv_contains", [])
        if not isinstance(contains, list) or not contains:
            raise GuardError("candidate_runtime_argv_contains proof is required")
        cmdline = _process_cmdline(pid)
        missing = [token for token in contains if str(token) not in cmdline]
        if missing:
            raise GuardError(f"candidate runtime argv missing tokens: {missing}")
        return {"pid": pid, "start_time": start, "argv_contains": contains}

    def _wait_for_proofs(self, key: str, timeout_seconds: float) -> list[dict[str, Any]]:
        proofs = self.plan.get(key)
        if not isinstance(proofs, list) or not proofs:
            raise GuardError(f"{key} must contain explicit health/ownership proofs")
        deadline = self.ops.now() + timeout_seconds
        last_error = "proofs not run"
        while self.ops.now() < deadline:
            try:
                evidence: list[dict[str, Any]] = []
                if key == "success_proofs":
                    evidence.append({"runtime_identity": self._runtime_identity_proof()})
                for proof in proofs:
                    if not isinstance(proof, dict):
                        raise GuardError(f"malformed {key} entry")
                    evidence.append(self._check_proof(proof))
                return evidence
            except GuardError as exc:
                last_error = str(exc)
            self.ops.sleep(float(self.plan.get("poll_seconds", 1.0)))
        raise GuardError(f"{key} deadline exceeded: {last_error}")

    def _execute_artifact(self, name: str, spec: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        timeout = float(spec.get("timeout_seconds", 120.0))
        result = self.ops.run(self._artifact_argv(spec), timeout)
        self.receipt.event(
            f"{name}_command",
            returncode=result.returncode,
            stdout=result.stdout[-4000:],
            stderr=result.stderr[-4000:],
        )
        return result

    def _rollback(self, rollback: dict[str, Any], trigger: str) -> int:
        self._clear_owned_drain()
        self.receipt.event("rollback_started", trigger=trigger)
        try:
            self._verify_artifact("rollback")
            result = self._execute_artifact("rollback", rollback)
            if result.returncode != 0:
                raise GuardError(f"rollback command exited {result.returncode}")
            evidence = self._wait_for_proofs(
                "rollback_proofs", float(self.plan.get("rollback_deadline_seconds", 120.0))
            )
        except Exception as exc:
            self.receipt.final("rollback_failed", trigger=trigger, error=str(exc))
            return 2
        self.receipt.final("rolled_back", trigger=trigger, proofs=evidence)
        return 1

    def run(self) -> int:
        try:
            candidate = self._verify_artifact("candidate")
            rollback = self._verify_artifact("rollback")
            state = self._runtime_state()
            self.old_pid = int(state["pid"])
            self.old_start_time = state.get("start_time")
            self.old_service_processes = self._service_processes(self.old_pid)
            self._write_owned_drain(self.old_pid)
            self.receipt.event(
                "armed",
                supervisor_pid=os.getpid(),
                old_gateway_pid=self.old_pid,
                old_gateway_start_time=self.old_start_time,
                old_gateway_cgroup_processes=self.old_service_processes,
                candidate_sha256=candidate["sha256"],
                rollback_sha256=rollback["sha256"],
            )
            drain_evidence = self._wait_for_drain()
            self.receipt.event("drain_proved", evidence=drain_evidence)
            result = self._execute_artifact("candidate", candidate)
            if result.returncode != 0:
                raise GuardError(f"candidate activation exited {result.returncode}")
            # The candidate command runs with this Unix user's authority. Re-hash
            # the sealed artifact before trusting any proof it participates in.
            self._verify_artifact("candidate")
            evidence = self._wait_for_proofs(
                "success_proofs", float(self.plan.get("readiness_deadline_seconds", 120.0))
            )
        except Exception as exc:
            rollback = self.plan.get("rollback")
            if not isinstance(rollback, dict):
                self.receipt.final("rollback_unavailable", error=str(exc))
                return 2
            return self._rollback(rollback, str(exc))
        self._clear_owned_drain()
        self.receipt.final("activated", proofs=evidence)
        return 0


def _validate_source_plan(plan: dict[str, Any]) -> None:
    required = {
        "version",
        "run_id",
        "state_path",
        "drain_path",
        "state_db_path",
        "active_sessions_path",
        "gateway_unit",
        "candidate",
        "rollback",
        "success_proofs",
        "rollback_proofs",
        "candidate_runtime_argv_contains",
    }
    missing = sorted(required - plan.keys())
    if missing:
        raise GuardError(f"plan missing required fields: {missing}")
    if plan.get("version") != PLAN_VERSION:
        raise GuardError(f"unsupported plan version: {plan.get('version')!r}")
    run_id = str(plan.get("run_id") or "")
    if not run_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in run_id):
        raise GuardError("run_id must contain only letters, digits, '-' or '_'")
    _validate_proof_contract(plan)


def _validate_proof_contract(plan: dict[str, Any]) -> None:
    """Require explicit disarm and rollback proofs, not merely a non-empty list."""
    required_roles = {
        "success_proofs": {
            "health",
            "candidate_runtime",
            "notifier_owner",
            "dispatcher_owner",
        },
        "rollback_proofs": {"health", "prior_runtime", "gateway_service"},
    }
    if plan.get("dashboard_unit"):
        required_roles["rollback_proofs"].add("dashboard_service")
    for key, required in required_roles.items():
        proofs = plan.get(key)
        if not isinstance(proofs, list):
            raise GuardError(f"{key} must be a list")
        roles = {
            str(proof.get("role") or "")
            for proof in proofs
            if isinstance(proof, dict)
        }
        missing = sorted(required - roles)
        if missing:
            raise GuardError(f"{key} missing mandatory proof roles: {missing}")
        for proof in proofs:
            if not isinstance(proof, dict) or proof.get("role") not in required:
                continue
            kind = proof.get("type")
            if kind in {"http_json", "json_file"}:
                expected = proof.get("expected")
                if not isinstance(expected, dict) or not expected:
                    raise GuardError(
                        f"{key} role {proof.get('role')} requires non-empty exact expectations"
                    )
            elif kind == "command_text":
                if not isinstance(proof.get("expected_stdout"), str) or not proof.get(
                    "expected_stdout"
                ):
                    raise GuardError(
                        f"{key} role {proof.get('role')} requires exact expected_stdout"
                    )
            else:
                raise GuardError(
                    f"{key} role {proof.get('role')} uses unsupported proof type {kind!r}"
                )
        health = next(
            proof
            for proof in proofs
            if isinstance(proof, dict) and proof.get("role") == "health"
        )
        parsed = urllib.parse.urlparse(str(health.get("url") or ""))
        if (
            health.get("type") != "http_json"
            or parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != 8642
            or parsed.path not in {"/health", "/health/detailed"}
        ):
            raise GuardError(
                f"{key} health proof must probe http://127.0.0.1:8642/health[/detailed]"
            )


def _copy_artifact(spec: dict[str, Any], destination: Path, name: str) -> dict[str, Any]:
    source = Path(str(spec.get("artifact", ""))).resolve()
    expected = str(spec.get("sha256", ""))
    if not source.is_file() or _sha256(source) != expected:
        raise GuardError(f"source {name} artifact missing or hash mismatch")
    target = destination / f"{name}-{expected[:16]}{source.suffix}"
    shutil.copyfile(source, target)
    os.chmod(target, 0o500)
    if _sha256(target) != expected:
        raise GuardError(f"copied {name} artifact hash mismatch")
    copied = dict(spec)
    copied["artifact"] = str(target)
    return copied


def _seal_proofs(
    proofs: Any, *, candidate_artifact: str, rollback_artifact: str
) -> Any:
    """Bind command proofs to the immutable per-run artifact copies."""
    if isinstance(proofs, list):
        return [
            _seal_proofs(
                item,
                candidate_artifact=candidate_artifact,
                rollback_artifact=rollback_artifact,
            )
            for item in proofs
        ]
    if isinstance(proofs, dict):
        return {
            key: _seal_proofs(
                value,
                candidate_artifact=candidate_artifact,
                rollback_artifact=rollback_artifact,
            )
            for key, value in proofs.items()
        }
    if isinstance(proofs, str):
        return proofs.replace("{candidate_artifact}", candidate_artifact).replace(
            "{rollback_artifact}", rollback_artifact
        )
    return proofs


def arm(plan_path: Path, *, launch: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None) -> Path:
    source = _read_json(plan_path.resolve())
    _validate_source_plan(source)
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).resolve()
    run_dir = home / "recovery-guard" / str(source["run_id"])
    run_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(run_dir, 0o700)
    snapshot = dict(source)
    snapshot["owner_token"] = uuid.uuid4().hex
    snapshot["candidate"] = _copy_artifact(source["candidate"], run_dir, "candidate")
    snapshot["rollback"] = _copy_artifact(source["rollback"], run_dir, "rollback")
    for proof_key in ("success_proofs", "rollback_proofs"):
        snapshot[proof_key] = _seal_proofs(
            source[proof_key],
            candidate_artifact=snapshot["candidate"]["artifact"],
            rollback_artifact=snapshot["rollback"]["artifact"],
        )
    guard_copy = run_dir / "recovery_guard.py"
    shutil.copyfile(Path(__file__).resolve(), guard_copy)
    os.chmod(guard_copy, 0o500)
    snapshot["guard_sha256"] = _sha256(guard_copy)
    snapshot_path = run_dir / "plan.json"
    _atomic_json(snapshot_path, snapshot)
    os.chmod(snapshot_path, 0o400)

    unit = f"hermes-recovery-{source['run_id']}.service"
    argv = [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        "--collect",
        "--service-type=exec",
        sys.executable,
        str(guard_copy),
        "supervise",
        "--plan",
        str(snapshot_path),
    ]
    launcher = launch or (lambda command: subprocess.run(command, text=True, capture_output=True, check=False))
    result = launcher(argv)
    if result.returncode != 0:
        raise GuardError(f"failed to launch out-of-process supervisor: {result.stderr.strip()}")
    deadline = time.time() + 10.0
    events_path = run_dir / "events.jsonl"
    while time.time() < deadline:
        if events_path.exists() and '"phase": "armed"' in events_path.read_text(encoding="utf-8"):
            return run_dir
        result_path = run_dir / "result.json"
        if result_path.exists():
            raise GuardError(f"supervisor failed before arming: {result_path.read_text(encoding='utf-8')}")
        time.sleep(0.1)
    raise GuardError(f"supervisor did not emit armed receipt within 10s; inspect {run_dir}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    arm_parser = subparsers.add_parser("arm", help="seal artifacts and launch the systemd-user supervisor")
    arm_parser.add_argument("--plan", required=True, type=Path)
    supervise_parser = subparsers.add_parser("supervise", help=argparse.SUPPRESS)
    supervise_parser.add_argument("--plan", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "arm":
            run_dir = arm(args.plan)
            print(f"Recovery guard armed: {run_dir}")
            return 0
        return RecoverySupervisor(args.plan).run()
    except GuardError as exc:
        print(f"recovery guard error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
