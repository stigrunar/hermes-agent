from __future__ import annotations

import json
import base64
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_codex_host import (
    HostRouterConfig,
    load_host_router_config,
    prepare_route,
    select_route,
    task_is_eligible,
)


def _run(*argv: str, cwd: Path) -> None:
    subprocess.run(
        argv, cwd=str(cwd), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "--quiet", cwd=repo)
    _run("git", "config", "user.email", "test@example.invalid", cwd=repo)
    _run("git", "config", "user.name", "Kanban Test", cwd=repo)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run("git", "add", "tracked.txt", cwd=repo)
    _run("git", "commit", "--quiet", "-m", "base", cwd=repo)
    return repo


def _fake_ssh(tmp_path: Path, mode: str = "normal") -> Path:
    script = tmp_path / f"fake_ssh_{mode}.py"
    script.write_text(
        "import json, subprocess, sys\n"
        "payload = sys.stdin.buffer.read()\n"
        "request = json.loads(payload.decode('utf-8')) if payload else {}\n"
        f"mode = {mode!r}\n"
        "args = sys.argv[1:]\n"
        "target = args[-1] if '--target' in args else ''\n"
        "target_index = args.index(target) if target in args else -1\n"
        "remote_argv = args[target_index + 1:] if target_index >= 0 else []\n"
        "expected_remote = ['python3', '-m', 'hermes_cli.kanban_codex_host', 'helper', '--target', target]\n"
        "if len(args) < 2 or args[-2:] != ['--target', target] or target != 'fake-host' or remote_argv != expected_remote:\n"
        "    raise SystemExit(19)\n"
        "helper = [sys.executable, '-m', 'hermes_cli.kanban_codex_host', 'helper', '--target', target]\n"
        "if mode == 'prepare_fail' and request.get('op') in ('prepare', 'cleanup'):\n"
        "    response = {'protocol': request.get('protocol'), 'marker': request.get('marker'), 'token': request.get('token'), 'ssh_target': target, 'workspace_root_marker': request.get('workspace_root_marker'), 'task_id': request.get('task_id'), 'remote_workspace': request.get('remote_workspace'), 'base': request.get('base'), 'tree': request.get('tree'), 'branch': request.get('branch'), 'workspace_marker': request.get('workspace_marker'), 'ok': False, 'reason': 'injected secret task prose'}\n"
        "    print(json.dumps(response))\n"
        "    raise SystemExit(23)\n"
        "if mode == 'prepare_mismatch' and request.get('op') == 'prepare':\n"
        "    done = subprocess.run(helper, input=payload, capture_output=True)\n"
        "    response = json.loads(done.stdout.decode('utf-8'))\n"
        "    response['tree'] = '0' * 40\n"
        "    print(json.dumps(response))\n"
        "    raise SystemExit(0)\n"
        "if mode == 'disconnect_before' and request.get('op') == 'run':\n"
        "    done = subprocess.run(helper, input=payload, stdout=subprocess.DEVNULL)\n"
        "    raise SystemExit(17 if done.returncode == 0 else done.returncode)\n"
        "if mode == 'disconnect_after' and request.get('op') == 'run':\n"
        "    done = subprocess.run(helper, input=payload, stdout=subprocess.DEVNULL)\n"
        "    raise SystemExit(17 if done.returncode == 0 else done.returncode)\n"
        "done = subprocess.run(helper, input=payload)\n"
        "raise SystemExit(done.returncode)\n",
        encoding="utf-8",
    )
    return script


def _fake_codex(tmp_path: Path, mode: str = "mutate", delay: float = 0.0) -> Path:
    script = tmp_path / f"fake_codex_{mode}.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "context = sys.stdin.read()\n"
        f"time.sleep({delay!r})\n"
        f"mode = {mode!r}\n"
        "if mode == 'mutate':\n"
        "    pathlib.Path('tracked.txt').write_text('remote\\n', encoding='utf-8')\n"
        "    pathlib.Path('untracked.txt').write_text('new\\n', encoding='utf-8')\n"
        "elif mode == 'no_mutation':\n"
        "    pass\n"
        "elif mode == 'mutate_then_fail':\n"
        "    pathlib.Path('tracked.txt').write_text('remote\\n', encoding='utf-8')\n"
        "    pathlib.Path('untracked.txt').write_text('new\\n', encoding='utf-8')\n"
        "    raise SystemExit(9)\n"
        "else:\n"
        "    raise SystemExit(11)\n"
        "assert context\n",
        encoding="utf-8",
    )
    return script


def _check_script(tmp_path: Path, success: bool = True) -> Path:
    script = tmp_path / ("check_ok.py" if success else "check_fail.py")
    script.write_text(
        "import sys\n"
        + ("raise SystemExit(0)\n" if success else "raise SystemExit(3)\n"),
        encoding="utf-8",
    )
    return script


def _selector(tmp_path: Path, route: str) -> Path:
    script = tmp_path / f"selector_{route}.py"
    script.write_text(
        "import json\n"
        f"print(json.dumps({{'route': {route!r}, 'reason': 'fake-ready'}}))\n",
        encoding="utf-8",
    )
    return script


def _route_config(
    tmp_path: Path,
    *,
    route: str = "mac_codex",
    codex_mode: str = "mutate",
    codex_delay: float = 0.0,
    ssh_mode: str = "normal",
    checks: tuple[tuple[str, ...], ...] = (),
    max_total: int = 2,
    route_timeout: float = 4,
) -> dict:
    ssh = _fake_ssh(tmp_path, ssh_mode)
    codex = _fake_codex(tmp_path, codex_mode, codex_delay)
    route_value = {
        "ssh_command": [sys.executable, str(ssh), ssh_mode],
        "ssh_target": "fake-host",
        "workspace_root": str(tmp_path / "remote-root"),
        "codex_command": [sys.executable, str(codex)],
        "timeout_seconds": route_timeout,
        "heartbeat_seconds": 0.05,
        "artifact_max_bytes": 2 * 1024 * 1024,
        "check_commands": [list(command) for command in checks],
    }
    return {
        "kanban": {
            "codex_host_router": {
                "enabled": True,
                "allowed_assignees": ["builder"],
                "selector_command": [sys.executable, str(_selector(tmp_path, route))],
                "selector_timeout_seconds": 2,
                "max_routes_per_tick": 1,
                "max_total_routes": max_total,
                "routes": {route: route_value},
            }
        }
    }


def _task(**overrides):
    value = dict(
        id="task-1", assignee="builder", status="ready", workspace_kind="worktree",
        project_id="proj", execution_preflight={"resolved": {"action": "write"}},
    )
    value.update(overrides)
    return SimpleNamespace(**value)


def _db_task(conn, repo: Path, *, title: str = "Implement change") -> str:
    task_id = kb.create_task(
        conn, title=title, body="private task prose secret-token",
        assignee="builder", workspace_kind="worktree", workspace_path=str(repo),
        initial_status="running",
    )
    conn.execute(
        "UPDATE tasks SET project_id=?, execution_preflight=? WHERE id=?",
        ("proj", json.dumps({"resolved": {"action": "write"}}), task_id),
    )
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
    conn.commit()
    return task_id


def _wait_task(conn, task_id: str, statuses: set[str], timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row and row["status"] in statuses:
            return row
        time.sleep(0.05)
    return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def _wait_event(conn, task_id: str, kind: str, timeout: float = 15.0) -> bool:
    """Wait for a committed lifecycle event, with timeout only as a guard."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind=? LIMIT 1",
            (task_id, kind),
        ).fetchone():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS.clear()
    with kb.connect(home / "kanban.db") as conn:
        yield home, conn


def test_absent_router_is_disabled_and_eligibility_is_false():
    cfg = load_host_router_config({})
    assert cfg.enabled is False
    assert task_is_eligible(_task(), cfg) is False


def test_eligibility_is_mechanical_and_review_or_nonmutating_tasks_are_rejected():
    cfg = HostRouterConfig.from_mapping({
        "kanban": {"codex_host_router": {
            "enabled": True, "allowed_assignees": ["builder"],
            "selector_command": ["/bin/true"], "max_routes_per_tick": 1,
            "max_total_routes": 1, "routes": {},
        }}
    })
    assert task_is_eligible(_task(), cfg)
    assert not task_is_eligible(_task(status="review"), cfg)
    assert not task_is_eligible(_task(workspace_kind="scratch"), cfg)
    assert not task_is_eligible(_task(execution_preflight={"resolved": {"action": "test"}}), cfg)
    assert not task_is_eligible(_task(project_id=None), cfg)


def test_selector_nonzero_malformed_oversized_timeout_and_unknown_defer(tmp_path, monkeypatch):
    def cfg(command):
        return HostRouterConfig.from_mapping({"kanban": {"codex_host_router": {
            "enabled": True, "allowed_assignees": ["builder"],
            # Real Python startup can exceed 50 ms when this file runs beside
            # the full parallel Kanban suite.  The timeout case below is
            # injected directly, so keep all process-backed cases deterministic.
            "selector_command": command, "selector_timeout_seconds": 1.0,
            "selector_max_output_bytes": 32, "max_routes_per_tick": 1,
            "max_total_routes": 1, "routes": {},
        }}})

    cases = [
        ([sys.executable, "-c", "import sys; print('x'); sys.exit(4)"], "selector_nonzero"),
        ([sys.executable, "-c", "print('not-json')"], "selector_failed"),
        ([sys.executable, "-c", "print('x'*100)"], "selector_output_oversized"),
        ([sys.executable, "-c", "import time; time.sleep(1)"], "selector_timeout"),
        ([sys.executable, "-c", "print('{\"route\":\"unknown\"}')"], "selector_unknown_route"),
    ]
    for command, reason in cases:
        if reason == "selector_timeout":
            import hermes_cli.kanban_codex_host as host
            monkeypatch.setattr(
                host, "_bounded_process",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    subprocess.TimeoutExpired(command, 0.05)
                ),
            )
        result = select_route(cfg(command), task_id="task-1", assignee="builder")
        assert result["route"] == "defer"
        assert result["reason"] == reason
        if reason == "selector_timeout":
            monkeypatch.undo()


def test_enabled_router_rejects_shell_string_and_unsafe_route_values():
    with pytest.raises(ValueError):
        HostRouterConfig.from_mapping({"kanban": {"codex_host_router": {
            "enabled": True, "allowed_assignees": ["builder"],
            "selector_command": "selector", "max_routes_per_tick": 1,
            "max_total_routes": 1, "routes": {},
        }}})
    base = {
        "enabled": True, "allowed_assignees": ["builder"],
        "selector_command": ["/bin/true"], "max_routes_per_tick": 1,
        "max_total_routes": 1,
    }
    route = {
        "ssh_command": ["ssh"], "ssh_target": "-bad",
        "workspace_root": "/tmp/r", "codex_command": ["codex"],
    }
    with pytest.raises(ValueError):
        HostRouterConfig.from_mapping({"kanban": {"codex_host_router": {**base, "routes": {"mac_codex": route}}}})


def test_prepare_proves_lease_bundle_checkout_and_marker_round_trip(tmp_path):
    repo = _git_repo(tmp_path)
    raw = _route_config(tmp_path)
    cfg = HostRouterConfig.from_mapping(raw)
    prepared = prepare_route(
        _task(), repo, cfg, {"route": "mac_codex", "reason": "fake"}
    )
    assert prepared.base and prepared.tree and prepared.marker
    remote = Path(raw["kanban"]["codex_host_router"]["routes"]["mac_codex"]["workspace_root"]) / prepared.remote_workspace
    assert remote.is_dir()
    assert (remote / ".hermes-codex-marker").read_text(encoding="ascii") == prepared.marker
    assert prepared.cleanup(cfg, allow="no_mutation")
    assert not remote.exists()


def test_selector_owned_wsl_and_mac_route_choices_are_executed_without_fallback(
    tmp_path,
):
    for route in ("wsl_codex", "mac_codex"):
        route_tmp = tmp_path / route
        route_tmp.mkdir()
        raw = _route_config(route_tmp, route=route)
        cfg = HostRouterConfig.from_mapping(raw)
        selection = select_route(cfg, task_id="task-1", assignee="builder")
        assert selection["route"] == route
        repo_tmp = tmp_path / f"repo-{route}"
        repo_tmp.mkdir()
        repo = _git_repo(repo_tmp)
        prepared = prepare_route(_task(), repo, cfg, selection)
        assert prepared.route == route
        assert prepared.cleanup(cfg, allow="no_mutation")


def test_fake_ssh_codex_lifecycle_applies_artifact_and_requests_same_run_review(
    isolated_home, tmp_path, monkeypatch,
):
    _home, conn = isolated_home
    repo = _git_repo(tmp_path)
    task_id = _db_task(conn, repo)
    raw = _route_config(tmp_path, codex_delay=0.5)
    rows = conn.execute(
        "SELECT id, assignee FROM tasks WHERE id=?", (task_id,)
    ).fetchall()
    result = kb.DispatchResult()
    kb._try_remote_codex_when_full(
        conn, rows, result, effective_config=raw, board="default",
    )
    assert len(result.remote_routed) == 1
    run_id = result.remote_routed[0]["run_id"]
    assert run_id is not None
    row = _wait_task(conn, task_id, {"review"})
    assert row["status"] == "review"
    assert row["current_run_id"] is None
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert Path(task.workspace_path).is_dir()
    assert Path(task.workspace_path, "tracked.txt").read_text(encoding="utf-8") == "remote\n"
    assert Path(task.workspace_path, "untracked.txt").read_text(encoding="utf-8") == "new\n"
    run = conn.execute(
        "SELECT id, outcome, launch_mode, metadata FROM task_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    assert run["id"] == run_id and run["outcome"] == "review_requested"
    assert run["launch_mode"] == "direct"
    metadata = json.loads(run["metadata"])
    assert metadata["artifact_status"] == "applied"
    assert metadata["artifact_bytes"] > 0
    assert metadata["check_count"] == 0
    assert metadata["remote_elapsed_ms"] >= 0
    assert metadata["latency_ms"] == metadata["remote_elapsed_ms"]
    assert "private task prose" not in json.dumps(metadata)
    assert "secret-token" not in json.dumps(metadata)
    assert conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
    ).fetchone()[0] == 1
    assert kb.get_task(conn, task_id).branch_name == f"wt/{task_id}"
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='heartbeat'",
        (task_id,),
    ).fetchone()[0] >= 1


def test_remote_check_failure_fences_without_applying_unverified_artifact(
    isolated_home, tmp_path,
):
    _home, conn = isolated_home
    repo = _git_repo(tmp_path)
    task_id = _db_task(conn, repo)
    check = _check_script(tmp_path, success=False)
    raw = _route_config(
        tmp_path, codex_mode="mutate", checks=((sys.executable, str(check)),),
    )
    rows = conn.execute("SELECT id, assignee FROM tasks WHERE id=?", (task_id,)).fetchall()
    result = kb.DispatchResult()
    kb._try_remote_codex_when_full(conn, rows, result, effective_config=raw, board="default")
    row = _wait_task(conn, task_id, {"blocked"})
    assert row["status"] == "blocked"
    assert str(row["claim_lock"]).startswith("remote-fence:")
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert Path(task.workspace_path, "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert not Path(task.workspace_path, "untracked.txt").exists()
    event = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert event["kind"] == "remote_mutation_fenced"
    assert "secret-token" not in (event["payload"] or "")


def test_disconnect_before_mutation_cleans_and_disconnect_after_mutation_fences(
    isolated_home, tmp_path,
):
    _home, conn = isolated_home
    repo = _git_repo(tmp_path)
    task_id = _db_task(conn, repo, title="disconnect task prose")
    raw = _route_config(tmp_path, ssh_mode="disconnect_before", codex_mode="no_mutation")
    rows = conn.execute("SELECT id, assignee FROM tasks WHERE id=?", (task_id,)).fetchall()
    result = kb.DispatchResult()
    kb._try_remote_codex_when_full(conn, rows, result, effective_config=raw, board="default")
    row = _wait_task(conn, task_id, {"blocked"})
    assert row["status"] == "blocked"
    # No mutation was proved, so the lease was positively cleaned and the
    # normal capability block did not retain a remote fence.
    assert row["claim_lock"] is None
    assert kb.unblock_task(conn, task_id) is True
    assert conn.execute(
        "SELECT status FROM tasks WHERE id=?", (task_id,)
    ).fetchone()[0] == "ready"

    task_id2 = _db_task(conn, repo, title="disconnect after prose")
    after_root = tmp_path / "after"
    after_root.mkdir()
    raw2 = _route_config(after_root, ssh_mode="disconnect_after")
    rows2 = conn.execute("SELECT id, assignee FROM tasks WHERE id=?", (task_id2,)).fetchall()
    result2 = kb.DispatchResult()
    kb._try_remote_codex_when_full(conn, rows2, result2, effective_config=raw2, board="default")
    row2 = _wait_task(conn, task_id2, {"blocked"})
    assert row2["status"] == "blocked"
    assert str(row2["claim_lock"]).startswith("remote-fence:")
    assert kb.unblock_task(conn, task_id2) is False
    assert kb.recompute_ready(conn) == 0


def test_supervisor_timeout_is_fenced_without_local_retry(
    isolated_home, tmp_path, monkeypatch,
):
    _home, conn = isolated_home
    repo = _git_repo(tmp_path)
    task_id = _db_task(conn, repo, title="timeout task")
    import hermes_cli.kanban_codex_host as host

    # Route selection is a separate contract (covered above).  Keep this
    # lifecycle regression deterministic by removing its selector subprocess
    # from the contention surface; the remote helper remains a real process.
    monkeypatch.setattr(
        host,
        "select_route",
        lambda *_args, **_kwargs: {
            "route": "mac_codex", "reason": "test-ready", "latency_ms": 0,
        },
    )
    # Leave enough time for prepare/helper interpreter startup under the full
    # parallel suite, while keeping the Codex child decisively beyond the run
    # deadline so this exercises the intended post-claim timeout path.
    raw = _route_config(tmp_path, codex_delay=4.0, route_timeout=2.0)
    rows = conn.execute("SELECT id, assignee FROM tasks WHERE id=?", (task_id,)).fetchall()
    result = kb.DispatchResult()
    kb._try_remote_codex_when_full(conn, rows, result, effective_config=raw, board="default")
    assert _wait_event(conn, task_id, "remote_supervisor_ready")
    assert _wait_event(conn, task_id, "blocked")
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["status"] == "blocked"
    # The shipped helper positively reports no mutation when its Codex child
    # is killed before it writes; the canonical capability block is therefore
    # safely retryable after an explicit unblock, without a remote fence.
    assert row["claim_lock"] is None
    assert row["current_run_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='remote_supervisor_ready'",
        (task_id,),
    ).fetchone()[0] == 1


def test_local_capacity_and_disabled_or_ineligible_tasks_never_contact_selector(
    isolated_home, tmp_path, monkeypatch,
):
    _home, conn = isolated_home
    repo = _git_repo(tmp_path)
    task_id = _db_task(conn, repo)
    raw = _route_config(tmp_path)
    import hermes_cli.kanban_codex_host as host
    monkeypatch.setattr(host, "select_route", lambda *_a, **_k: pytest.fail("selector contacted"))
    disabled = json.loads(json.dumps(raw))
    disabled["kanban"]["codex_host_router"]["enabled"] = False
    rows = conn.execute("SELECT id, assignee FROM tasks WHERE id=?", (task_id,)).fetchall()
    result = kb.DispatchResult()
    kb._try_remote_codex_when_full(conn, rows, result, effective_config=disabled, board="default")
    assert conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0] is None

    conn.execute("UPDATE tasks SET execution_preflight=? WHERE id=?", (json.dumps({"resolved": {"action": "inspect"}}), task_id))
    conn.commit()
    kb._try_remote_codex_when_full(conn, rows, result, effective_config=raw, board="default")
    assert conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0] is None


def test_local_slot_available_uses_native_worker_without_selector(
    isolated_home, tmp_path, monkeypatch,
):
    _home, conn = isolated_home
    repo = _git_repo(tmp_path)
    task_id = _db_task(conn, repo)
    raw = _route_config(tmp_path)
    import hermes_cli.kanban_codex_host as host
    monkeypatch.setattr(host, "select_route", lambda *_a, **_k: pytest.fail("selector contacted"))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda *_a, **_k: "ok")
    monkeypatch.setattr(
        kb, "observe_running_tasks_other_boards",
        lambda _board: SimpleNamespace(
            running_count=0,
            has_independent_db=False,
            source="test",
        ),
    )
    monkeypatch.setattr(
        kb, "_worker_scope_config",
        lambda *_a, **_k: kb._WorkerScopeConfig(
            enabled=False, required=False, slice="hermes-kanban-workers.slice",
            memory_high="2G", memory_max="3G", memory_swap_max="512M",
            tasks_max=512, oom_policy="stop",
        ),
    )
    spawned = []
    monkeypatch.setattr(
        kb, "_default_spawn",
        lambda task, workspace, **_kwargs: spawned.append((task.id, workspace)) or 4242,
    )
    result = kb.dispatch_once(
        conn, max_spawn=1, effective_config=raw, board="default",
    )
    assert result.spawned and result.spawned[0][0] == task_id
    assert spawned and spawned[0][0] == task_id
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "running"


def test_selector_is_called_only_after_local_capacity_is_proven_full(
    isolated_home, tmp_path, monkeypatch,
):
    _home, conn = isolated_home
    repo = _git_repo(tmp_path)
    task_id = _db_task(conn, repo)
    raw = _route_config(tmp_path)
    import hermes_cli.kanban_codex_host as host
    calls = []
    monkeypatch.setattr(host, "select_route", lambda *args, **kwargs: calls.append(kwargs) or {"route": "defer", "reason": "local"})
    rows = conn.execute("SELECT id, assignee FROM tasks WHERE id=?", (task_id,)).fetchall()
    result = kb.DispatchResult()
    kb._try_remote_codex_when_full(conn, rows, result, effective_config=raw, board="default")
    assert calls == [{"task_id": task_id, "assignee": "builder"}]
    assert conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0] is None


def test_active_remote_cap_is_separate_from_local_capacity(
    isolated_home, tmp_path, monkeypatch,
):
    _home, conn = isolated_home
    repo = _git_repo(tmp_path)
    active_id = _db_task(conn, repo, title="active")
    active = kb.claim_task(conn, active_id, claimer="remote-owner")
    assert active is not None and active.current_run_id is not None
    conn.execute(
        "UPDATE task_runs SET launch_mode='remote-codex-supervisor', verification_status='remote-running' WHERE id=?",
        (active.current_run_id,),
    )
    waiting_id = _db_task(conn, repo, title="waiting")
    raw = _route_config(tmp_path, max_total=1)
    import hermes_cli.kanban_codex_host as host
    monkeypatch.setattr(host, "select_route", lambda *_a, **_k: pytest.fail("active route cap ignored"))
    rows = conn.execute("SELECT id, assignee FROM tasks WHERE status='ready'").fetchall()
    result = kb.DispatchResult()
    kb._try_remote_codex_when_full(conn, rows, result, effective_config=raw, board="default")
    assert conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (waiting_id,)).fetchone()[0] is None


def test_claim_lost_after_prepare_cleans_without_run_or_failure(
    isolated_home, tmp_path, monkeypatch,
):
    _home, conn = isolated_home
    repo = _git_repo(tmp_path)
    task_id = _db_task(conn, repo)
    raw = _route_config(tmp_path)
    monkeypatch.setattr(kb, "claim_task", lambda *_a, **_k: None)
    rows = conn.execute("SELECT id, assignee FROM tasks WHERE id=?", (task_id,)).fetchall()
    result = kb.DispatchResult()
    kb._try_remote_codex_when_full(conn, rows, result, effective_config=raw, board="default")
    row = conn.execute("SELECT status, current_run_id, consecutive_failures FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert tuple(row) == ("ready", None, 0)
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0] == 0
    assert not (tmp_path / "repo" / ".worktrees" / task_id).exists()


def test_postclaim_supervisor_launch_failure_releases_lease_and_uses_one_run(
    isolated_home, tmp_path, monkeypatch,
):
    _home, conn = isolated_home
    repo = _git_repo(tmp_path)
    task_id = _db_task(conn, repo)
    raw = _route_config(tmp_path)
    import hermes_cli.kanban_codex_host as host
    monkeypatch.setattr(
        host, "launch_supervisor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("launch unavailable")),
    )
    rows = conn.execute("SELECT id, assignee FROM tasks WHERE id=?", (task_id,)).fetchall()
    result = kb.DispatchResult()
    kb._try_remote_codex_when_full(conn, rows, result, effective_config=raw, board="default")
    row = conn.execute(
        "SELECT status, current_run_id, worker_pid, consecutive_failures FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    assert tuple(row) == ("ready", None, None, 1)
    run = conn.execute(
        "SELECT COUNT(*), outcome FROM task_runs WHERE task_id=?", (task_id,)
    ).fetchone()
    assert run[0] == 1 and run[1] == "spawn_failed"
    assert not any((tmp_path / "remote-root").glob(".hermes-codex-leases/*"))


def test_preclaim_failure_cleanup_and_unproven_cleanup_are_fail_closed(
    isolated_home, tmp_path,
):
    _home, conn = isolated_home
    repo = _git_repo(tmp_path)
    task_id = _db_task(conn, repo, title="prepare mismatch")
    raw = _route_config(tmp_path, ssh_mode="prepare_mismatch")
    rows = conn.execute("SELECT id, assignee FROM tasks WHERE id=?", (task_id,)).fetchall()
    result = kb.DispatchResult()
    kb._try_remote_codex_when_full(conn, rows, result, effective_config=raw, board="default")
    row = conn.execute(
        "SELECT status, current_run_id, consecutive_failures FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    assert tuple(row) == ("ready", None, 0)
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0] == 0

    task_id2 = _db_task(conn, repo, title="prepare unavailable")
    unavailable_root = tmp_path / "unavailable"
    unavailable_root.mkdir()
    raw2 = _route_config(unavailable_root, ssh_mode="prepare_fail")
    rows2 = conn.execute("SELECT id, assignee FROM tasks WHERE id=?", (task_id2,)).fetchall()
    kb._try_remote_codex_when_full(conn, rows2, result, effective_config=raw2, board="default")
    row2 = conn.execute(
        "SELECT status, claim_lock, current_run_id, consecutive_failures FROM tasks WHERE id=?",
        (task_id2,),
    ).fetchone()
    assert tuple(row2) == ("blocked", None, None, 0)
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id2,)).fetchone()[0] == 0
    event = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id2,),
    ).fetchone()
    assert event["kind"] == "remote_route_fenced"
    assert "injected secret" not in (event["payload"] or "")


def test_artifact_paths_are_rejected_before_any_local_write(tmp_path):
    repo = _git_repo(tmp_path)
    import hermes_cli.kanban_codex_host as host
    artifact = {
        "diff_b64": "",
        "untracked": [{
            "path": "../escape.txt",
            "mode": 0o644,
            "data_b64": base64.b64encode(b"must not write").decode("ascii"),
        }],
        "bytes": len(b"must not write"),
    }
    with pytest.raises(ValueError):
        host._apply_artifact(repo, artifact, 1024)
    assert not (tmp_path / "escape.txt").exists()


def test_remote_reclaim_timeout_and_crash_paths_keep_a_durable_fence(
    isolated_home, tmp_path,
):
    _home, conn = isolated_home
    repo = _git_repo(tmp_path)
    task_id = _db_task(conn, repo, title="remote stale")
    claimed = kb.claim_task(conn, task_id, claimer=kb._claimer_id())
    assert claimed is not None and claimed.current_run_id is not None
    run_id = int(claimed.current_run_id)
    conn.execute(
        "UPDATE tasks SET worker_pid=?, claim_expires=? WHERE id=?",
        (999999999, int(time.time()) - 5, task_id),
    )
    conn.execute(
        "UPDATE task_runs SET worker_pid=?, launch_mode='remote-codex-supervisor', verification_status='remote-running' WHERE id=?",
        (999999999, run_id),
    )
    conn.commit()
    assert kb.release_stale_claims(conn) == 0
    row = conn.execute(
        "SELECT status, claim_lock, current_run_id FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    assert row["status"] == "blocked"
    assert row["claim_lock"] == f"remote-fence:{run_id}"
    assert row["current_run_id"] is None
    assert kb.unblock_task(conn, task_id) is False

    task_id2 = _db_task(conn, repo, title="remote timeout")
    claimed2 = kb.claim_task(conn, task_id2, claimer=kb._claimer_id())
    assert claimed2 is not None and claimed2.current_run_id is not None
    run_id2 = int(claimed2.current_run_id)
    conn.execute(
        "UPDATE tasks SET worker_pid=?, max_runtime_seconds=?, started_at=? WHERE id=?",
        (999999999, 1, int(time.time()) - 10, task_id2),
    )
    conn.execute(
        "UPDATE task_runs SET worker_pid=?, launch_mode='remote-codex-supervisor', verification_status='remote-running', started_at=? WHERE id=?",
        (999999999, int(time.time()) - 10, run_id2),
    )
    conn.commit()
    assert task_id2 in kb.enforce_max_runtime(conn)
    row2 = conn.execute(
        "SELECT status, claim_lock, current_run_id FROM tasks WHERE id=?", (task_id2,)
    ).fetchone()
    assert row2["status"] == "blocked"
    assert row2["claim_lock"] == f"remote-fence:{run_id2}"
    assert row2["current_run_id"] is None


def test_receipt_allowlist_excludes_selector_prose_and_remote_paths(tmp_path):
    raw = _route_config(tmp_path)
    cfg = HostRouterConfig.from_mapping(raw)
    prepared = prepare_route(
        _task(), _git_repo(tmp_path), cfg,
        {"route": "mac_codex", "reason": "secret-token task prose /tmp/private"},
    )
    receipt = prepared.receipt(run_id=7)
    encoded = json.dumps(receipt)
    assert "secret-token" not in encoded
    assert "task prose" not in encoded
    assert str(tmp_path / "remote-root") not in encoded
    assert set(receipt) == {
        "contract", "task_id", "run_id", "route", "reason", "base", "tree",
        "branch", "workspace_marker", "mutation_state", "artifact_status",
        "check_status", "latency_ms",
    }
    assert prepared.cleanup(cfg, allow="no_mutation")
