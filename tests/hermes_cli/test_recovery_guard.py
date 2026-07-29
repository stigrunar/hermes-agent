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

    def _legacy_incumbent_proof(self) -> dict:
        return {
            "source": self.plan["legacy_incumbent_identity"],
            "runtime": {"pid": self.old_pid, "start_time": self.old_start_time},
            "proof": {"type": "command_text", "role": "legacy_incumbent"},
        }


class CgroupSupervisor(RecoverySupervisor):
    __test__ = False

    service_pids: list[int]

    def _service_pids(self, expected_main_pid: int) -> list[int]:
        return self.service_pids

    def _legacy_incumbent_proof(self) -> dict:
        return {
            "source": self.plan["legacy_incumbent_identity"],
            "runtime": {"pid": self.old_pid, "start_time": self.old_start_time},
            "proof": {"type": "command_text", "role": "legacy_incumbent"},
        }


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _runtime(pid: int, start: int, clock: FakeClock, state: str = "running") -> dict:
    runtime = {
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
    if pid == 222:
        runtime.update({"source_commit": "c" * 40, "source_tree": "d" * 40})
    return runtime


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

    candidate = _artifact(tmp_path / "candidate.sh", "candidate")
    rollback = _artifact(tmp_path / "rollback.sh", "rollback")
    legacy_identity = {
        "commit": recovery_guard.LEGACY_INCUMBENT_COMMIT,
        "tree": recovery_guard.LEGACY_INCUMBENT_TREE,
    }
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
        "legacy_incumbent_identity": legacy_identity,
        "candidate_identity": {"commit": "c" * 40, "tree": "d" * 40},
        "legacy_incumbent_proof": {
            "type": "command_text",
            "role": "legacy_incumbent",
            "argv": [
                rollback["artifact"],
                "prove-legacy-incumbent",
                "{runtime_pid}",
                "{runtime_start_time}",
            ],
            "expected_stdout": (
                f"commit={legacy_identity['commit']}\ntree={legacy_identity['tree']}"
            ),
        },
        "candidate": candidate,
        "rollback": rollback,
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
                "expected": {
                    "gateway_state": "running",
                    "start_time": 2,
                    "source_commit": "c" * 40,
                    "source_tree": "d" * 40,
                },
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
    assert events.index('"phase": "drain_proved"') < events.index(
        '"phase": "evidence_phase_transition"'
    )
    assert events.index('"phase": "evidence_phase_transition"') < events.index(
        '"phase": "candidate_command"'
    )
    assert '"phase": "candidate_disarm_proved"' in events
    assert result["evidence_phase"] == recovery_guard.CANDIDATE_PHASE
    assert result["candidate_identity"] == {"commit": "c" * 40, "tree": "d" * 40}
    assert {
        "type": "command_text",
        "role": "notifier_owner",
        "argv": ["notifier-proof"],
    } in result["proofs"]


def test_stale_healthy_incumbent_fails_before_arm_without_invoking_service_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, _, _, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)
    clock.value += 11
    commands: list[list[str]] = []

    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    supervisor = TestSupervisor(
        plan_path,
        ops=GuardOps(now=clock.now, sleep=clock.sleep, run=run),
    )

    assert supervisor.run() == 2
    assert commands == []
    assert not (tmp_path / ".drain_request.json").exists()
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["outcome"] == "prearm_failed"
    assert "runtime state is stale" in result["error"]
    events = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert '"phase": "armed"' not in events
    assert '"phase": "rollback_started"' not in events
    assert '"phase": "candidate_command"' not in events
    assert '"phase": "rollback_command"' not in events


def test_drain_timeout_after_durable_arm_runs_verified_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, state_path, _, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)
    commands: list[list[str]] = []

    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        if argv == [str(tmp_path / "rollback.sh")]:
            _write_json(state_path, _runtime(333, 3, clock))
            return subprocess.CompletedProcess(argv, 0, "prior runtime restored", "")
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

    assert supervisor.run() == 1
    assert [str(tmp_path / "candidate.sh")] not in commands
    assert [str(tmp_path / "rollback.sh")] in commands
    assert not (tmp_path / ".drain_request.json").exists()
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["outcome"] == "rolled_back"
    assert "drain deadline exceeded" in result["trigger"]
    events = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert events.index('"phase": "armed"') < events.index('"phase": "rollback_started"')


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
    assert result["evidence_phase"] == recovery_guard.CANDIDATE_PHASE
    assert result["legacy_incumbent_identity"]["commit"] == (
        recovery_guard.LEGACY_INCUMBENT_COMMIT
    )
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
    assert result["candidate_identity"] == {"commit": "c" * 40, "tree": "d" * 40}
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
    supervisor.old_service_processes = {111: 1}

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
    supervisor.old_service_processes = {111: 1}

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
    supervisor.old_service_processes = {111: 1}
    supervisor.evidence_phase = recovery_guard.CANDIDATE_PHASE

    with pytest.raises(GuardError, match="active session registry is unavailable"):
        supervisor._drain_is_safe()


def test_exact_quiet_legacy_incumbent_can_prove_drain_without_registry(
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
    supervisor.old_service_processes = {111: 1}

    evidence = supervisor._drain_is_safe()

    assert evidence["active_session_registry"] == "legacy_absent"
    assert evidence["legacy_incumbent"]["source"] == {
        "commit": recovery_guard.LEGACY_INCUMBENT_COMMIT,
        "tree": recovery_guard.LEGACY_INCUMBENT_TREE,
    }
    assert evidence["split_work"] == {
        "active_agents": 0,
        "active_cron_jobs": 0,
        "active_api_runs": 0,
        "active_work": 0,
    }


def test_legacy_proof_binds_pinned_source_to_observed_pid_and_start_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, _, _, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)
    observed_argv: list[str] = []

    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        observed_argv[:] = argv
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                f"commit={recovery_guard.LEGACY_INCUMBENT_COMMIT}\n"
                f"tree={recovery_guard.LEGACY_INCUMBENT_TREE}\n"
                "pid=111\nstart_time=1\n"
            ),
            stderr="",
        )

    supervisor = RecoverySupervisor(
        plan_path,
        ops=GuardOps(now=clock.now, sleep=clock.sleep, run=run),
    )
    evidence = supervisor._legacy_incumbent_proof()

    assert observed_argv[-2:] == ["111", "1"]
    assert evidence["runtime"] == {"pid": 111, "start_time": 1}


def test_legacy_proof_rejects_source_identity_mismatch_for_observed_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, _, _, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)

    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                f"commit={'a' * 40}\n"
                f"tree={recovery_guard.LEGACY_INCUMBENT_TREE}\n"
                "pid=111\nstart_time=1\n"
            ),
            stderr="",
        )

    supervisor = RecoverySupervisor(
        plan_path,
        ops=GuardOps(now=clock.now, sleep=clock.sleep, run=run),
    )

    with pytest.raises(GuardError, match="not bound to the observed live runtime identity"):
        supervisor._legacy_incumbent_proof()


@pytest.mark.parametrize(
    "field",
    ["active_agents", "active_cron_jobs", "active_api_runs"],
    ids=["model-or-tool-turn", "cron-work", "api-work"],
)
def test_legacy_drain_rejects_each_split_counter_work_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    clock = FakeClock()
    plan_path, state_path, sessions_path, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)
    state = _runtime(111, 1, clock, "draining")
    state[field] = 1
    state["active_work"] = 1
    _write_json(state_path, state)
    sessions_path.unlink()
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor.old_pid = 111
    supervisor.old_start_time = 1
    supervisor.old_service_processes = {111: 1}

    with pytest.raises(GuardError, match="proof mismatch|active work"):
        supervisor._drain_is_safe()


def test_candidate_phase_requires_registry_and_rejects_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, _, sessions_path, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor._transition_to_candidate()

    sessions_path.unlink()
    with pytest.raises(GuardError, match="registry is unavailable"):
        supervisor._candidate_session_evidence()

    sessions_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(GuardError, match="cannot read JSON"):
        supervisor._candidate_session_evidence()


def test_candidate_phase_is_reloaded_and_remains_strict_after_reconstruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, _, sessions_path, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)
    first = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    first._transition_to_candidate()

    reconstructed = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    assert reconstructed.evidence_phase == recovery_guard.CANDIDATE_PHASE
    sessions_path.unlink()
    with pytest.raises(GuardError, match="registry is unavailable"):
        reconstructed._candidate_session_evidence()


def test_candidate_phase_without_transition_receipt_fails_closed_on_reconstruction(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    plan_path, _, _, _ = _make_plan(tmp_path, clock)
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor._transition_to_candidate()
    supervisor.receipt.events_path.unlink()

    with pytest.raises(GuardError, match="requires exactly one durable transition"):
        TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("not-json\n", "cannot recover durable evidence phase"),
        ('{"phase": "evidence_phase_transition"}\n', "transition mismatch contract"),
    ],
    ids=["malformed-json", "phase-only"],
)
def test_candidate_phase_rejects_malformed_transition_receipt(
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    clock = FakeClock()
    plan_path, _, _, _ = _make_plan(tmp_path, clock)
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor._transition_to_candidate()
    supervisor.receipt.events_path.write_text(contents, encoding="utf-8")

    with pytest.raises(GuardError, match=message):
        TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))


def test_candidate_phase_rejects_duplicate_transition_receipts(tmp_path: Path) -> None:
    clock = FakeClock()
    plan_path, _, _, _ = _make_plan(tmp_path, clock)
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor._transition_to_candidate()
    event = supervisor.receipt.events_path.read_text(encoding="utf-8")
    supervisor.receipt.events_path.write_text(event + event, encoding="utf-8")

    with pytest.raises(GuardError, match="requires exactly one durable transition"):
        TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contract", "wrong-contract"),
        ("run_id", "wrong-run"),
        ("legacy_incumbent_identity", {"commit": "a" * 40, "tree": "b" * 40}),
        ("candidate_identity", {"commit": "e" * 40, "tree": "f" * 40}),
        ("from_phase", recovery_guard.CANDIDATE_PHASE),
        ("to_phase", recovery_guard.LEGACY_INCUMBENT_PHASE),
        ("active_session_registry", False),
    ],
    ids=[
        "contract",
        "run-id",
        "legacy-identity",
        "candidate-identity",
        "from-phase",
        "to-phase",
        "false-registry-marker",
    ],
)
def test_candidate_phase_rejects_transition_contract_mismatch(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    clock = FakeClock()
    plan_path, _, _, _ = _make_plan(tmp_path, clock)
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor._transition_to_candidate()
    event = json.loads(supervisor.receipt.events_path.read_text(encoding="utf-8"))
    event[field] = value
    supervisor.receipt.events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    with pytest.raises(GuardError, match=f"transition mismatch {field}"):
        TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))


def test_candidate_phase_rejects_transition_without_registry_marker(tmp_path: Path) -> None:
    clock = FakeClock()
    plan_path, _, _, _ = _make_plan(tmp_path, clock)
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor._transition_to_candidate()
    event = json.loads(supervisor.receipt.events_path.read_text(encoding="utf-8"))
    event.pop("active_session_registry")
    supervisor.receipt.events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    with pytest.raises(GuardError, match="transition mismatch active_session_registry"):
        TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))


def test_transition_receipt_is_rejected_while_durable_phase_is_legacy(tmp_path: Path) -> None:
    clock = FakeClock()
    plan_path, _, _, _ = _make_plan(tmp_path, clock)
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor._transition_to_candidate()
    _write_json(
        supervisor.phase_path,
        supervisor._phase_payload(recovery_guard.LEGACY_INCUMBENT_PHASE),
    )

    with pytest.raises(GuardError, match="transition requires durable candidate phase"):
        TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))


def test_transition_receipt_without_durable_phase_fails_closed_on_reconstruction(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    plan_path, _, _, _ = _make_plan(tmp_path, clock)
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor._transition_to_candidate()
    supervisor.phase_path.unlink()

    with pytest.raises(GuardError, match="phase is missing after transition"):
        TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))

    _write_json(
        supervisor.phase_path,
        supervisor._phase_payload(recovery_guard.LEGACY_INCUMBENT_PHASE),
    )
    with pytest.raises(GuardError, match="phase regressed after transition"):
        TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("not-json", "cannot read JSON"),
        ('{"entries": {}}', "active session registry is malformed"),
        ('{"entries": [null]}', "contains malformed entry"),
        ('{"entries": [{"pid": 123}]}', "uninspectable lease identity"),
    ],
)
def test_drain_rejects_corrupt_or_malformed_active_session_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: str,
    message: str,
) -> None:
    clock = FakeClock()
    plan_path, state_path, sessions_path, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)
    _write_json(state_path, _runtime(111, 1, clock, "draining"))
    sessions_path.write_text(contents, encoding="utf-8")
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor.old_pid = 111
    supervisor.old_start_time = 1
    supervisor.old_service_processes = {111: 1}

    with pytest.raises(GuardError, match=message):
        supervisor._drain_is_safe()


def test_drain_rejects_live_inflight_session_and_stale_or_foreign_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, state_path, sessions_path, _ = _make_plan(tmp_path, clock)
    _write_json(state_path, _runtime(111, 1, clock, "draining"))
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor.old_pid = 111
    supervisor.old_start_time = 1
    supervisor.old_service_processes = {111: 1}
    monkeypatch.setattr(recovery_guard, "_pid_alive", lambda pid: pid in {111, 222})
    monkeypatch.setattr(recovery_guard, "_process_start_time", lambda pid: 22 if pid == 222 else 1)

    entry = {
        "lease_id": "live-lease",
        "session_id": "in-flight",
        "surface": "gateway:telegram",
        "pid": 222,
        "process_start_ticks": 22,
        "started_at": clock.now(),
        "updated_at": clock.now(),
    }
    _write_json(sessions_path, {"entries": [entry]})
    with pytest.raises(GuardError, match="sessions=1"):
        supervisor._drain_is_safe()

    entry["process_start_ticks"] = 21
    _write_json(sessions_path, {"entries": [entry]})
    with pytest.raises(GuardError, match="process identity"):
        supervisor._drain_is_safe()

    monkeypatch.setattr(recovery_guard, "_pid_alive", lambda pid: pid == 111)
    _write_json(sessions_path, {"entries": [entry]})
    with pytest.raises(GuardError, match="stale active session lease"):
        supervisor._drain_is_safe()


@pytest.mark.parametrize(
    "start_time",
    [None, 0, -1, True, "1", 1.5],
    ids=["missing", "zero", "negative", "bool", "string", "float"],
)
def test_runtime_state_rejects_missing_or_invalid_persisted_start_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_time: object,
) -> None:
    clock = FakeClock()
    plan_path, state_path, _, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)
    state = _runtime(111, 1, clock)
    if start_time is None:
        state.pop("start_time")
    else:
        state["start_time"] = start_time
    _write_json(state_path, state)

    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))

    with pytest.raises(GuardError, match="valid positive start_time"):
        supervisor._runtime_state()


@pytest.mark.parametrize(
    ("live_start_time", "message"),
    [(None, "unavailable or invalid"), (0, "unavailable or invalid"), (True, "unavailable or invalid"), (2, "does not match")],
    ids=["unavailable", "zero", "bool", "mismatch"],
)
def test_runtime_state_rejects_unavailable_invalid_or_mismatched_live_start_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_start_time: object,
    message: str,
) -> None:
    clock = FakeClock()
    plan_path, _, _, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)
    monkeypatch.setattr(recovery_guard, "_process_start_time", lambda pid: live_start_time)
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))

    with pytest.raises(GuardError, match=message):
        supervisor._runtime_state()


def test_drain_and_candidate_identity_paths_fail_closed_without_complete_start_time_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, state_path, _, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor.old_pid = 111
    supervisor.old_start_time = 1
    supervisor.old_service_processes = {111: 1}

    drain_state = _runtime(111, 1, clock, "draining")
    drain_state.pop("start_time")
    _write_json(state_path, drain_state)
    with pytest.raises(GuardError, match="valid positive start_time"):
        supervisor._drain_is_safe()

    _write_json(state_path, _runtime(222, 2, clock))
    monkeypatch.setattr(recovery_guard, "_process_start_time", lambda pid: None)
    with pytest.raises(GuardError, match="live start_time is unavailable"):
        supervisor._runtime_identity_proof()


def test_drain_allows_only_unchanged_persistent_gateway_unit_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, state_path, _, _ = _make_plan(tmp_path, clock)
    _write_json(state_path, _runtime(111, 1, clock, "draining"))
    starts = {111: 1, 444: 40, 555: 50}
    monkeypatch.setattr(recovery_guard, "_pid_alive", lambda pid: pid in starts)
    monkeypatch.setattr(recovery_guard, "_process_start_time", starts.get)
    supervisor = CgroupSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor.service_pids = [111, 444, 555]
    supervisor.old_pid = 111
    supervisor.old_start_time = 1
    supervisor.old_service_processes = {111: 1, 444: 40, 555: 50}

    evidence = supervisor._drain_is_safe()

    assert evidence["persistent_cgroup_processes"] == {444: 40, 555: 50}


@pytest.mark.parametrize(
    ("service_pids", "starts", "message"),
    [
        ([111, 444, 666], {111: 1, 444: 40, 666: 60}, "unexpected_cgroup_processes"),
        ([111, 444], {111: 1, 444: 41}, "unexpected_cgroup_processes"),
        ([111, 444], {111: 1, 444: None}, "start_time is unavailable"),
    ],
    ids=["new-descendant", "reused-pid", "unprovable-identity"],
)
def test_drain_rejects_unexpected_or_unprovable_gateway_unit_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service_pids: list[int],
    starts: dict[int, int | None],
    message: str,
) -> None:
    clock = FakeClock()
    plan_path, state_path, _, _ = _make_plan(tmp_path, clock)
    _write_json(state_path, _runtime(111, 1, clock, "draining"))
    monkeypatch.setattr(recovery_guard, "_pid_alive", lambda pid: pid in starts)
    monkeypatch.setattr(recovery_guard, "_process_start_time", starts.get)
    supervisor = CgroupSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor.service_pids = service_pids
    supervisor.old_pid = 111
    supervisor.old_start_time = 1
    supervisor.old_service_processes = {111: 1, 444: 40}

    with pytest.raises(GuardError, match=message):
        supervisor._drain_is_safe()


def test_drain_rejects_main_pid_identity_change_during_cgroup_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, state_path, _, _ = _make_plan(tmp_path, clock)
    _write_json(state_path, _runtime(111, 1, clock, "draining"))
    observed_starts = iter([1, 2])
    monkeypatch.setattr(recovery_guard, "_pid_alive", lambda pid: pid == 111)
    monkeypatch.setattr(recovery_guard, "_process_start_time", lambda pid: next(observed_starts))
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor.old_pid = 111
    supervisor.old_start_time = 1
    supervisor.old_service_processes = {111: 1}

    with pytest.raises(GuardError, match="MainPID identity"):
        supervisor._drain_is_safe()


def test_drain_rejects_missing_pre_drain_cgroup_identity_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, state_path, _, _ = _make_plan(tmp_path, clock)
    _patch_processes(monkeypatch)
    _write_json(state_path, _runtime(111, 1, clock, "draining"))
    supervisor = TestSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor.old_pid = 111
    supervisor.old_start_time = 1

    with pytest.raises(GuardError, match="not captured before drain"):
        supervisor._drain_is_safe()


def test_drain_rejects_active_session_even_when_its_process_identity_is_preexisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, state_path, sessions_path, _ = _make_plan(tmp_path, clock)
    _write_json(state_path, _runtime(111, 1, clock, "draining"))
    _write_json(
        sessions_path,
        {
            "entries": [
                {
                    "lease_id": "active-lease",
                    "session_id": "active",
                    "surface": "gateway:telegram",
                    "pid": 444,
                    "process_start_ticks": 40,
                }
            ]
        },
    )
    starts = {111: 1, 444: 40}
    monkeypatch.setattr(recovery_guard, "_pid_alive", lambda pid: pid in starts)
    monkeypatch.setattr(recovery_guard, "_process_start_time", starts.get)
    supervisor = CgroupSupervisor(plan_path, ops=GuardOps(now=clock.now, sleep=clock.sleep))
    supervisor.service_pids = [111, 444]
    supervisor.old_pid = 111
    supervisor.old_start_time = 1
    supervisor.old_service_processes = starts

    with pytest.raises(GuardError, match="sessions=1"):
        supervisor._drain_is_safe()


def test_arm_seals_guard_and_artifacts_before_systemd_user_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    plan_path, _, _, plan = _make_plan(tmp_path, clock)
    plan.pop("owner_token")
    plan["legacy_incumbent_proof"]["argv"][0] = "{rollback_artifact}"
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
    assert sealed["legacy_incumbent_proof"]["argv"][0] == sealed["rollback"]["artifact"]
    phase = json.loads(
        (run_dir / recovery_guard.EVIDENCE_PHASE_FILE).read_text(encoding="utf-8")
    )
    assert phase["phase"] == recovery_guard.LEGACY_INCUMBENT_PHASE
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


def test_plan_rejects_unpinned_legacy_identity_or_unsealed_identity_proof(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    plan_path, _, _, plan = _make_plan(tmp_path, clock)
    plan["legacy_incumbent_identity"]["commit"] = "a" * 40
    _write_json(plan_path, plan)
    with pytest.raises(GuardError, match="audited pinned baseline"):
        RecoverySupervisor(plan_path)

    plan["legacy_incumbent_identity"]["commit"] = recovery_guard.LEGACY_INCUMBENT_COMMIT
    plan["legacy_incumbent_proof"]["argv"][0] = "/tmp/mutable-helper"
    _write_json(plan_path, plan)
    with pytest.raises(GuardError, match="sealed rollback artifact"):
        RecoverySupervisor(plan_path)

    plan["legacy_incumbent_proof"]["argv"][0] = plan["rollback"]["artifact"]
    plan["legacy_incumbent_proof"]["argv"].remove("{runtime_start_time}")
    _write_json(plan_path, plan)
    with pytest.raises(GuardError, match="observed runtime PID and start time"):
        RecoverySupervisor(plan_path)

    plan["legacy_incumbent_proof"]["argv"].append("{runtime_start_time}")
    plan["success_proofs"][1]["expected"]["source_tree"] = "e" * 40
    _write_json(plan_path, plan)
    with pytest.raises(GuardError, match="not bound to candidate commit/tree"):
        RecoverySupervisor(plan_path)
