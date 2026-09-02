from __future__ import annotations

import subprocess

from hermes_cli import outcomes_db as odb
from hermes_cli.outcome_packet import materialize_status


def test_materialized_status_is_projection_not_second_database(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

    with odb.connect_closing() as conn:
        oid = odb.create_outcome(
            conn,
            project_id="p_ps",
            outcome_key="STAFFING-TEST-ENABLER-R1",
            name="Bemanning",
            state="implementing",
            visible_owner="default",
            current_base_ref="origin/main@abc",
            frozen_acceptance=["source-backed", "read-only"],
            next_action="Implement real seam",
        )
        odb.bind_conversation_lane(
            conn,
            project_id="p_ps",
            outcome_id=oid,
            platform="telegram",
            chat_id="-1001",
            thread_id="42",
            label="Bemanning",
        )
        odb.acquire_mutation_lease(
            conn,
            project_id="p_ps",
            outcome_id=oid,
            repository="repo",
            path_scope=["apps/bemanning/**"],
            owner_execution_id="codex:staffing",
        )

    target = materialize_status(
        project_id="p_ps",
        project_name="Prosjektstyring",
        outcome_id=oid,
        repo=repo,
    )
    assert target == repo / "docs/outcomes/STAFFING-TEST-ENABLER-R1/00-status.md"
    text = target.read_text()
    assert "Generated current-state projection" in text
    assert "codex:staffing" in text
    assert "telegram:-1001:42" in text
    assert "source-backed" in text
    assert "Implement real seam" in text
    assert (target.parent / "receipts").is_dir()
    assert not (target.parent / "01-outcome.md").exists()
