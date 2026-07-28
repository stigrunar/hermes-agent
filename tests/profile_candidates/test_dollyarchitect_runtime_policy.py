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
    policy.reset_runtime_policy_state_for_tests()
    model_tools._clear_tool_defs_cache()
    return root, home


def _contract(workspace: Path, *, work_kind: str = "cross_repo_contract",
              workspace_kind: str = "scratch", actions=None) -> dict:
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    documents = (
        [str(artifacts / "architecture.md")]
        if workspace_kind == "worktree"
        else []
    )
    return {
        "contract_id": "runtime-contract-1",
        "work_kind": work_kind,
        "workspace_kind": workspace_kind,
        "writable_artifact_roots": [str(artifacts)],
        "architecture_document_paths": documents,
        "requested_actions": actions or ["architecture_decision"],
        "implementation_owner": None,
        "operations_owner": None,
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
        current_run_id=run_id,
    )


def _activate_worker(tmp_path: Path, monkeypatch, *, workspace_kind="scratch"):
    root, home = _install_profile(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    contract = _contract(workspace, workspace_kind=workspace_kind)
    task = _task(workspace, contract)
    payload = policy.prepare_dollyarchitect_spawn_env(
        task=task,
        workspace=str(workspace),
        profile_name="dollyarchitect",
        hermes_home=home,
    )
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


@pytest.mark.parametrize(
    ("work_kind", "owner"),
    [("implementation_code_patch", "DollyCode"), ("deploy", "DollyOps")],
)
def test_nonfit_work_reroutes_before_popen(
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
    with pytest.raises(policy.ProfileRuntimePolicyError, match="mixed architecture"):
        kb._default_spawn(task, str(workspace))
    assert called is False


def test_common_boundary_allows_scratch_write_and_rejects_escape(
    tmp_path, monkeypatch
):
    _, _, workspace, _ = _activate_worker(tmp_path, monkeypatch)
    artifacts = workspace / "artifacts"
    target = artifacts / "decision.md"

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
        tmp_path, monkeypatch, workspace_kind="worktree"
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
        "write_file",
        "patch",
        "kanban_show",
        "kanban_create",
        "kanban_complete",
        "kanban_block",
    } <= names
    assert "terminal" not in names
    assert "execute_code" not in names
    assert "delegate_task" not in names

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


def _decision_packet() -> dict:
    return {
        "packet_id": "packet-1",
        "decision": "Use one narrow runtime policy seam.",
        "rationale": "Keep ordinary profiles unchanged.",
        "constraints": ["No implementation in DollyArchitect."],
        "acceptance_criteria": ["DollyCode receives one canonical handoff."],
        "dollycode_owner": "DollyCode",
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
    assert first["ok"] is True
    emitted_body = captured[0]["body"]
    assert policy.HANDOFF_MARKER in emitted_body
    emitted = json.loads(emitted_body.splitlines()[1])
    assert emitted["implementation_actions"] == []
    assert captured[0]["assignee"] == "dollycode"
    assert captured[0]["parents"] == ["t_architect_runtime"]

    second = json.loads(
        run_tool_execution_middleware("kanban_create", create_args, create_terminal)
    )
    assert "exactly one DollyCode handoff" in second["error"]
    assert len(captured) == 1

    completed = run_tool_execution_middleware(
        "kanban_complete", {"summary": "handoff emitted"}, lambda args: "complete"
    )
    assert completed == "complete"


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
