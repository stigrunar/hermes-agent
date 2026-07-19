"""Terminal SSH and sudo credentials remain owned by the active profile."""

from __future__ import annotations

import pytest

from agent import secret_scope as ss


@pytest.fixture(autouse=True)
def _reset_multiplex():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


def test_ssh_and_sudo_ignore_hostile_ambient_values(monkeypatch):
    from tools import terminal_tool
    from tools.approval import _check_sudo_stdin_guard

    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "hostile.invalid")
    monkeypatch.setenv("TERMINAL_SSH_USER", "hostile-user")
    monkeypatch.setenv("TERMINAL_SSH_PORT", "9922")
    monkeypatch.setenv("TERMINAL_SSH_KEY", "/synthetic/hostile-key")
    monkeypatch.setenv("SUDO_PASSWORD", "synthetic-hostile-sudo")
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(
        {
            "TERMINAL_SSH_HOST": "profile.invalid",
            "TERMINAL_SSH_USER": "profile-user",
            "TERMINAL_SSH_PORT": "2222",
            "TERMINAL_SSH_KEY": "/synthetic/profile-key",
            "SUDO_PASSWORD": "synthetic-profile-sudo",
        }
    )
    try:
        config = terminal_tool._get_env_config()
        transformed, sudo_stdin = terminal_tool._transform_sudo_command(
            "sudo id"
        )
        sudo_guard = _check_sudo_stdin_guard("printf x | sudo -S id")
    finally:
        ss.reset_secret_scope(token)

    assert config["env_type"] == "ssh"  # exact deployment-global mechanic
    assert config["ssh_host"] == "profile.invalid"
    assert config["ssh_user"] == "profile-user"
    assert config["ssh_port"] == 2222
    assert config["ssh_key"] == "/synthetic/profile-key"
    assert transformed == "sudo -S -p '' id"
    assert sudo_stdin == "synthetic-profile-sudo\n"
    assert sudo_guard == (False, None)


def test_empty_profile_does_not_consume_ambient_terminal_credentials(monkeypatch):
    from tools import terminal_tool
    from tools.approval import _check_sudo_stdin_guard

    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "hostile.invalid")
    monkeypatch.setenv("TERMINAL_SSH_USER", "hostile-user")
    monkeypatch.setenv("TERMINAL_SSH_KEY", "/synthetic/hostile-key")
    monkeypatch.setenv("SUDO_PASSWORD", "synthetic-hostile-sudo")
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    monkeypatch.setattr(terminal_tool, "_sudo_nopasswd_works", lambda: False)

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({})
    try:
        config = terminal_tool._get_env_config()
        command, sudo_stdin = terminal_tool._transform_sudo_command("sudo id")
        sudo_guard = _check_sudo_stdin_guard("printf x | sudo -S id")
    finally:
        ss.reset_secret_scope(token)

    assert config["ssh_host"] == ""
    assert config["ssh_user"] == ""
    assert config["ssh_key"] == ""
    assert command == "sudo id"
    assert sudo_stdin is None
    assert sudo_guard[0] is True


def test_terminal_config_members_are_projected_into_each_profile_scope(tmp_path):
    profiles = []
    for suffix in ("a", "b"):
        home = tmp_path / suffix
        home.mkdir()
        (home / "config.yaml").write_text(
            "terminal:\n"
            f"  ssh_host: profile-{suffix}.invalid\n"
            f"  ssh_user: profile-{suffix}-user\n"
            f"  ssh_port: '{2200 + len(profiles)}'\n"
            f"  ssh_key: /synthetic/profile-{suffix}-key\n"
            f"  sudo_password: synthetic-profile-{suffix}-sudo\n",
            encoding="utf-8",
        )
        profiles.append(ss.build_profile_secret_scope(home))

    assert profiles[0]["TERMINAL_SSH_HOST"] == "profile-a.invalid"
    assert profiles[0]["SUDO_PASSWORD"] == "synthetic-profile-a-sudo"
    assert profiles[1]["TERMINAL_SSH_HOST"] == "profile-b.invalid"
    assert profiles[1]["SUDO_PASSWORD"] == "synthetic-profile-b-sudo"


def test_terminal_credentials_are_not_deployment_globals():
    for name in (
        "TERMINAL_SSH_HOST",
        "TERMINAL_SSH_USER",
        "TERMINAL_SSH_PORT",
        "TERMINAL_SSH_KEY",
        "SUDO_PASSWORD",
    ):
        with pytest.raises(ValueError, match="profile-scoped"):
            ss.get_deployment_env(name)
