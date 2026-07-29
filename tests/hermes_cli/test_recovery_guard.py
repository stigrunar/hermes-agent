from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

from hermes_cli import recovery_guard
from hermes_cli.recovery_guard import GuardError, GuardOps, RecoverySupervisor, arm


class FakeClock:
    def __init__(self, value: float = 1_800_000_000.0):
        self.value = value
        self.on_sleep: Callable[[], None] | None = None

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += max(seconds, 0.01)
        if self.on_sleep:
            self.on_sleep()


class TestSupervisor(RecoverySupervisor):
    __test__ = False

    def _service_pids(self, expected_main_pid: int) -> list[int]:
        return [expected_main_pid]


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _runtime(pid: int, start: int, clock: FakeClock, state: str = "running") -> dict:
    return {
        "pid": pid,
        "start_time": start,
        "argv": ["hermes", "gateway", "run"],
        "gateway_state": state,
        "drain_quiesced": state == "draining",
        "active_agents": 0,
        "active_cron_jobs": 0,
        "active_api_runs": 0,
        "active_work": 0,
        "updated_at": datetime.fromtimestamp(clock.now(), timezone.utc).isoformat(),
    }


def _artifact(path: Path, text: str) -> dict:
    path.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "artifact": str(path),
        "sha256": digest,
        "argv": ["{artifact}"],
        "timeout_seconds": 10,
    }


def _make_plan(tmp_path: Path, clock: FakeClock) -> tuple[Path, Path, Path, dict]:
    state_path = tmp_path / "gateway_state.json"
    sessions_path = tmp_path / "runtime" / "active_sessions.json"
    db_path = tmp_path / "state.db"
    _write_json(state_path, _runtime(111, 1, clock))
    _write_json(sessions_path, {"entries": []})
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE compression_locks (session_id TEXT, holder TEXT, acquired_at REAL, expires_at REAL)"
        )

    plan = {
        "version": 1,
        "run_id": "test-guard",
        "owner_token": "owner-token",
        "state_path": str(state_path),
        "drain_path": str(tmp_path / ".drain_request.json"),
        "state_db_path": str(db_path),
        "active_sessions_path": str(sessions_path),
        "gateway_unit": "hermes-gateway.service",
        "freshness_seconds": 10,
        "poll_seconds": 1,
        "drain_timeout_seconds": 5,
        "readiness_deadline_seconds": 3,
        "rollback_deadline_seconds": 3,
        "candidate_runtime_argv_contains": ["candidate-runtime"],
        "candidate": _artifact(tmp_path / "candidate.sh", "candidate"),
        "rollback": _artifact(tmp_path / "rollback.sh", "rollback"),
        "success_proofs": [
            {
                "type": "http_json",
                "role": "health",
                "url": "http://127.0.0.1:8642/health",
                "expected": {"status": "ok"},
            },
            {
                "type": "json_file",
                "role": "candidate_runtime",
                "path": str(state_path),
                "expected": {"gateway_state": "running", "start_time": 2},
            },
            {
                "type": "command_text",
                "role": "notifier_owner",
                "argv": ["notifier-proof"],
                "expected_stdout": "external",
            },
            {
                "type": "command_text",
                "role": "dispatcher_owner",
                "argv": ["dispatcher-proof"],
                "expected_stdout": "external",
            },
        ],
        "rollback_proofs": [
            {
                "type": "http_json",
                "role": "health",
                "url": "http://127.0.0.1:8642/health",
                "expected": {"status": "ok", "runtime": "prior"},
            },
            {
                "type": "json_file",
                "role": "prior_runtime",
                "path": str(state_path),
                "expected": {"gateway_state": "running"},
            },
            {
                "type": "command_text",
                "role": "gateway_service",
                "argv": ["gateway-service-proof"],
                "expected_stdout": "active",
            },
        ],
    }
    plan_path = tmp_path / "plan.json"
    _write_json(plan_path, plan)
    return plan_path, state_path, sessions_path, plan


def _patch_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recovery_guard, "_pid_alive", lambda pid: pid in {111, 222, 333})
    monkeypatch.setattr(recovery_guard, "_process_start_time", lambda pid: {111: 1, 222: 2, 333: 3}.get(pid))
    monkeypatch.setattr(
        recovery_guard,
        "_process_cmdline",
        lambda pid: "python candidate-runtime gateway run" if pid == 222 else "python prior-runtime gateway run",
    )


def test_success_path_disarms_only_after_identity_health_and_ownership_proofs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, state_path, _, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)

    def on_sleep() -> None:
        if Path(tmp_path / ".drain_request.json").exists():
            _write_json(state_path, _runtime(111, 1, clock, "draining"))

    clock.on_sleep = on_sleep

    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv == [str(tmp_path / "candidate.sh")]:
            _write_json(state_path, _runtime(222, 2, clock))
            return subprocess.CompletedProcess(argv, 0, "candidate activated", "")
        if argv in (["notifier-proof"], ["dispatcher-proof"]):
            return subprocess.CompletedProcess(argv, 0, "external\n", "")
        raise AssertionError(f"unexpected command: {argv}")

    ops = GuardOps(now=clock.now, sleep=clock.sleep, run=run, http_json=lambda url, timeout: {"status": "ok"})
    supervisor = TestSupervisor(plan_path, ops=ops)

    assert supervisor.run() == 0
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["outcome"] == "activated"
    assert not (tmp_path / ".drain_request.json").exists()
    events = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert events.index('"phase": "armed"') < events.index('"phase": "drain_proved"')
    assert {
        "type": "command_text",
        "role": "notifier_owner",
        "argv": ["notifier-proof"],
    } in result["proofs"]


def test_candidate_readiness_timeout_runs_verified_rollback_and_probes_prior_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, state_path, _, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)

    def on_sleep() -> None:
        if Path(tmp_path / ".drain_request.json").exists():
            current = json.loads(state_path.read_text(encoding="utf-8"))
            if current["pid"] == 111:
                _write_json(state_path, _runtime(111, 1, clock, "draining"))

    clock.on_sleep = on_sleep

    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv == [str(tmp_path / "candidate.sh")]:
            _write_json(state_path, _runtime(222, 2, clock))
            return subprocess.CompletedProcess(argv, 0, "candidate activated", "")
        if argv == [str(tmp_path / "rollback.sh")]:
            _write_json(state_path, _runtime(333, 3, clock))
            return subprocess.CompletedProcess(argv, 0, "prior runtime restored", "")
        if argv in (["notifier-proof"], ["dispatcher-proof"]):
            return subprocess.CompletedProcess(argv, 0, "external\n", "")
        if argv == ["gateway-service-proof"]:
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        raise AssertionError(f"unexpected command: {argv}")

    def http_json(url: str, timeout: float) -> dict:
        if json.loads(state_path.read_text(encoding="utf-8"))["pid"] == 333:
            return {"status": "ok", "runtime": "prior"}
        raise GuardError("candidate health unavailable")

    supervisor = TestSupervisor(
        plan_path,
        ops=GuardOps(now=clock.now, sleep=clock.sleep, run=run, http_json=http_json),
    )

    assert supervisor.run() == 1
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["outcome"] == "rolled_back"
    assert "success_proofs deadline exceeded" in result["trigger"]
    assert not (tmp_path / ".drain_request.json").exists()
    assert '"phase": "rollback_started"' in (tmp_path / "events.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("rollback_code", "expected_status", "expected_outcome"),
    [(0, 1, "rolled_back"), (23, 2, "rollback_failed")],
    ids=["failed-candidate-restores-prior", "failed-rollback-escalates"],
)
def test_failed_candidate_uses_rollback_and_escalates_if_restore_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rollback_code: int,
    expected_status: int,
    expected_outcome: str,
) -> None:
    clock = FakeClock()
    plan_path, state_path, _, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)

    def on_sleep() -> None:
        if Path(tmp_path / ".drain_request.json").exists():
            _write_json(state_path, _runtime(111, 1, clock, "draining"))

    clock.on_sleep = on_sleep

    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv == [str(tmp_path / "candidate.sh")]:
            return subprocess.CompletedProcess(argv, 17, "", "candidate failed")
        if argv == [str(tmp_path / "rollback.sh")]:
            if rollback_code == 0:
                _write_json(state_path, _runtime(333, 3, clock))
            return subprocess.CompletedProcess(argv, rollback_code, "restore", "")
        if argv == ["gateway-service-proof"]:
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        raise AssertionError(f"unexpected command: {argv}")

    supervisor = TestSupervisor(
        plan_path,
        ops=GuardOps(
            now=clock.now,
            sleep=clock.sleep,
            run=run,
            http_json=lambda url, timeout: {"status": "ok", "runtime": "prior"},
        ),
    )

    assert supervisor.run() == expected_status
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["outcome"] == expected_outcome
    assert "candidate activation exited 17" in result["trigger"]
    assert not (tmp_path / ".drain_request.json").exists()


def test_drain_rejects_state_zero_when_compression_lock_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, state_path, _, plan = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)
    _write_json(state_path, _runtime(111, 1, clock, "draining"))
    with sqlite3.connect(plan["state_db_path"]) as connection:
        connection.execute(
            "INSERT INTO compression_locks VALUES (?, ?, ?, ?)",
            ("session", "compressor", clock.now(), clock.now() + 60),
        )

    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor.old_pid = 111
    supervisor.old_start_time = 1

    with pytest.raises(GuardError, match="compression_locks=1"):
        supervisor._drain_is_safe()


def test_drain_rejects_stale_state_even_when_all_counters_are_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, state_path, _, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)
    _write_json(state_path, _runtime(111, 1, clock, "draining"))
    clock.value += 11

    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor.old_pid = 111
    supervisor.old_start_time = 1

    with pytest.raises(GuardError, match="runtime state is stale"):
        supervisor._drain_is_safe()


def test_drain_rejects_counter_zero_when_live_session_evidence_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, state_path, sessions_path, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)
    _write_json(state_path, _runtime(111, 1, clock, "draining"))
    sessions_path.unlink()

    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor.old_pid = 111
    supervisor.old_start_time = 1

    with pytest.raises(GuardError, match="active session registry is unavailable"):
        supervisor._drain_is_safe()


def test_arm_seals_guard_and_artifacts_before_systemd_user_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, _, _, plan = _make_plan(tmp_path, clock)
    plan.pop("owner_token")
    plan["success_proofs"][2]["argv"] = ["{candidate_artifact}", "prove-notifier"]
    plan["rollback_proofs"][2]["argv"] = ["{rollback_artifact}", "prove-service"]
    _write_json(plan_path, plan)
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    launched: list[str] = []

    def launch(argv: list[str]) -> subprocess.CompletedProcess[str]:
        launched.extend(argv)
        run_dir = home / "recovery-guard" / "test-guard"
        (run_dir / "events.jsonl").write_text('{"phase": "armed"}\n', encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    run_dir = arm(plan_path, launch=launch)
    sealed = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))

    assert launched[:2] == ["systemd-run", "--user"]
    assert "--service-type=exec" in launched
    assert Path(sealed["candidate"]["artifact"]).parent == run_dir
    assert Path(sealed["rollback"]["artifact"]).parent == run_dir
    assert sealed["success_proofs"][2]["argv"][0] == sealed["candidate"]["artifact"]
    assert sealed["rollback_proofs"][2]["argv"][0] == sealed["rollback"]["artifact"]
    assert hashlib.sha256(Path(sealed["rollback"]["artifact"]).read_bytes()).hexdigest() == sealed["rollback"]["sha256"]
    assert (run_dir / "recovery_guard.py").is_file()


def test_plan_cannot_disarm_without_explicit_ownership_proof_roles(tmp_path: Path) -> None:
    clock = FakeClock()
    plan_path, _, _, plan = _make_plan(tmp_path, clock)
    plan["success_proofs"] = [
        proof for proof in plan["success_proofs"] if proof["role"] != "dispatcher_owner"
    ]
    _write_json(plan_path, plan)

    with pytest.raises(GuardError, match="dispatcher_owner"):
        RecoverySupervisor(plan_path)
