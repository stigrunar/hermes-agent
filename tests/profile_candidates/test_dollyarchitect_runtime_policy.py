"""Integration contracts for the DollyArchitect runtime-policy seam."""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

import model_tools
from agent import profile_runtime_policy as policy
from hermes_cli import kanban_db as kb
from hermes_cli import projects_db
from hermes_cli.middleware import run_tool_execution_middleware


SOURCE_OVERLAY = Path(__file__).parents[2] / "profile_candidates" / "dollyarchitect"


def _config_text() -> str:
    return """
model:
  default: gpt-5.6-sol
agent:
  max_turns: 60
  reasoning_effort: high
  runtime_policy:
    id: dollyarchitect.v1
    enabled: true
telegram:
  dm_policy: allowlist
  allow_from:
    - "123456"
  group_policy: disabled
toolsets:
  - hermes-cli
skills:
  disabled:
    - contract-driven-frontend-implementation
    - external-upstream-pr-recuts
    - mobile-ui-verification
    - release-candidate-evidence
""".lstrip()


def _install_profile(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    root = tmp_path / ".hermes"
    home = root / "profiles" / "dollyarchitect"
    overlay = home / policy.OVERLAY_RELATIVE_PATH
    overlay.mkdir(parents=True)
    for name in policy.EXPECTED_OVERLAY_HASHES:
        shutil.copyfile(SOURCE_OVERLAY / name, overlay / name)
    (home / "config.yaml").write_text(_config_text(), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "dollyarchitect")
    _set_project_repo(root, "/repos/runtime")
    policy.reset_runtime_policy_state_for_tests()
    model_tools._clear_tool_defs_cache()
    return root, home


def _set_project_repo(root: Path, repo: str) -> None:
    with projects_db.connect_closing(db_path=root / "projects.db") as conn:
        conn.execute(
            "INSERT OR REPLACE INTO projects "
            "(id, slug, name, primary_path, created_at, archived) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            ("project-runtime", "project-runtime", "Runtime project", repo, 1),
        )
        conn.commit()


def _contract(
    workspace: Path,
    *,
    work_kind: str = "cross_repo_contract",
    workspace_kind: str = "scratch",
    actions=None,
    implementation_repo: str = "/repos/runtime",
    implementation_workspace_policy: str = "preparation_only_requires_distinct_workspace",
) -> dict:
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    selected_actions = actions or ["architecture_decision"]
    writes = selected_actions == ["write_architecture_document"]
    documents = [str(artifacts / "architecture.md")] if writes else []
    return {
        "contract_id": "runtime-contract-1",
        "work_kind": work_kind,
        "workspace_kind": workspace_kind,
        "writable_artifact_roots": [str(artifacts)] if writes else [],
        "architecture_document_paths": documents,
        "requested_actions": selected_actions,
        "implementation_owner": None,
        "operations_owner": None,
        "project_id": "project-runtime",
        "repository_identity": f"project:project-runtime:repo:{implementation_repo}",
        "implementation_repo": implementation_repo,
        "implementation_workspace_policy": implementation_workspace_policy,
        "bounded_file_cluster": ["agent/profile_runtime_policy.py"],
        "non_goals": ["Release and deploy"],
    }


def _block(marker: str, payload: dict) -> str:
    return (
        f"<!-- {marker}\n"
        f"{json.dumps(payload, separators=(',', ':'), sort_keys=True)}\n"
        f"{marker} -->"
    )


def _task(workspace: Path, contract: dict, *, run_id: int = 7) -> kb.Task:
    return kb.Task(
        id="t_architect_runtime",
        title="Architecture contract",
        body=_block(policy.DISPATCH_MARKER, contract),
        assignee="dollyarchitect",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=1,
        completed_at=None,
        workspace_kind=contract["workspace_kind"],
        workspace_path=str(workspace),
        claim_lock="claim",
        claim_expires=None,
        tenant=None,
        project_id=contract["project_id"],
        current_run_id=run_id,
    )


def _activate_worker(
    tmp_path: Path,
    monkeypatch,
    *,
    workspace_kind="scratch",
    actions=None,
    implementation_repo="/repos/runtime",
    implementation_workspace_policy="preparation_only_requires_distinct_workspace",
):
    root, home = _install_profile(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    contract = _contract(
        workspace,
        workspace_kind=workspace_kind,
        actions=actions,
        implementation_repo=implementation_repo,
        implementation_workspace_policy=implementation_workspace_policy,
    )
    _set_project_repo(root, implementation_repo)
    task = _task(workspace, contract)
    monkeypatch.setenv("HERMES_HOME", str(root))
    payload = policy.prepare_dollyarchitect_spawn_env(
        task=task,
        workspace=str(workspace),
        profile_name="dollyarchitect",
        hermes_home=home,
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(policy.INTERNAL_POLICY_ENV, payload)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    return root, home, workspace, contract


def test_accepted_architect_task_injects_canonical_policy_env_and_reaches_spawn(
    tmp_path, monkeypatch
):
    root, home = _install_profile(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _task(workspace, _contract(workspace))
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["env"] = dict(kwargs["env"])
        return FakeProc()

    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    assert kb._default_spawn(task, str(workspace)) == 4242
    payload = json.loads(captured["env"][policy.INTERNAL_POLICY_ENV])
    assert payload["policy_id"] == policy.POLICY_ID
    assert payload["task_id"] == task.id
    assert payload["workspace"] == str(workspace.resolve())
    assert captured["env"]["HERMES_HOME"] == str(home)


def test_spawn_rejects_contract_repo_that_no_longer_matches_project(
    tmp_path, monkeypatch
):
    root, home = _install_profile(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    contract = _contract(workspace)
    task = _task(workspace, contract)
    _set_project_repo(root, "/repos/rebound")
    monkeypatch.setenv("HERMES_HOME", str(root))

    with pytest.raises(
        policy.ProfileRuntimePolicyError,
        match="repository binding contradicts the current project",
    ):
        policy.prepare_dollyarchitect_spawn_env(
            task=task,
            workspace=str(workspace),
            profile_name="dollyarchitect",
            hermes_home=home,
        )


@pytest.mark.parametrize(
    ("work_kind", "owner"),
    [("implementation_code_patch", "DollyCode"), ("deploy", "DollyOps")],
)
def test_nonfit_work_is_rejected_before_popen_and_names_required_owner(
    tmp_path, monkeypatch, work_kind, owner
):
    root, _ = _install_profile(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _task(workspace, _contract(workspace, work_kind=work_kind))
    called = False

    def fake_popen(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(policy.ProfileRuntimePolicyError, match=owner):
        kb._default_spawn(task, str(workspace))
    assert called is False


def test_contradictory_contract_rejected_before_popen(tmp_path, monkeypatch):
    root, _ = _install_profile(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    contract = _contract(
        workspace,
        actions=["architecture_decision", "implementation"],
    )
    contract["implementation_owner"] = "DollyCode"
    task = _task(workspace, contract)
    called = False

    def fake_popen(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(
        policy.ProfileRuntimePolicyError, match="exactly one architecture capability"
    ):
        kb._default_spawn(task, str(workspace))
    assert called is False


def test_excluded_force_loaded_skill_rejected_before_popen(tmp_path, monkeypatch):
    root, _ = _install_profile(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _task(workspace, _contract(workspace))
    task.skills = ["contract-driven-frontend-implementation"]
    called = False

    def fake_popen(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(policy.ProfileRuntimePolicyError, match="excluded skill"):
        kb._default_spawn(task, str(workspace))
    assert called is False


def test_common_boundary_allows_scratch_write_and_rejects_escape(
    tmp_path, monkeypatch
):
    _, _, workspace, _ = _activate_worker(
        tmp_path,
        monkeypatch,
        actions=["write_architecture_document"],
    )
    artifacts = workspace / "artifacts"
    target = artifacts / "architecture.md"

    result = json.loads(
        model_tools.handle_function_call(
            "write_file", {"path": str(target), "content": "decision"}
        )
    )
    assert not result.get("error")
    assert target.read_text(encoding="utf-8") == "decision"
    patched = json.loads(
        model_tools.handle_function_call(
            "patch",
            {
                "mode": "replace",
                "path": str(target),
                "old_string": "decision",
                "new_string": "revised decision",
            },
        )
    )
    assert not patched.get("error")
    assert target.read_text(encoding="utf-8") == "revised decision"

    outside = tmp_path / "outside.md"
    denied = json.loads(
        model_tools.handle_function_call(
            "write_file", {"path": str(outside), "content": "escape"}
        )
    )
    assert "profile runtime policy denied" in denied["error"]
    assert not outside.exists()

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (artifacts / "escape").symlink_to(outside_dir, target_is_directory=True)
    symlink_target = artifacts / "escape" / "decision.md"
    denied = json.loads(
        model_tools.handle_function_call(
            "write_file", {"path": str(symlink_target), "content": "escape"}
        )
    )
    assert "profile runtime policy denied" in denied["error"]
    assert not (outside_dir / "decision.md").exists()


def test_worktree_only_named_document_is_writable(tmp_path, monkeypatch):
    _, _, workspace, contract = _activate_worker(
        tmp_path,
        monkeypatch,
        workspace_kind="worktree",
        actions=["write_architecture_document"],
    )
    named = Path(contract["architecture_document_paths"][0])
    allowed = json.loads(
        model_tools.handle_function_call(
            "write_file", {"path": str(named), "content": "# Architecture"}
        )
    )
    assert not allowed.get("error")
    assert named.exists()

    other = workspace / "artifacts" / "other.md"
    denied = json.loads(
        model_tools.handle_function_call(
            "write_file", {"path": str(other), "content": "no"}
        )
    )
    assert "profile runtime policy denied" in denied["error"]
    assert not other.exists()

    v4a = json.loads(
        model_tools.handle_function_call(
            "patch", {"mode": "patch", "patch": "*** Begin Patch\n*** End Patch"}
        )
    )
    assert "forbids V4A" in v4a["error"]


def test_disabled_tools_absent_and_direct_dispatch_denied(tmp_path, monkeypatch):
    _activate_worker(tmp_path, monkeypatch)
    definitions = model_tools.get_tool_definitions(quiet_mode=True)
    names = {item["function"]["name"] for item in definitions}
    assert names <= policy.ALLOWED_TOOLS
    assert {
        "read_file",
        "search_files",
        "skill_view",
        "skills_list",
        "session_search",
        "kanban_show",
        "kanban_create",
        "kanban_complete",
        "kanban_block",
    } <= names
    assert "terminal" not in names
    assert "execute_code" not in names
    assert "delegate_task" not in names
    assert "write_file" not in names
    assert "patch" not in names

    denied = json.loads(
        model_tools.handle_function_call(
            "terminal", {"command": "printf should-not-run"}
        )
    )
    assert "disabled by DollyArchitect policy" in denied["error"]

    agent = SimpleNamespace(
        tools=[
            {"function": {"name": "read_file"}},
            {"function": {"name": "hindsight_retain"}},
            {"function": {"name": "lcm_expand"}},
        ],
        valid_tool_names={"read_file", "hindsight_retain", "lcm_expand"},
        _context_engine_tool_names={"lcm_expand"},
    )
    policy.enforce_agent_tool_surface(agent)
    assert [item["function"]["name"] for item in agent.tools] == ["read_file"]
    assert agent.valid_tool_names == {"read_file"}
    assert agent._context_engine_tool_names == set()


@pytest.mark.parametrize("action", ["no_edits", "architecture_decision"])
def test_handoff_only_actions_deny_actual_writes_and_remove_schemas(
    tmp_path, monkeypatch, action
):
    _, _, workspace, _ = _activate_worker(
        tmp_path, monkeypatch, actions=[action]
    )
    target = workspace / "artifacts" / "forbidden.md"
    names = {
        item["function"]["name"]
        for item in model_tools.get_tool_definitions(quiet_mode=True)
    }
    assert {"write_file", "patch"}.isdisjoint(names)

    denied = json.loads(
        model_tools.handle_function_call(
            "write_file", {"path": str(target), "content": "must not write"}
        )
    )
    assert "disabled for this requested_actions capability" in denied["error"]
    assert not target.exists()
    patch_denied = json.loads(
        model_tools.handle_function_call(
            "patch",
            {
                "mode": "replace",
                "path": str(target),
                "old_string": "x",
                "new_string": "y",
            },
        )
    )
    assert "disabled for this requested_actions capability" in patch_denied["error"]
    assert not target.exists()


def _decision_packet() -> dict:
    return {
        "packet_id": "packet-1",
        "decision": "Use one narrow runtime policy seam.",
        "rationale": "Keep ordinary profiles unchanged.",
        "constraints": ["No implementation in DollyArchitect."],
        "acceptance_criteria": ["DollyCode receives one canonical handoff."],
        "dollycode_owner": "DollyCode",
        "architecture_artifact": "inline:architecture-decision",
        "validation_hypothesis": "A focused runtime test passes without redesign.",
    }


def test_one_handoff_canonicalized_second_denied_and_completion_gated(
    tmp_path, monkeypatch
):
    _activate_worker(tmp_path, monkeypatch)
    captured = []

    before = json.loads(
        run_tool_execution_middleware(
            "kanban_complete", {"summary": "done"}, lambda args: {"ok": True}
        )
    )
    assert "cannot complete before" in before["error"]

    create_args = {
        "title": "Implement architecture",
        "assignee": "dollycode",
        "body": _block(policy.DECISION_MARKER, _decision_packet()),
    }

    def create_terminal(args):
        captured.append(args)
        return json.dumps({"ok": True, "task_id": "t_child"})

    first = json.loads(
        run_tool_execution_middleware("kanban_create", create_args, create_terminal)
    )
    assert first.get("ok") is True, first
    emitted_body = captured[0]["body"]
    assert policy.HANDOFF_MARKER in emitted_body
    emitted = json.loads(emitted_body.splitlines()[1])
    assert emitted["implementation_actions"] == []
    assert captured[0]["assignee"] == "dollycode"
    assert captured[0]["parents"] == ["t_architect_runtime"]
    assert captured[0]["workspace_kind"] == "scratch"
    assert captured[0]["initial_status"] == "blocked"
    assert captured[0]["_trusted_project_binding"] == {
        "project_id": "project-runtime",
        "implementation_repo": "/repos/runtime",
    }

    second = json.loads(
        run_tool_execution_middleware("kanban_create", create_args, create_terminal)
    )
    assert "exactly one DollyCode handoff" in second["error"]
    assert len(captured) == 1

    completed = run_tool_execution_middleware(
        "kanban_complete", {"summary": "handoff emitted"}, lambda args: "complete"
    )
    assert completed == "complete"


def test_handoff_create_is_durable_exact_match_across_process_reset(
    tmp_path, monkeypatch
):
    root, home, workspace, contract = _activate_worker(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO tasks "
                "(id, title, assignee, status, workspace_kind, workspace_path, "
                "project_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "t_architect_runtime",
                    "Architecture source",
                    "dollyarchitect",
                    "running",
                    "scratch",
                    str(workspace),
                    contract["project_id"],
                    1,
                ),
            )

    create_args = {
        "title": "Implement architecture",
        "assignee": "dollycode",
        "body": _block(policy.DECISION_MARKER, _decision_packet()),
    }
    first = json.loads(
        model_tools.handle_function_call("kanban_create", create_args)
    )
    assert first.get("ok") is True, first
    with kb.connect_closing() as conn:
        created = kb.get_task(conn, first["task_id"])
        assert created is not None
        assert created.workspace_kind == "scratch"
        assert created.workspace_path is None
        materialized_workspace = kb.resolve_workspace(created)
        kb.set_workspace_path(conn, created.id, materialized_workspace)
    assert materialized_workspace.is_dir()

    policy.reset_runtime_policy_state_for_tests()
    retry_task = _task(workspace, contract, run_id=8)
    monkeypatch.setenv("HERMES_HOME", str(root))
    retry_payload = policy.prepare_dollyarchitect_spawn_env(
        task=retry_task,
        workspace=str(workspace),
        profile_name="dollyarchitect",
        hermes_home=home,
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(policy.INTERNAL_POLICY_ENV, retry_payload)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "8")

    retried = json.loads(
        model_tools.handle_function_call("kanban_create", create_args)
    )
    assert retried["ok"] is True
    assert retried["task_id"] == first["task_id"]
    assert retried["idempotent_reuse"] is True
    with kb.connect_closing() as conn:
        matching = conn.execute(
            "SELECT id, body, workspace_kind, workspace_path, status, project_id "
            "FROM tasks WHERE idempotency_key LIKE 'dollyarchitect:%'"
        ).fetchall()
    assert len(matching) == 1
    assert matching[0]["workspace_kind"] == "scratch"
    assert matching[0]["workspace_path"] == str(materialized_workspace)
    assert matching[0]["status"] == "blocked"
    assert matching[0]["project_id"] == contract["project_id"]
    emitted = json.loads(matching[0]["body"].splitlines()[1])
    assert emitted["source_task_id"] == "t_architect_runtime"
    assert emitted["dispatch_contract_id"] == contract["contract_id"]
    assert emitted["implementation_workspace_policy"] == (
        "preparation_only_requires_distinct_workspace"
    )
    assert len(emitted["architecture_artifact_sha256"]) == 64

    policy.reset_runtime_policy_state_for_tests()
    conflict = dict(create_args)
    conflict["title"] = "Conflicting implementation title"
    denied = json.loads(
        model_tools.handle_function_call("kanban_create", conflict)
    )
    assert "conflicting create content" in denied["error"]
    with kb.connect_closing() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks "
            "WHERE idempotency_key LIKE 'dollyarchitect:%'"
        ).fetchone()[0]
    assert count == 1


def test_materialized_project_repo_creates_bound_dollycode_worktree(
    tmp_path, monkeypatch
):
    implementation_repo = tmp_path / "implementation-repo"
    implementation_repo.mkdir()
    (implementation_repo / ".git").mkdir()
    root, home, workspace, contract = _activate_worker(
        tmp_path,
        monkeypatch,
        workspace_kind="worktree",
        implementation_repo=str(implementation_repo),
        implementation_workspace_policy="project_primary_repo_worktree",
    )
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO tasks "
                "(id, title, assignee, status, workspace_kind, workspace_path, "
                "project_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "t_architect_runtime",
                    "Architecture source",
                    "dollyarchitect",
                    "running",
                    "worktree",
                    str(workspace),
                    contract["project_id"],
                    1,
                ),
            )

    create_args = {
        "title": "Implement architecture",
        "assignee": "dollycode",
        "body": _block(policy.DECISION_MARKER, _decision_packet()),
    }
    first = json.loads(model_tools.handle_function_call("kanban_create", create_args))
    assert first.get("ok") is True, first
    with kb.connect_closing() as conn:
        created = kb.get_task(conn, first["task_id"])
    assert created is not None
    assert created.status == "todo"
    assert created.project_id == contract["project_id"]
    assert created.workspace_kind == "worktree"
    assert created.workspace_path == str(
        implementation_repo / ".worktrees" / created.id
    )
    assert created.branch_name == f"{contract['project_id']}/{created.id}"

    policy.reset_runtime_policy_state_for_tests()
    retry_task = _task(workspace, contract, run_id=8)
    monkeypatch.setenv("HERMES_HOME", str(root))
    retry_payload = policy.prepare_dollyarchitect_spawn_env(
        task=retry_task,
        workspace=str(workspace),
        profile_name="dollyarchitect",
        hermes_home=home,
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(policy.INTERNAL_POLICY_ENV, retry_payload)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "8")
    retried = json.loads(
        model_tools.handle_function_call("kanban_create", create_args)
    )
    assert retried["ok"] is True
    assert retried["task_id"] == created.id
    assert retried["idempotent_reuse"] is True


def test_ordinary_profiles_remain_unchanged(tmp_path, monkeypatch):
    home = tmp_path / ".hermes" / "profiles" / "ordinary"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "ordinary")
    monkeypatch.delenv(policy.INTERNAL_POLICY_ENV, raising=False)
    definitions = [{"function": {"name": "terminal"}}]
    assert policy.filter_tool_definitions(definitions) is definitions
    args = {"command": "true"}
    assert run_tool_execution_middleware(
        "terminal", args, lambda payload: payload
    ) is args


@pytest.mark.parametrize("drift", ["identity", "config", "overlay"])
def test_activation_identity_config_and_overlay_drift_fail_closed(
    tmp_path, monkeypatch, drift
):
    _, home = _install_profile(tmp_path, monkeypatch)
    if drift == "identity":
        monkeypatch.setenv("HERMES_PROFILE", "ordinary")
    elif drift == "config":
        text = (home / "config.yaml").read_text(encoding="utf-8")
        (home / "config.yaml").write_text(
            text.replace("reasoning_effort: high", "reasoning_effort: medium"),
            encoding="utf-8",
        )
    else:
        (home / policy.OVERLAY_RELATIVE_PATH / "hardening.py").write_text(
            "# drift\n", encoding="utf-8"
        )

    with pytest.raises(policy.ProfileRuntimePolicyError):
        policy.load_active_profile_runtime_policy()


def test_overlay_hardening_executes_the_same_bytes_that_were_verified(
    tmp_path, monkeypatch
):
    _, home = _install_profile(tmp_path, monkeypatch)
    hardening_path = home / policy.OVERLAY_RELATIVE_PATH / "hardening.py"
    overlay = hardening_path.parent
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text
    replaced = False
    reads = {}

    def replacing_read_bytes(path):
        nonlocal replaced
        if path.parent == overlay:
            reads[path.name] = reads.get(path.name, 0) + 1
        source = original_read_bytes(path)
        if path == hardening_path and not replaced:
            replaced = True
            hardening_path.write_bytes(b"raise RuntimeError('unverified reread')\n")
        return source

    def rejecting_overlay_read_text(path, *args, **kwargs):
        if path.parent == overlay:
            raise AssertionError("overlay bytes must not be reopened as text")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", replacing_read_bytes)
    monkeypatch.setattr(Path, "read_text", rejecting_overlay_read_text)
    loaded = policy.load_active_profile_runtime_policy()

    assert replaced is True
    assert loaded.hardening.classify_architect_fit(
        {"work_kind": "ontology"}
    ).accepted is True
    assert reads == {name: 1 for name in policy.EXPECTED_OVERLAY_HASHES}


def test_profile_local_disabled_skills_invariant_fails_closed(
    tmp_path, monkeypatch
):
    _, home = _install_profile(tmp_path, monkeypatch)
    config = (home / "config.yaml").read_text(encoding="utf-8")
    (home / "config.yaml").write_text(
        config.replace("    - mobile-ui-verification\n", ""),
        encoding="utf-8",
    )

    with pytest.raises(
        policy.ProfileRuntimePolicyError, match="skills.disabled"
    ):
        policy.load_active_profile_runtime_policy()


@pytest.mark.parametrize("value", ["mentions", " ALL "])
def test_inherited_telegram_bot_bypass_rejects_activation(
    tmp_path, monkeypatch, value
):
    _install_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEGRAM_ALLOW_BOTS", value)

    with pytest.raises(
        policy.ProfileRuntimePolicyError,
        match="TELEGRAM_ALLOW_BOTS",
    ):
        policy.load_active_profile_runtime_policy()


def test_unset_or_none_telegram_bot_policy_accepts_activation(
    tmp_path, monkeypatch
):
    _install_profile(tmp_path, monkeypatch)
    monkeypatch.delenv("TELEGRAM_ALLOW_BOTS", raising=False)
    assert policy.load_active_profile_runtime_policy() is not None

    monkeypatch.setenv("TELEGRAM_ALLOW_BOTS", " NoNe ")
    assert policy.load_active_profile_runtime_policy() is not None
