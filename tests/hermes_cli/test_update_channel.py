from types import SimpleNamespace

import pytest

from hermes_cli.update_channel import (
    UpdateChannelError,
    UpdateTarget,
    resolve_update_branch,
    resolve_update_target,
)


def _repo(tmp_path, *, stig=False):
    root = tmp_path / "repo"
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    remotes = '[remote "origin"]\n\turl = https://github.com/NousResearch/hermes-agent.git\n'
    if stig:
        remotes += '[remote "stig"]\n\turl = git@github.com:stigrunar/hermes-agent.git\n'
    (git_dir / "config").write_text(remotes, encoding="utf-8")
    return root


def test_explicit_branch_has_highest_precedence(tmp_path):
    root = _repo(tmp_path, stig=True)
    assert resolve_update_branch(
        "topic/test", project_root=root,
        config={"updates": {"release_channel": "release/stig-tested"}},
    ) == "topic/test"


def test_target_resolver_selects_stig_remote_for_implicit_tested_channel(tmp_path):
    root = _repo(tmp_path, stig=True)
    assert resolve_update_target(
        project_root=root,
        config={"updates": {"release_channel": "release/stig-tested"}},
    ) == UpdateTarget("stig", "release/stig-tested")


def test_explicit_tested_branch_selects_stig_remote(tmp_path):
    root = _repo(tmp_path, stig=True)
    assert resolve_update_target(
        "release/stig-tested", project_root=root, config={"updates": {}}
    ) == UpdateTarget("stig", "release/stig-tested")


def test_explicit_other_branch_on_stig_checkout_uses_origin(tmp_path):
    root = _repo(tmp_path, stig=True)
    assert resolve_update_target(
        "topic/test", project_root=root,
        config={"updates": {"release_channel": "release/stig-tested"}},
    ) == UpdateTarget("origin", "topic/test")


def test_configured_release_channel_pins_stig_checkout(tmp_path):
    root = _repo(tmp_path, stig=True)
    assert resolve_update_branch(
        project_root=root,
        config={"updates": {"release_channel": " release/stig-tested "}},
    ) == "release/stig-tested"


def test_stig_checkout_without_channel_fails_closed(tmp_path):
    root = _repo(tmp_path, stig=True)
    with pytest.raises(UpdateChannelError, match="release/stig-tested"):
        resolve_update_branch(project_root=root, config={"updates": {}})


def test_stig_checkout_with_other_configured_branch_fails_closed(tmp_path):
    root = _repo(tmp_path, stig=True)
    with pytest.raises(UpdateChannelError, match="expected release/stig-tested"):
        resolve_update_branch(
            project_root=root,
            config={"updates": {"release_channel": "main"}},
        )


def test_ordinary_upstream_checkout_keeps_main_default(tmp_path):
    assert resolve_update_target(
        project_root=_repo(tmp_path), config={"updates": {}}
    ) == UpdateTarget("origin", "main")


def test_ordinary_configured_branch_uses_origin(tmp_path):
    assert resolve_update_target(
        project_root=_repo(tmp_path),
        config={"updates": {"release_channel": "release/stig-tested"}},
    ) == UpdateTarget("origin", "release/stig-tested")


@pytest.mark.parametrize("value", ["", "   ", 123, {}, []])
def test_invalid_configured_channel_fails_closed(tmp_path, value):
    with pytest.raises(UpdateChannelError, match="non-empty branch"):
        resolve_update_branch(
            project_root=_repo(tmp_path),
            config={"updates": {"release_channel": value}},
        )


def test_cli_adapter_uses_same_configured_policy(tmp_path, monkeypatch):
    from hermes_cli import main

    root = _repo(tmp_path, stig=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "updates:\n  release_channel: release/stig-tested\n", encoding="utf-8"
    )
    monkeypatch.setattr(main, "PROJECT_ROOT", root)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert main._resolve_update_branch(SimpleNamespace(branch=None)) == "release/stig-tested"


@pytest.mark.parametrize("value", ["bad branch", "bad..branch", "bad@{ref}", "-bad"])
def test_malformed_branch_is_rejected_before_target_selection(tmp_path, value):
    with pytest.raises(UpdateChannelError, match="valid branch"):
        resolve_update_target(
            project_root=_repo(tmp_path),
            config={"updates": {"release_channel": value}},
        )
