from __future__ import annotations

import re
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import execution_state as es
from hermes_cli import kanban_db as kb
from hermes_cli import outcome_operating_model as oom


def _task(
    task_id: str,
    *,
    body: str = "",
    project_id: str = "p_demo",
    workspace_path: str = "/repo/.worktrees/t_demo",
    status: str = "ready",
    created_at: int = 1,
    priority: int = 0,
):
    return SimpleNamespace(
        id=task_id,
        body=body,
        project_id=project_id,
        workspace_path=workspace_path,
        status=status,
        created_at=created_at,
        priority=priority,
    )


def _claim(task_id: str, *, workspace_path: str | None = None, **markers):
    body = "\n".join(f"{key}: {value}" for key, value in markers.items())
    if workspace_path is None:
        logical = str(markers.get("shared_authority_scope") or markers.get("authority_scope") or task_id)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", logical).strip("-") or task_id
        workspace_path = f"/repos/{safe}"
    return oom.task_execution_claim(
        _task(task_id, body=body, workspace_path=workspace_path), board="demo"
    )


def _resume_receipt(**overrides):
    receipt = {
        "project": "Demo",
        "outcome": "Usable slice",
        "last_verified_result": "V1 route passed representative fixture",
        "repository": {
            "path": "/repo",
            "branch": "feature/demo",
            "worktree": "/repo/.worktrees/demo",
            "commit": "abc123",
        },
        "works": ["primary flow"],
        "not_done": ["polish"],
        "frozen_acceptance": ["primary flow usable"],
        "next_action": "Run user check",
        "dependencies": [],
        "risks": ["direct Codex unavailable"],
        "deploy_state": "not deployed",
        "terminal_history": ["t_old/run_7 iteration_exhausted terminal"],
        "execution_mode": "direct",
    }
    receipt.update(overrides)
    return receipt


def _outcome(outcome_id: str, *, tier="focus", kind="normal", **extra):
    payload = {
        "contract_id": outcome_id,
        "outcome_id": outcome_id,
        "outcome_tier": tier,
        "outcome_kind": kind,
        "outcome_owner": "default",
        "maturity": "V1",
        "execution_mode": "direct",
    }
    payload.update(extra)
    return payload


def test_legacy_kanban_task_fails_closed_as_mutating_durable_without_guessing_tier():
    claim = oom.task_execution_claim(_task("t_legacy"), board="default")

    assert claim.mode is oom.ExecutionMode.DURABLE
    assert claim.access is oom.ExecutionAccess.MUTATING
    assert claim.tier is None
    assert claim.owner == "default"
    assert claim.authority_scope == "workspace:/repo"
    assert claim.outcome_id == "task:t_legacy"


def test_more_than_three_projects_are_not_a_policy_error_but_fourth_focus_is():
    projects = [f"p_{index}" for index in range(7)]
    assert len(projects) > 3

    payloads = [_outcome(f"o_{index}") for index in range(4)]
    errors = oom.validate_focus_set(payloads)
    assert errors == ["normal focus outcomes 4 exceed cap 3"]

    payloads[3]["outcome_tier"] = "warm"
    payloads[3]["resume_receipt"] = _resume_receipt()
    assert oom.validate_focus_set(payloads) == []


def test_warm_or_cold_outcome_cannot_start_mutating_execution():
    for tier in ("warm", "cold"):
        candidate = _claim(
            f"t_{tier}",
            outcome_id=f"o_{tier}",
            outcome_tier=tier,
            maturity="V1",
            execution_mode="durable",
            execution_access="mutating",
            authority_scope=f"repo:{tier}",
        )
        decision = oom.admit_execution(candidate)
        assert decision.allowed is False
        assert decision.reason == f"{tier}_outcome_cannot_mutate"


def test_read_only_specialist_can_run_separately_from_mutating_capacity_and_scope():
    active = _claim(
        "t_mutating",
        outcome_id="o_a",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
        authority_scope="repo:shared",
    )
    candidate = _claim(
        "t_review",
        outcome_id="o_b",
        outcome_tier="warm",
        execution_mode="specialist",
        execution_access="read_only",
        authority_scope="repo:shared",
    )

    decision = oom.admit_execution(candidate, active_claims=[active])
    assert decision.allowed is True
    assert decision.reason == "read_only_separate_capacity"


def test_read_only_specialist_can_run_when_three_mutating_slots_are_full():
    active = [
        _claim(
            f"t_mutating_{index}",
            outcome_id=f"o_mutating_{index}",
            outcome_tier="focus",
            execution_mode="durable",
            execution_access="mutating",
            authority_scope=f"repo:mutating:{index}",
        )
        for index in range(3)
    ]
    candidate = _claim(
        "t_read_only_full",
        outcome_id="o_read_only",
        outcome_tier="warm",
        execution_mode="specialist",
        execution_access="read_only",
        authority_scope="repo:mutating:0",
    )

    decision = oom.admit_execution(candidate, active_claims=active)

    assert decision.allowed is True
    assert decision.reason == "read_only_separate_capacity"


def test_global_three_mutating_workers_and_one_per_authority_scope_fail_closed():
    active_a = _claim(
        "t_a",
        outcome_id="o_a",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
        authority_scope="repo:a",
    )
    active_b = _claim(
        "t_b",
        outcome_id="o_b",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
        authority_scope="repo:b",
    )
    third = _claim(
        "t_c",
        outcome_id="o_c",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
        authority_scope="repo:c",
    )
    fourth = _claim(
        "t_d",
        outcome_id="o_d",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
        authority_scope="repo:d",
    )
    same_scope = _claim(
        "t_same",
        outcome_id="o_same",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
        authority_scope="repo:a",
    )

    third_slot = oom.admit_execution(third, active_claims=[active_a, active_b])
    capacity = oom.admit_execution(fourth, active_claims=[active_a, active_b, third])
    collision = oom.admit_execution(same_scope, active_claims=[active_a])

    assert third_slot.allowed is True
    assert third_slot.reason == "mutating_capacity_available"
    assert third_slot.active_mutating == 2
    assert capacity.allowed is False
    assert capacity.reason == "global_mutating_capacity"
    assert capacity.active_mutating == 3
    assert collision.allowed is False
    assert collision.reason == "authority_scope_collision"
    assert collision.collides_with == ("t_a",)


def test_same_repository_collides_even_when_explicit_shared_scopes_differ():
    active = _claim(
        "t_repo_a",
        workspace_path="/repos/shared-repo/.worktrees/a",
        outcome_id="o_a",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
        authority_scope="logical:a",
    )
    candidate = _claim(
        "t_repo_b",
        workspace_path="/repos/shared-repo/.worktrees/b",
        outcome_id="o_b",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
        authority_scope="logical:b",
    )

    decision = oom.admit_execution(candidate, active_claims=[active])

    assert active.repository_scope == candidate.repository_scope == "workspace:/repos/shared-repo"
    assert active.shared_authority_scope == "logical:a"
    assert candidate.shared_authority_scope == "logical:b"
    assert decision.allowed is False
    assert decision.reason == "authority_scope_collision"


def test_cross_repository_same_shared_authority_scope_collides():
    active = _claim(
        "t_shared_a",
        workspace_path="/repos/a",
        outcome_id="o_a",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
        shared_authority_scope="finance-ledger",
    )
    candidate = _claim(
        "t_shared_b",
        workspace_path="/repos/b",
        outcome_id="o_b",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
        shared_authority_scope="finance-ledger",
    )

    decision = oom.admit_execution(candidate, active_claims=[active])

    assert active.repository_scope != candidate.repository_scope
    assert decision.allowed is False
    assert decision.reason == "authority_scope_collision"
    assert decision.collides_with == ("t_shared_a",)


def test_cross_repository_different_or_empty_shared_scopes_can_run_when_capacity_exists():
    active = _claim(
        "t_distinct_a",
        workspace_path="/repos/a",
        outcome_id="o_a",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
        shared_authority_scope="ledger-a",
    )
    different = _claim(
        "t_distinct_b",
        workspace_path="/repos/b",
        outcome_id="o_b",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
        shared_authority_scope="ledger-b",
    )
    no_shared = _claim(
        "t_distinct_c",
        workspace_path="/repos/c",
        outcome_id="o_c",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
    )

    assert oom.admit_execution(different, active_claims=[active]).allowed is True
    assert oom.admit_execution(no_shared, active_claims=[active]).allowed is True


def test_separate_git_worktrees_from_same_common_dir_collide(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test", "commit", "--allow-empty", "-qm", "init",
        ],
        check=True,
    )
    wt_a = tmp_path / "wt-a"
    wt_b = tmp_path / "wt-b"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-qb", "a", str(wt_a)], check=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-qb", "b", str(wt_b)], check=True)

    active = _claim(
        "t_wt_a",
        workspace_path=str(wt_a),
        outcome_id="o_a",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
        authority_scope="logical:a",
    )
    candidate = _claim(
        "t_wt_b",
        workspace_path=str(wt_b),
        outcome_id="o_b",
        outcome_tier="focus",
        execution_mode="durable",
        execution_access="mutating",
        authority_scope="logical:b",
    )

    decision = oom.admit_execution(candidate, active_claims=[active])

    assert active.repository_scope == candidate.repository_scope
    assert active.repository_scope.startswith("git:")
    assert decision.allowed is False
    assert decision.collides_with == ("t_wt_a",)


def test_incident_requests_preemption_instead_of_becoming_fourth_mutating_worker():
    active = [
        _claim(
            f"t_{index}",
            outcome_id=f"o_{index}",
            outcome_tier="focus",
            execution_mode="durable",
            execution_access="mutating",
            authority_scope=f"repo:{index}",
        )
        for index in range(3)
    ]
    incident = _claim(
        "t_incident",
        outcome_id="incident-1",
        outcome_tier="focus",
        outcome_kind="incident",
        execution_mode="durable",
        execution_access="mutating",
        authority_scope="repo:incident",
    )

    decision = oom.admit_execution(incident, active_claims=active)

    assert decision.allowed is False
    assert decision.reason == "incident_preemption_required"
    assert decision.preempt_required is True
    assert decision.active_mutating == 3


def test_over_capacity_focus_set_requires_owner_instead_of_guessing_priority():
    portfolio = [
        _claim(
            f"t_{index}",
            outcome_id=f"o_{index}",
            outcome_tier="focus",
            execution_mode="durable",
            execution_access="mutating",
            authority_scope=f"repo:{index}",
        )
        for index in range(4)
    ]

    decision = oom.admit_execution(portfolio[-1], portfolio_claims=portfolio)
    assert decision.allowed is False
    assert decision.reason == "focus_set_over_capacity_requires_owner"


def test_warm_resume_uses_receipt_and_current_repo_truth_without_touching_terminal_history():
    payload = _outcome(
        "o_warm",
        tier="warm",
        resume_receipt=_resume_receipt(),
    )
    new_repo_truth = {
        "path": "/repo",
        "branch": "feature/demo",
        "worktree": "/repo/.worktrees/demo",
        "commit": "def456",
    }

    resumed = oom.resume_warm_outcome(payload, current_repository_truth=new_repo_truth)

    assert resumed["outcome_tier"] == "focus"
    assert resumed["resume_receipt"]["repository"] == new_repo_truth
    assert resumed["resume_receipt"]["terminal_history"] == payload["resume_receipt"]["terminal_history"]
    assert "reactivate" not in resumed
    assert payload["outcome_tier"] == "warm"


def test_warm_outcome_requires_complete_resume_receipt():
    payload = _outcome("o_warm", tier="warm", resume_receipt={"project": "Demo"})
    errors = oom.validate_outcome_state_payload(payload)
    assert any("missing last_verified_result" in item for item in errors)
    assert any("missing execution_mode" in item for item in errors)


def test_warm_receipt_terminal_history_must_be_structured_not_free_text():
    payload = _outcome(
        "o_warm",
        tier="warm",
        resume_receipt=_resume_receipt(terminal_history="run 7 terminal"),
    )
    errors = oom.validate_outcome_state_payload(payload)
    assert "resume_receipt terminal_history must be a sequence" in errors


def test_terminal_worker_evidence_cannot_close_outcome_without_outcome_receipt():
    payload = _outcome("o_done", outcome_result="delivered")
    errors = oom.validate_outcome_state_payload(payload)
    assert "delivered outcome requires outcome_receipt" in errors

    payload["outcome_receipt"] = {
        "verified_user_result": "User-visible output verified",
        "evidence": ["tests green", "representative fixture"],
    }
    assert oom.validate_outcome_state_payload(payload) == []


def test_repo_execution_marker_reuses_existing_canon_and_validates_outcome_metadata(tmp_path):
    canon = tmp_path / "TASKS.md"
    canon.write_text("# Tasks\n", encoding="utf-8")
    payload = _outcome(
        "o_repo",
        tier="warm",
        resume_receipt=_resume_receipt(),
        status="active",
        revision="r1",
    )

    es.upsert_repo_execution_state(canon, payload)
    states = es.read_repo_execution_states(canon)

    assert states["o_repo"]["contract_id"] == "o_repo"
    assert states["o_repo"]["outcome_tier"] == "warm"
    assert states["o_repo"]["resume_receipt"]["next_action"] == "Run user check"


def test_plain_historical_repo_execution_marker_still_validates_without_outcome_fields(tmp_path):
    canon = tmp_path / "TASKS.md"
    canon.write_text("# Tasks\n", encoding="utf-8")
    es.upsert_repo_execution_state(
        canon,
        {"contract_id": "legacy", "revision": "r1", "status": "active"},
    )
    assert es.read_repo_execution_states(canon)["legacy"]["contract_id"] == "legacy"


@pytest.mark.parametrize(
    ("routing_request", "mode", "executor"),
    [
        (oom.RoutingRequest(repositories=1, frozen_acceptance=True), "direct", "codex"),
        (oom.RoutingRequest(repositories=2, must_survive_session=True), "durable", "dollycode"),
        (oom.RoutingRequest(deploy=True), "ops", "dollyops"),
        (oom.RoutingRequest(design_trigger=True), "specialist", "dollydesign"),
        (oom.RoutingRequest(research_trigger=True), "specialist", "dollyresearch"),
        (
            oom.RoutingRequest(architect_triggers=("shared_data_ownership",)),
            "specialist",
            "dollyarchitect",
        ),
        (
            oom.RoutingRequest(phase="review", independent_qa_trigger=True),
            "specialist",
            "dollyqa",
        ),
    ],
)
def test_execution_routing_is_single_path_and_default_remains_outcome_owner(routing_request, mode, executor):
    decision = oom.choose_execution_mode(routing_request)
    assert decision.mode.value == mode
    assert decision.executor == executor
    assert decision.owner == "default"
    assert decision.automatic_chain == ()


def test_local_feature_without_system_boundary_does_not_trigger_architect():
    decision = oom.choose_execution_mode(
        oom.RoutingRequest(repositories=1, frozen_acceptance=True, bounded_session=True)
    )
    assert decision.mode is oom.ExecutionMode.DIRECT
    assert decision.executor == "codex"


def test_bounded_review_allows_ship_with_accepted_risk_and_caps_blockers():
    verdict = oom.bounded_review_verdict([], accepted_risks=["direct Codex E2E unavailable"])
    assert verdict.verdict == "SHIP_WITH_ACCEPTED_RISK"

    with pytest.raises(ValueError, match="at most three blockers"):
        oom.bounded_review_verdict(["b1", "b2", "b3", "b4"])


def test_compact_portfolio_surface_has_only_outcome_relevant_information():
    focus = _outcome(
        "o_focus",
        project="Shroomie",
        outcome="Photo recognition V1",
        last_verified_evidence="fixture passed",
        next_action="field test",
        run_id=99,
        worker_pid=123,
        event_cursor=456,
    )
    incident = _outcome(
        "incident-1",
        kind="incident",
        project="HWapi",
        outcome="Invoice regression",
        preempts="Photo recognition V1",
    )
    warm = _outcome(
        "o_warm",
        tier="warm",
        project="Fisketur",
        outcome="Guide",
        resume_receipt=_resume_receipt(),
    )
    warm["decision_required"] = "Choose whether Fisketur returns to focus"

    rendered = oom.render_portfolio_status([focus, incident, warm])

    assert "FOCUS:" in rendered
    assert "INCIDENT:" in rendered
    assert "WARM:" in rendered
    assert "DECISIONS:" in rendered
    assert "V1/direct" in rendered
    assert "fixture passed" in rendered
    assert "preempts: Photo recognition V1" in rendered
    assert "run_id" not in rendered
    assert "worker_pid" not in rendered
    assert "event_cursor" not in rendered


def test_compact_portfolio_surface_turns_focus_overflow_into_owner_decision():
    rendered = oom.render_portfolio_status(
        [_outcome(f"o_{index}", project=f"P{index}") for index in range(4)]
    )

    assert rendered.count("V1/direct") == 3
    assert "Choose focus priority: 4 normal outcomes exceed the cap of 3." in rendered


def test_current_portfolio_projection_reuses_repo_canon_and_marks_legacy_running_not_verified(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    repo = tmp_path / "project"
    repo.mkdir()
    projects = sqlite3.connect(home / "projects.db")
    projects.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, primary_path TEXT, archived INTEGER)"
    )
    projects.execute(
        "INSERT INTO projects VALUES(?,?,?,0)",
        ("p_demo", "Demo project", str(repo)),
    )
    projects.commit()
    projects.close()

    canon = repo / "TASKS.md"
    canon.write_text("# Tasks\n", encoding="utf-8")
    es.upsert_repo_execution_state(
        canon,
        _outcome(
            "o_warm",
            tier="warm",
            project="Demo project",
            outcome="Warm slice",
            resume_receipt=_resume_receipt(),
            status="active",
            revision="r1",
        ),
    )

    db = home / "kanban" / "boards" / "demo" / "kanban.db"
    db.parent.mkdir(parents=True)
    kb.init_db(db_path=db)
    with kb.connect_closing(db_path=db) as con:
        task_id = kb.create_task(
            con,
            title="Legacy active implementation",
            assignee="dollycode",
            project_id="p_demo",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        assert kb.claim_task(con, task_id) is not None

    payloads = oom.current_portfolio_payloads(home)
    rendered = oom.render_portfolio_status(payloads)

    assert any(item.get("outcome_id") == "o_warm" for item in payloads)
    legacy = next(item for item in payloads if item.get("outcome_id") == f"task:{task_id}")
    assert legacy["outcome_tier"] == "focus"
    assert legacy["execution_mode"] == "not_verified"
    assert "Warm slice" in rendered
    assert "Legacy active implementation" in rendered
    assert "Dolly/default must map active legacy work" in rendered
