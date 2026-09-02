"""Kanban <-> Projects integration: project-linked tasks get a deterministic
worktree path + branch instead of the random ``wt/<task-id>`` fallback."""

from __future__ import annotations

import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


@pytest.fixture
def kanban_conn(tmp_path):
    c = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        yield c
    finally:
        c.close()


def _make_project(name="Web App", repo="/tmp/webapp"):
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(pc, name=name, folders=[repo])
        return pdb.get_project(pc, pid)


def test_project_linked_task_gets_deterministic_worktree_and_branch(kanban_conn):
    proj = _make_project()
    tid = kb.create_task(kanban_conn, title="Add login", project_id=proj.slug)
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id == proj.id
    assert task.workspace_kind == "worktree"
    # Worktree dir anchored under the project's primary repo, keyed on task id.
    assert task.workspace_path == os.path.join(proj.primary_path, ".worktrees", tid)
    # Deterministic branch: <slug>/<task-id>-<title-slug>. NOT a random wt/...
    assert task.branch_name == f"{proj.slug}/{tid}-add-login"
    assert not task.branch_name.startswith("wt/")


def test_explicit_branch_overrides_project_default(kanban_conn):
    proj = _make_project()
    tid = kb.create_task(
        kanban_conn,
        title="x",
        project_id=proj.slug,
        workspace_kind="worktree",
        branch_name="feature/custom",
    )
    task = kb.get_task(kanban_conn, tid)
    assert task.branch_name == "feature/custom"


def test_unlinked_task_unchanged(kanban_conn):
    tid = kb.create_task(kanban_conn, title="plain")
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id is None
    assert task.workspace_kind == "scratch"
    # No branch is persisted — the worker still owns the wt/<id> fallback for
    # genuinely ad-hoc worktree tasks, but unlinked scratch tasks have none.
    assert task.branch_name is None




def test_project_linked_task_can_bind_shared_outcome(kanban_conn):
    from hermes_cli import outcomes_db as odb

    proj = _make_project(name="Outcome App", repo="/tmp/outcome-app")
    with odb.connect_closing() as oc:
        oid = odb.create_outcome(
            oc,
            project_id=proj.id,
            outcome_key="LOGIN-R1",
            name="Login",
        )

    tid = kb.create_task(
        kanban_conn,
        title="Implement login",
        project_id=proj.id,
        outcome_id=oid,
    )
    task = kb.get_task(kanban_conn, tid)
    assert task.project_id == proj.id
    assert task.outcome_id == oid


def test_task_rejects_outcome_from_another_project(kanban_conn):
    from hermes_cli import outcomes_db as odb

    a = _make_project(name="Project A", repo="/tmp/project-a")
    b = _make_project(name="Project B", repo="/tmp/project-b")
    with odb.connect_closing() as oc:
        oid = odb.create_outcome(oc, project_id=a.id, outcome_key="O1")

    with pytest.raises(ValueError, match="does not resolve inside"):
        kb.create_task(
            kanban_conn,
            title="Wrong project",
            project_id=b.id,
            outcome_id=oid,
        )


def test_outcome_mutation_scope_is_persisted_with_canonical_repo(kanban_conn, tmp_path):
    from hermes_cli import outcomes_db as odb

    repo = tmp_path / "repo"
    repo.mkdir()
    # A local repo without an origin intentionally falls back to its absolute
    # repository path; Git-backed projects use their origin identity.
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(pc, name="Scoped", folders=[str(repo)])
        proj = pdb.get_project(pc, pid)
    with odb.connect_closing() as oc:
        oid = odb.create_outcome(oc, project_id=proj.id, outcome_key="O")
    tid = kb.create_task(
        kanban_conn,
        title="Scoped mutation",
        project_id=proj.id,
        outcome_id=oid,
        mutation_scope=["src/bemanning/**", "src/bemanning/**"],
        mutation_base_ref="origin/main@abc",
    )
    task = kb.get_task(kanban_conn, tid)
    assert task.mutation_repository == str(repo.resolve())
    assert task.mutation_scope == ["src/bemanning/**"]
    assert task.mutation_base_ref == "origin/main@abc"


def test_mutation_scope_requires_outcome(kanban_conn):
    proj = _make_project(name="No Outcome", repo="/tmp/no-outcome")
    with pytest.raises(ValueError, match="requires outcome_id"):
        kb.create_task(
            kanban_conn,
            title="invalid",
            project_id=proj.id,
            mutation_scope=["src/**"],
        )
