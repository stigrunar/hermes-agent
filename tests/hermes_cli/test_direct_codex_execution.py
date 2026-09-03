from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import outcomes_db as odb
from hermes_cli.direct_codex_execution import (
    DirectCodexExecutionError,
    canonical_repo_identity,
    current_base_ref,
    run_direct_codex_execution,
)


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-qm", "seed")
    return repo


def _fake_codex(tmp_path: Path) -> Path:
    exe = tmp_path / "fake-codex"
    exe.write_text(
        """#!/usr/bin/env python3
import pathlib, sys, time
args = sys.argv[1:]
out = pathlib.Path(args[args.index('-o') + 1])
prompt = sys.stdin.read()
time.sleep(0.25)
pathlib.Path('artifact.txt').write_text('changed\\n', encoding='utf-8')
out.write_text('receipt:' + prompt, encoding='utf-8')
""",
        encoding="utf-8",
    )
    exe.chmod(0o755)
    return exe


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(odb, "cross_project_orchestration_enabled", lambda: True)
    conn = odb.connect()
    try:
        oid = odb.create_outcome(conn, project_id="p", outcome_key="O")
        yield conn, oid
    finally:
        conn.close()


def _execution(conn, oid: str, repo: Path, *, eid: str, resources=None) -> str:
    return odb.create_execution(
        conn,
        execution_id=eid,
        project_id="p",
        outcome_id=oid,
        execution_mode="direct_codex",
        owner="default",
        mutating=True,
        repository=canonical_repo_identity(repo),
        mutation_scope=["artifact.txt"],
        base_ref=current_base_ref(repo),
        resource_requirements=resources,
    )


def test_runner_tracks_heartbeats_and_terminal_receipt(store, tmp_path, monkeypatch):
    conn, oid = store
    repo = _repo(tmp_path)
    eid = _execution(conn, oid, repo, eid="ex_direct", resources=["vectorworks-local"])
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("make artifact", encoding="utf-8")
    output = tmp_path / "last.md"
    stderr = tmp_path / "stderr.log"
    fake = _fake_codex(tmp_path)

    calls = {"heartbeat": 0}
    original = odb.heartbeat_execution

    def _heartbeat(*args, **kwargs):
        calls["heartbeat"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(odb, "heartbeat_execution", _heartbeat)
    result = run_direct_codex_execution(
        conn,
        execution_id=eid,
        repo=repo,
        prompt_file=prompt,
        output_file=output,
        stderr_file=stderr,
        codex_executable=str(fake),
        heartbeat_seconds=0.1,
        timeout_seconds=5,
    )

    assert result["ok"] is True
    assert result["returncode"] == 0
    assert calls["heartbeat"] >= 1
    execution = odb.get_execution(conn, eid)
    assert execution is not None
    assert execution["state"] == "completed"
    assert execution["receipt_uri"] == output.resolve().as_uri()
    assert output.read_text(encoding="utf-8") == "receipt:make artifact"
    assert (repo / "artifact.txt").read_text(encoding="utf-8") == "changed\n"
    assert odb.active_mutation_leases(conn) == []
    assert odb.list_resource_leases(conn, active_only=True) == []


def test_resource_wait_never_launches_direct_process(store, tmp_path):
    conn, oid = store
    holder_repo = _repo(tmp_path / "holder")
    waiter_repo = _repo(tmp_path / "waiter")
    holder = odb.create_execution(
        conn,
        execution_id="ex_holder",
        project_id="p",
        outcome_id=oid,
        execution_mode="external",
        owner="dollyqa",
        mutating=False,
        resource_requirements=["vectorworks-local"],
    )
    assert odb.admit_execution(conn, holder)["state"] == "running"
    waiter = _execution(
        conn, oid, waiter_repo, eid="ex_waiter", resources=["vectorworks-local"]
    )
    prompt = tmp_path / "waiter-prompt.txt"
    prompt.write_text("must not launch", encoding="utf-8")
    fake = _fake_codex(tmp_path)

    with pytest.raises(odb.ExecutionAdmissionBlocked, match="waiting_resource"):
        run_direct_codex_execution(
            conn,
            execution_id=waiter,
            repo=waiter_repo,
            prompt_file=prompt,
            output_file=tmp_path / "waiter-last.md",
            stderr_file=tmp_path / "waiter-stderr.log",
            codex_executable=str(fake),
            heartbeat_seconds=0.1,
            timeout_seconds=5,
        )
    assert odb.get_execution(conn, waiter)["state"] == "waiting_resource"
    assert not (waiter_repo / "artifact.txt").exists()


def test_dirty_worktree_is_rejected_before_admission(store, tmp_path):
    conn, oid = store
    repo = _repo(tmp_path)
    eid = _execution(conn, oid, repo, eid="ex_dirty")
    (repo / "unrelated.txt").write_text("dirty", encoding="utf-8")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("nope", encoding="utf-8")

    with pytest.raises(DirectCodexExecutionError, match="clean isolated"):
        run_direct_codex_execution(
            conn,
            execution_id=eid,
            repo=repo,
            prompt_file=prompt,
            output_file=tmp_path / "last.md",
            stderr_file=tmp_path / "stderr.log",
            codex_executable=str(_fake_codex(tmp_path)),
        )
    assert odb.get_execution(conn, eid)["state"] == "queued"
