"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp
from hermes_cli import projects_db
from agent import profile_runtime_policy as policy


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


def test_decompose_with_fanout_creates_children(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_decompose_fanout_false_assigns_default_when_unassigned(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="just one thing", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "**Goal**\nDo the thing.",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is False
    assert outcome.new_title == "Tightened title"
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    # specify path with no parents -> recompute_ready flips to 'ready'
    assert task.status == "ready"
    assert task.title == "Tightened title"
    assert task.assignee == "fallback"


def test_decompose_fanout_false_preserves_existing_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="already routed",
            assignee="engineer",
            triage=True,
        )

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Keep existing lane.",
        "assignee": "fallback",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "engineer"
    assert task.title == "Tightened title"


def test_decompose_fanout_false_uses_valid_llm_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to specialist.",
        "assignee": "engineer",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "engineer"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"


def test_decompose_unknown_assignee_falls_back_to_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    # Roster only has 'orchestrator' and 'fallback'; LLM picks 'made_up'.
    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test",
        "tasks": [
            {"title": "do X", "body": "", "assignee": "made_up", "parents": []},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with patch.dict(
            "os.environ", {}, clear=False,
        ), _patch_aux_client(llm_payload), _patch_extra_body(), \
            patch(
                "hermes_cli.kanban_decompose._load_config",
                return_value={
                    "kanban": {
                        "orchestrator_profile": "orchestrator",
                        "default_assignee": "fallback",
                    }
                },
            ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 1
    with kb.connect() as conn:
        child = kb.get_task(conn, outcome.child_ids[0])
    # 'made_up' wasn't in roster, so assignee rewritten to 'fallback'
    assert child.assignee == "fallback"


def _structured_task(work_kind: str, *, title: str) -> dict:
    return {
        "title": title,
        "body": f"Execute the explicit {work_kind} contract.",
        "assignee": "orchestrator",
        "work_kind": work_kind,
        "requested_actions": (
            ["architecture_decision"]
            if work_kind == "cross_repo_contract"
            else []
        ),
        "architecture_document_path": None,
        "bounded_file_cluster": ["agent/profile_runtime_policy.py"],
        "non_goals": ["Release or deploy"],
        "parents": [],
    }


def _install_architect_profile(home: Path) -> Path:
    profile_home = home / "profiles" / "dollyarchitect"
    overlay = profile_home / policy.OVERLAY_RELATIVE_PATH
    overlay.mkdir(parents=True)
    source = (
        Path(__file__).parents[2]
        / "profile_candidates"
        / "dollyarchitect"
    )
    for name in policy.EXPECTED_OVERLAY_HASHES:
        shutil.copyfile(source / name, overlay / name)
    (profile_home / "config.yaml").write_text(
        """
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
  allow_from: ["123"]
  group_policy: disabled
skills:
  disabled:
    - contract-driven-frontend-implementation
    - external-upstream-pr-recuts
    - mobile-ui-verification
    - release-candidate-evidence
""".lstrip(),
        encoding="utf-8",
    )
    return profile_home


def test_structured_work_kinds_materialize_contract_and_named_routes(
    kanban_home, tmp_path
):
    implementation_repo = tmp_path / "implementation-repo"
    implementation_repo.mkdir()
    (implementation_repo / ".git").mkdir()
    with projects_db.connect_closing() as project_conn:
        project_id = projects_db.create_project(
            project_conn,
            name="Target binding",
            primary_path=str(implementation_repo),
        )
    workspace = tmp_path / "source-workspace"
    workspace.mkdir()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Route an architecture program",
            workspace_kind="scratch",
            workspace_path=str(workspace),
            project_id=project_id,
            triage=True,
        )
    expected = {
        "cross_repo_contract": "dollyarchitect",
        "implementation_code_patch": "dollycode",
        "benchmark": "dollyqa",
        "routine_qa_review": "dollyqa",
        "visual_design": "dollydesign",
        "pull_request": "dollyops",
        "release": "dollyops",
        "deploy": "dollyops",
    }
    payload = jsonlib.dumps(
        {
            "fanout": True,
            "rationale": "Explicit controller routing",
            "tasks": [
                _structured_task(kind, title=f"Route {kind}")
                for kind in expected
            ],
        }
    )
    names = ["orchestrator", "dollyarchitect", "dollycode", "dollyqa", "dollydesign", "dollyops"]
    patches = _patch_list_profiles(names)
    for item in patches:
        item.start()
    try:
        with _patch_aux_client(payload):
            outcome = decomp.decompose_task(tid, author="router")
    finally:
        for item in patches:
            item.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        children = [kb.get_task(conn, child_id) for child_id in outcome.child_ids]
    assert {
        kind: child.assignee
        for kind, child in zip(expected, children)
    } == expected
    architect = children[0]
    assert architect is not None
    assert architect.body.count(policy.DISPATCH_MARKER) == 2
    contract = policy._parse_exact_json_block(
        architect.body, policy.DISPATCH_MARKER
    )
    assert contract["requested_actions"] == ["architecture_decision"]
    assert contract["writable_artifact_roots"] == []
    assert contract["architecture_document_paths"] == []
    assert contract["project_id"] == project_id
    assert contract["implementation_repo"] == str(implementation_repo)
    assert contract["implementation_workspace_policy"] == (
        "project_primary_repo_worktree"
    )
    assert architect.project_id == project_id

    profile_home = _install_architect_profile(kanban_home)
    spawned_workspace = tmp_path / "architect-workspace"
    spawned_workspace.mkdir()
    architect.workspace_path = str(spawned_workspace)
    architect.current_run_id = 1
    payload = policy.prepare_dollyarchitect_spawn_env(
        task=architect,
        workspace=str(spawned_workspace),
        profile_name="dollyarchitect",
        hermes_home=profile_home,
    )
    assert jsonlib.loads(payload)["contract"]["contract_id"] == contract["contract_id"]


def test_architect_route_rejects_unbound_source_task(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Unbound architecture program",
            triage=True,
        )
    payload = jsonlib.dumps(
        {
            "fanout": True,
            "rationale": "Must not invent a target",
            "tasks": [_structured_task("cross_repo_contract", title="Architect")],
        }
    )
    patches = _patch_list_profiles(["orchestrator", "dollyarchitect"])
    for item in patches:
        item.start()
    try:
        with _patch_aux_client(payload):
            outcome = decomp.decompose_task(tid, author="router")
    finally:
        for item in patches:
            item.stop()

    assert outcome.ok is False
    assert "trusted source project binding" in outcome.reason
    with kb.connect() as conn:
        source = kb.get_task(conn, tid)
    assert source is not None
    assert source.status == "triage"


def test_decompose_handles_malformed_llm_json(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client("not json at all, sorry"), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "malformed JSON" in outcome.reason


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False
    assert "not in triage" in outcome.reason


def test_decompose_no_aux_client_configured(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        # call_llm raises RuntimeError when no provider is configured; the
        # decomposer must convert that into a failed outcome, not a crash.
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=RuntimeError("No LLM provider configured"),
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    # call_llm's no-provider RuntimeError surfaces via the LLM-error branch.
    assert "LLM error" in outcome.reason
