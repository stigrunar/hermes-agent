"""Focused proof for schema-v2 roadmap binding admission."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.project_execution_policy import (
    canonical_execution_preflight,
    resolve_project_execution_policy,
    validate_roadmap_binding,
)


def _binding(repo: Path) -> dict:
    return {
        "project_id": "project-1",
        "lane_id": "lane-1",
        "roadmap_revision": "r1",
        "canonical_ref": "origin/main",
        "base_commit": "a" * 40,
        "acceptance_ref": "docs/current/acceptance.md",
        "implementation_repo": "owner/project-1",
        "path_scope": ["hermes_cli/**"],
        "dependency_pins": [{
            "project": "dependency-1",
            "repo": "owner/dependency-1",
            "commit": "b" * 40,
            "path": "docs/contract.md",
            "blob": "c" * 40,
        }],
    }


def _register(repo: Path) -> None:
    path = repo / "docs" / "current"
    path.mkdir(parents=True, exist_ok=True)
    (path / "workstream-register.json").write_text(
        json.dumps({
            "schema_version": "schema-v2",
            "mutation_admission": {
                "required": True,
                "validator": "scripts/check_workstream_admission.py",
            },
        }),
        encoding="utf-8",
    )


def _admission(repo: Path) -> dict:
    raw = (repo / "docs" / "current" / "workstream-register.json").read_bytes()
    return {
        "schema": "schema-v2",
        "required": True,
        "validator": "scripts/check_workstream_admission.py",
        "register_digest": hashlib.sha256(raw).hexdigest(),
    }


def test_binding_is_closed_and_paths_are_portable(tmp_path: Path) -> None:
    value = validate_roadmap_binding(_binding(tmp_path))
    assert value["implementation_repo"] == "owner/project-1"
    assert value["path_scope"] == ["hermes_cli/**"]
    with pytest.raises(ValueError, match="unknown field"):
        validate_roadmap_binding({**value, "extra": "nope"})
    with pytest.raises(ValueError, match="missing or empty"):
        validate_roadmap_binding({key: value[key] for key in value if key != "lane_id"})
    with pytest.raises(ValueError, match="repository identifier"):
        validate_roadmap_binding({**value, "implementation_repo": str(tmp_path)})
    with pytest.raises(ValueError, match="repository identifier"):
        validate_roadmap_binding({**value, "implementation_repo": "relative"})
    with pytest.raises(ValueError, match="repository-relative"):
        validate_roadmap_binding({**value, "path_scope": [str(tmp_path)]})
    with pytest.raises(ValueError, match="repository-relative"):
        validate_roadmap_binding({**value, "path_scope": ["../outside/**"]})


@pytest.mark.parametrize(
    ("pins", "message"),
    [
        (["dependency-1"], "must be an object"),
        ([{"project": "p", "commit": "c", "path": "x"}], "missing or empty"),
        ([{"project": "p", "commit": "c", "path": "x", "blob": ""}], "missing or empty"),
        ([{"project": "p", "commit": "c", "path": "x", "blob": "b", "extra": "x"}], "unknown field"),
        ([{"project": "p", "commit": "c", "path": "x", "blob": "b", "repo": ""}], "repo must be a non-empty string"),
    ],
)
def test_binding_rejects_malformed_dependency_pins(
    tmp_path: Path, pins: list, message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_roadmap_binding({**_binding(tmp_path), "dependency_pins": pins})


def test_binding_preserves_dependency_pin_literals_and_canonical_key_order(
    tmp_path: Path,
) -> None:
    pin = {
        "blob": " blob literal ",
        "repo": " Owner/Repo ",
        "path": " docs/Contract.md ",
        "commit": " Commit-Literal ",
        "project": " Project Literal ",
    }
    value = validate_roadmap_binding({**_binding(tmp_path), "dependency_pins": [pin]})
    assert value["dependency_pins"] == [{
        "project": " Project Literal ",
        "commit": " Commit-Literal ",
        "path": " docs/Contract.md ",
        "blob": " blob literal ",
        "repo": " Owner/Repo ",
    }]


def test_bound_worktree_branch_starts_at_exact_base_not_ambient_head(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str, cwd: Path = repo) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        )
        return completed.stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Roadmap Binding Test")
    git("config", "user.email", "roadmap-binding@example.invalid")
    tracked = repo / "tracked.txt"
    tracked.write_text("bound base\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-qm", "bound base")
    base_commit = git("rev-parse", "HEAD")
    tracked.write_text("ambient unrelated head\n", encoding="utf-8")
    git("commit", "-qam", "ambient head")
    ambient_head = git("rev-parse", "HEAD")
    assert ambient_head != base_commit

    task = cast(kb.Task, SimpleNamespace(execution_preflight={
        "roadmap_binding": {**_binding(repo), "base_commit": base_commit},
    }))
    assert kb._roadmap_worktree_base(task) == base_commit

    target = repo / ".worktrees" / "bound-task"
    try:
        kb._ensure_git_worktree(
            repo, target, "test/bound-task", start_point=base_commit,
        )
        assert git("rev-parse", "HEAD", cwd=target) == base_commit
        assert git(
            "merge-base", "--is-ancestor", base_commit, "HEAD", cwd=target,
        ) == ""
    finally:
        if target.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(target)],
                cwd=repo, check=False, capture_output=True, text=True,
            )


def test_existing_worktree_with_unrelated_history_rejects_bound_base(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str, cwd: Path = repo) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        )
        return completed.stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Roadmap Binding Test")
    git("config", "user.email", "roadmap-binding@example.invalid")
    tracked = repo / "tracked.txt"
    tracked.write_text("bound base\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-qm", "bound base")
    base_commit = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    unrelated_commit = git("commit-tree", tree, "-m", "unrelated root")
    target = repo / ".worktrees" / "unrelated-task"
    git(
        "worktree", "add", "-q", "-b", "test/unrelated-task",
        str(target), unrelated_commit,
    )
    try:
        with pytest.raises(RuntimeError, match="not descended from bound base"):
            kb._ensure_git_worktree(
                repo, target, "test/unrelated-task", start_point=base_commit,
            )
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(target)],
            cwd=repo, check=False, capture_output=True, text=True,
        )


def test_required_register_requires_binding_and_snapshots_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(tmp_path)
    calls: list[tuple[list[str], dict]] = []
    binding_files: list[Path] = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        binding_file = Path(argv[-1])
        binding_files.append(binding_file)
        assert stat.S_IMODE(binding_file.stat().st_mode) == 0o600
        assert json.loads(binding_file.read_text(encoding="utf-8")) == _binding(tmp_path)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "hermes_cli.project_execution_policy.subprocess.run",
        run,
    )
    with pytest.raises(ValueError, match="roadmap_binding is required"):
        resolve_project_execution_policy(tmp_path, {"action": "write"}, project_id="project-1")
    result = resolve_project_execution_policy(
        tmp_path,
        {"action": "write", "roadmap_binding": _binding(tmp_path)},
        project_id="project-1",
    )
    assert result is not None
    assert result["roadmap_binding"]["lane_id"] == "lane-1"
    assert result["roadmap_admission"]["required"] is True
    assert len(calls) == 1
    assert calls[0][0][1:3] == [
        "scripts/check_workstream_admission.py", "--source-ref",
    ]
    assert all(not path.exists() for path in binding_files)


def test_create_persists_exact_structured_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import projects_db as pdb

    home = tmp_path / ".hermes"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _register(repo)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    pdb._INITIALIZED_PATHS.clear()
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn, name="Bound Project", folders=[str(repo)],
        )
        project = pdb.get_project(project_conn, project_id)
    assert project is not None
    binding = {**_binding(repo), "project_id": project_id}
    observed_files: list[Path] = []

    def run(argv, **kwargs):
        binding_file = Path(argv[-1])
        observed_files.append(binding_file)
        assert stat.S_IMODE(binding_file.stat().st_mode) == 0o600
        assert json.loads(binding_file.read_text(encoding="utf-8")) == binding
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("hermes_cli.project_execution_policy.subprocess.run", run)
    kb._INITIALIZED_PATHS.clear()
    conn = kb.connect(db_path=home / "kanban.db")
    try:
        task_id = kb.create_task(
            conn,
            title="persist exact binding",
            body="Bound implementation body",
            project_id=project.slug,
            execution={"action": "write", "roadmap_binding": binding},
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.execution_preflight is not None
        assert task.execution_preflight["roadmap_binding"] == binding
        raw = conn.execute(
            "SELECT execution_preflight FROM tasks WHERE id=?", (task_id,),
        ).fetchone()["execution_preflight"]
        assert raw == canonical_execution_preflight(task.execution_preflight)
        assert json.loads(raw)["roadmap_binding"]["dependency_pins"] == binding["dependency_pins"]
        context = kb.build_worker_context(conn, task_id)
        assert context.index("Roadmap binding:") < context.index("## Body")
    finally:
        conn.close()
    assert observed_files and all(not path.exists() for path in observed_files)


@pytest.mark.parametrize("schema_version", [2, "v2", "schema-v2"])
def test_schema_v2_markers_activate_only_with_boolean_required(
    tmp_path: Path, schema_version: object,
) -> None:
    register = tmp_path / "docs" / "current" / "workstream-register.json"
    register.parent.mkdir(parents=True)
    register.write_text(
        json.dumps({
            "schema_version": schema_version,
            "mutation_admission": {
                "required": True,
                "validator": "scripts/check_workstream_admission.py",
            },
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="roadmap_binding is required"):
        resolve_project_execution_policy(
            tmp_path, {"action": "write"}, project_id="project-1",
        )

    register.write_text(
        json.dumps({
            "schema_version": schema_version,
            "mutation_admission": {
                "required": "true",
                "validator": "scripts/check_workstream_admission.py",
            },
        }),
        encoding="utf-8",
    )
    result = resolve_project_execution_policy(
        tmp_path, {"action": "write"}, project_id="project-1",
    )
    assert result is not None and "roadmap_admission" not in result


def test_non_integer_numeric_schema_version_does_not_activate(tmp_path: Path) -> None:
    register = tmp_path / "docs" / "current" / "workstream-register.json"
    register.parent.mkdir(parents=True)
    register.write_text(
        json.dumps({
            "schema_version": 2.0,
            "mutation_admission": {
                "required": True,
                "validator": "scripts/check_workstream_admission.py",
            },
        }),
        encoding="utf-8",
    )
    result = resolve_project_execution_policy(
        tmp_path, {"action": "write"}, project_id="project-1",
    )
    assert result is not None and "roadmap_admission" not in result


def test_claim_refreshes_and_cleans_restricted_binding_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    kb._INITIALIZED_PATHS.clear()
    repo = tmp_path / "repo"
    repo.mkdir()
    _register(repo)
    binding = _binding(repo)
    preflight = {
        "inputs": {"action": "write", "project_repo": str(repo)},
        "resolved": {"action": "write"},
        "roadmap_binding": binding,
        "roadmap_admission": _admission(repo),
    }
    conn = kb.connect(db_path=home / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="bound task", assignee="default")
        conn.execute(
            "UPDATE tasks SET project_id=?, execution_preflight=? WHERE id=?",
            ("project-1", canonical_execution_preflight(preflight), task_id),
        )
        conn.commit()
        calls: list[tuple[list[str], dict]] = []
        binding_files: list[Path] = []

        def run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            if argv[1] == "fetch":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[1] == "rev-parse":
                # A moved canonical branch is not itself drift; the
                # project-owned validator decides whether base_commit is
                # still admissible.
                return SimpleNamespace(returncode=0, stdout="b" * 40 + "\n", stderr="")
            binding_file = Path(argv[-1])
            binding_files.append(binding_file)
            assert stat.S_IMODE(binding_file.stat().st_mode) == 0o600
            assert json.loads(binding_file.read_text(encoding="utf-8")) == binding
            return SimpleNamespace(returncode=0, stdout="OK", stderr="")

        monkeypatch.setattr("hermes_cli.kanban_db.subprocess.run", run)
        assert kb.claim_task(conn, task_id, claimer="test-worker") is not None
        assert calls[0][0] == ["git", "fetch", "origin", "main"]
        assert calls[1][0][-1] == "refs/remotes/origin/main^{commit}"
        assert calls[2][0][1:3] == ["scripts/check_workstream_admission.py", "--source-ref"]
        assert all(not path.exists() for path in binding_files)
    finally:
        conn.close()


def test_claim_drift_blocks_without_retry_and_deduplicates_owner_replan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    kb._INITIALIZED_PATHS.clear()
    repo = tmp_path / "repo"
    repo.mkdir()
    _register(repo)
    binding = _binding(repo)
    preflight = {
        "inputs": {"action": "write", "project_repo": str(repo)}, "resolved": {"action": "write"},
        "roadmap_binding": binding,
        "roadmap_admission": _admission(repo),
    }
    conn = kb.connect(db_path=home / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="drift", assignee="default")
        conn.execute("UPDATE tasks SET project_id=?, execution_preflight=? WHERE id=?",
                     ("project-1", canonical_execution_preflight(preflight), task_id))
        conn.commit()
        kb.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="c",
                          chat_type="group", thread_id="t", notifier_profile="default")

        calls: list[list[str]] = []

        def run(argv, **kwargs):
            calls.append(list(argv))
            if argv[1] == "fetch":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[1] == "rev-parse":
                return SimpleNamespace(returncode=0, stdout="b" * 40 + "\n", stderr="")
            return SimpleNamespace(returncode=9, stdout="SECRET", stderr="also secret")

        monkeypatch.setattr("hermes_cli.kanban_db.subprocess.run", run)
        assert kb.claim_task(conn, task_id, claimer="must-not-run") is None
        assert len(calls) == 3
        assert kb.claim_task(conn, task_id, claimer="must-not-run-again") is None
        assert len(calls) == 3, "blocked drift must not be retried automatically"
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "blocked" and task.current_run_id is None
        assert task.block_kind == "roadmap_binding_drift"
        assert task.max_retries == 0 and task.consecutive_failures == 0
        assert task.block_recurrences == 0
        events = kb.list_events(conn, task_id)
        drift_events = [event for event in events if event.kind == "roadmap_binding_drift"]
        assert len(drift_events) == 1
        assert "SECRET" not in json.dumps(drift_events[0].payload)
        intents = [event for event in events if event.kind == "needs_owner_replan"]
        assert len(intents) == 1
        assert intents[0].payload["reason"] == "roadmap_binding_drift"
        assert intents[0].run_id is None

        # An explicit warm resume is the only retry seam; it revalidates the
        # canonical ref and validator before allowing ready -> running.
        assert kb.unblock_task(conn, task_id)
        def accepted(argv, **kwargs):
            calls.append(list(argv))
            if argv[1] == "fetch":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[1] == "rev-parse":
                return SimpleNamespace(returncode=0, stdout="c" * 40 + "\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr("hermes_cli.kanban_db.subprocess.run", accepted)
        assert kb.claim_task(conn, task_id, claimer="after-owner-repair") is not None
    finally:
        conn.close()


def test_claim_rereads_register_but_ignores_irrelevant_metadata_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    kb._INITIALIZED_PATHS.clear()
    repo = tmp_path / "repo"
    repo.mkdir()
    _register(repo)
    binding = _binding(repo)
    preflight = {
        "inputs": {"project_repo": str(repo), "action": "write"},
        "resolved": {"action": "write"},
        "roadmap_binding": binding,
        "roadmap_admission": _admission(repo),
    }
    conn = kb.connect(db_path=home / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="changed register", assignee="default")
        conn.execute(
            "UPDATE tasks SET project_id=?, execution_preflight=? WHERE id=?",
            ("project-1", canonical_execution_preflight(preflight), task_id),
        )
        conn.commit()
        # Keep schema-v2/required but change irrelevant register metadata.  A
        # new digest alone must not force replan; the validator owns semantics.
        (repo / "docs" / "current" / "workstream-register.json").write_text(
            json.dumps({
                "schema_version": "schema-v2",
                "mutation_admission": {
                    "required": True,
                    "validator": "scripts/check_workstream_admission.py",
                    "changed": True,
                },
            }),
            encoding="utf-8",
        )
        calls: list[list[str]] = []
        def run(argv, **kwargs):
            calls.append(list(argv))
            if argv[1] == "fetch":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[1] == "rev-parse":
                return SimpleNamespace(returncode=0, stdout="d" * 40 + "\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr("hermes_cli.kanban_db.subprocess.run", run)
        assert kb.claim_task(conn, task_id, claimer="runs-validator") is not None
        assert len(calls) == 3
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
    finally:
        conn.close()


def test_legacy_task_is_admission_gated_only_for_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    kb._INITIALIZED_PATHS.clear()
    repo = tmp_path / "repo"
    repo.mkdir()
    register = repo / "docs" / "current" / "workstream-register.json"
    register.parent.mkdir(parents=True)
    conn = kb.connect(db_path=home / "kanban.db")
    try:
        mutation = kb.create_task(conn, title="legacy mutation", assignee="default")
        read_only = kb.create_task(conn, title="legacy inspect", assignee="default")
        mutation_preflight = {
            "inputs": {"project_repo": str(repo), "action": "write"},
            "resolved": {"action": "write"},
        }
        inspect_preflight = {
            "inputs": {"project_repo": str(repo), "action": "inspect"},
            "resolved": {"action": "inspect"},
        }
        conn.execute(
            "UPDATE tasks SET project_id=?, execution_preflight=? WHERE id=?",
            ("project-1", canonical_execution_preflight(mutation_preflight), mutation),
        )
        conn.execute(
            "UPDATE tasks SET project_id=?, execution_preflight=? WHERE id=?",
            ("project-1", canonical_execution_preflight(inspect_preflight), read_only),
        )
        conn.commit()
        _register(repo)
        monkeypatch.setattr(
            "hermes_cli.kanban_db.subprocess.run",
            lambda *args, **kwargs: pytest.fail("missing newly-required binding must stop before subprocess"),
        )
        assert kb.claim_task(conn, mutation, claimer="must-not-run") is None
        assert kb.get_task(conn, mutation).status == "blocked"
        assert kb.get_task(conn, mutation).block_kind == "roadmap_binding_drift"
        # Read-only work remains claimable even though the project now opts
        # into required mutation admission.
        assert kb.claim_task(conn, read_only, claimer="inspect") is not None
    finally:
        conn.close()


def test_claim_fails_closed_when_required_register_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    kb._INITIALIZED_PATHS.clear()
    repo = tmp_path / "repo"
    repo.mkdir()
    _register(repo)
    binding = _binding(repo)
    preflight = {
        "inputs": {"project_repo": str(repo), "action": "write"},
        "resolved": {"action": "write"},
        "roadmap_binding": binding,
        "roadmap_admission": _admission(repo),
    }
    conn = kb.connect(db_path=home / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="missing register", assignee="default")
        conn.execute(
            "UPDATE tasks SET project_id=?, execution_preflight=? WHERE id=?",
            ("project-1", canonical_execution_preflight(preflight), task_id),
        )
        conn.commit()
        (repo / "docs" / "current" / "workstream-register.json").unlink()
        monkeypatch.setattr(
            "hermes_cli.kanban_db.subprocess.run",
            lambda *args, **kwargs: pytest.fail("missing register must stop before subprocess"),
        )
        assert kb.claim_task(conn, task_id, claimer="must-not-run") is None
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "roadmap_binding_drift"
    finally:
        conn.close()


def test_review_claim_does_not_run_roadmap_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    kb._INITIALIZED_PATHS.clear()
    conn = kb.connect(db_path=home / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="review", assignee="reviewer")
        assert kb.request_review(conn, task_id, summary="candidate")
        monkeypatch.setattr("hermes_cli.kanban_db.subprocess.run",
                            lambda *args, **kwargs: pytest.fail("review claim must not refresh roadmap"))
        assert kb.claim_review_task(conn, task_id, claimer="reviewer") is not None
    finally:
        conn.close()


def _review_contract(candidate: dict) -> dict:
    return {
        "outcome": "Return one exact candidate verdict",
        "candidates": [candidate],
        "parent_receipt": "parent receipt",
        "frozen_criteria": ["exact identity"],
        "auth_fixture_state": "synthetic",
        "owner": "default",
        "verification": ["focused tests"],
        "qa_boundary": "candidate only",
        "will_not_do": ["deploy"],
        "stop_when": ["verdict returned"],
    }


def test_explicit_integration_ready_candidate_requires_immutable_push_clean_and_proof() -> None:
    from tools.kanban_tools import _prepare_review_contract

    candidate = {
        "label": "candidate",
        "source": "origin/feature",
        "source_base": "origin/main",
        "workspace_or_url": "/repo/.worktrees/candidate",
        "state": "integration_ready",
        "artifact_sha256": "d" * 64,
    }
    body, error = _prepare_review_contract(
        assignee="dollyqa", triage=False,
        contract=_review_contract(candidate), body=None,
    )
    assert body is None
    assert error and "immutable commit and tree" in error

    candidate.update({
        "commit": "a" * 40,
        "tree": "b" * 40,
        "pushed_remote_ref": "refs/remotes/origin/feature",
        "pushed_remote_commit": "a" * 40,
    })
    body, error = _prepare_review_contract(
        assignee="dollyqa", triage=False,
        contract=_review_contract(candidate), body=None,
    )
    assert body is None
    assert error and "clean worktree receipt" in error

    candidate["clean_worktree_receipt"] = "dirty worktree"
    body, error = _prepare_review_contract(
        assignee="dollyqa", triage=False,
        contract=_review_contract(candidate), body=None,
    )
    assert body is None
    assert error and "clean worktree receipt" in error

    candidate["clean_worktree_receipt"] = "clean @ " + "a" * 40
    body, error = _prepare_review_contract(
        assignee="dollyqa", triage=False,
        contract=_review_contract(candidate), body=None,
    )
    assert body is None
    assert error and "proof bound" in error

    candidate.update({"proof_commit": "a" * 40, "proof_tree": "c" * 40})
    body, error = _prepare_review_contract(
        assignee="dollyqa", triage=False,
        contract=_review_contract(candidate), body=None,
    )
    assert body is None
    assert error and "proof bound" in error

    candidate["proof_tree"] = "b" * 40
    body, error = _prepare_review_contract(
        assignee="dollyqa", triage=False,
        contract=_review_contract(candidate), body=None,
    )
    assert error is None
    assert body is not None
    assert "State: integration_ready" in body
    assert "Pushed remote readback: refs/remotes/origin/feature @ " + "a" * 40 in body
    assert "Clean worktree receipt: clean @ " + "a" * 40 in body
    assert "Proof identity: commit " + "a" * 40 + " / tree " + "b" * 40 in body
