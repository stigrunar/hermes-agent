"""Regression tests for hermes secrets bitwarden setup non-TTY guard.

Issue #40274: cmd_setup() crashes with EOFError when stdin is not a TTY
because getpass.getpass() and console.input() require an interactive terminal.
"""
from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest


class TestCmdSetupNonTtyGuard:
    """cmd_setup should fail early with a clear error in non-TTY environments."""

    @staticmethod
    def _make_args(**overrides):
        ns = argparse.Namespace(
            server_url=overrides.get("server_url", ""),
            project_id=overrides.get("project_id", ""),
        )
        return ns

    def test_missing_all_flags_returns_1(self, monkeypatch, capsys):
        """Non-TTY with no flags → exit 1 with missing flags listed."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.find_bws", lambda install_if_missing=False: "/usr/bin/bws"
        )
        monkeypatch.setattr(
            "hermes_cli.secrets_cli._bws_version", lambda _: "2.0.0"
        )

        from hermes_cli.secrets_cli import cmd_setup

        result = cmd_setup(self._make_args())
        assert result == 1
        captured = capsys.readouterr()
        assert "Non-interactive mode" in captured.out
        assert "BWS_ACCESS_TOKEN" in captured.out
        assert "--server-url" in captured.out
        assert "--project-id" in captured.out

    def test_missing_access_token_only(self, monkeypatch, capsys):
        """Non-TTY with server-url and project-id but no token names the env var."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.find_bws", lambda install_if_missing=False: "/usr/bin/bws"
        )
        monkeypatch.setattr(
            "hermes_cli.secrets_cli._bws_version", lambda _: "2.0.0"
        )

        from hermes_cli.secrets_cli import cmd_setup

        result = cmd_setup(self._make_args(
            server_url="https://vault.bitwarden.com",
            project_id="aaaa-bbbb",
        ))
        assert result == 1
        captured = capsys.readouterr()
        # The "Missing:" line should list the token environment variable only.
        assert "Missing:" in captured.out
        assert "BWS_ACCESS_TOKEN" in captured.out
        # The usage example contains --server-url and --project-id, so check
        # the missing line specifically: it should NOT list them as missing
        missing_line = [l for l in captured.out.split("\n") if "Missing:" in l][0]
        assert "BWS_ACCESS_TOKEN" in missing_line
        assert "--server-url" not in missing_line
        assert "--project-id" not in missing_line

    def test_missing_server_url_with_env_var_passes(self, monkeypatch):
        """Non-TTY with BWS_SERVER_URL env set → server-url not required."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setenv("BWS_SERVER_URL", "https://vault.bitwarden.com")
        monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.synthetic-token")
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.find_bws", lambda install_if_missing=False: "/usr/bin/bws"
        )
        monkeypatch.setattr(
            "hermes_cli.secrets_cli._bws_version", lambda _: "2.0.0"
        )
        monkeypatch.setattr("hermes_cli.secrets_cli.load_config", lambda: {})
        monkeypatch.setattr("hermes_cli.secrets_cli.save_env_value", lambda *a: None)
        monkeypatch.setattr("hermes_cli.secrets_cli.get_env_path", lambda: "/tmp/.env")
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.fetch_bitwarden_secrets",
            lambda **kw: ({"KEY": "val"}, []),
        )

        from hermes_cli.secrets_cli import cmd_setup

        result = cmd_setup(self._make_args(
            project_id="aaaa-bbbb",
        ))
        assert result == 0

    def test_non_tty_token_env_and_flags_pass_guard(self, monkeypatch):
        """Non-TTY setup takes its bearer token from env, never argv."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.synthetic-token")
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.find_bws", lambda install_if_missing=False: "/usr/bin/bws"
        )
        monkeypatch.setattr(
            "hermes_cli.secrets_cli._bws_version", lambda _: "2.0.0"
        )
        monkeypatch.setattr("hermes_cli.secrets_cli.load_config", lambda: {})
        monkeypatch.setattr("hermes_cli.secrets_cli.save_env_value", lambda *a: None)
        monkeypatch.setattr("hermes_cli.secrets_cli.get_env_path", lambda: "/tmp/.env")
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.fetch_bitwarden_secrets",
            lambda **kw: ({"KEY": "val"}, []),
        )

        from hermes_cli.secrets_cli import cmd_setup

        result = cmd_setup(self._make_args(
            server_url="https://vault.bitwarden.com",
            project_id="aaaa-bbbb",
        ))
        assert result == 0

    def test_non_tty_token_can_come_from_profile_dotenv(self, monkeypatch):
        """The documented profile .env path is consulted before rejecting CI."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.get_env_value_prefer_dotenv",
            lambda key: "0.synthetic-dotenv-token" if key == "BWS_ACCESS_TOKEN" else None,
        )
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.find_bws", lambda install_if_missing=False: "/usr/bin/bws"
        )
        monkeypatch.setattr("hermes_cli.secrets_cli._bws_version", lambda _: "2.0.0")
        monkeypatch.setattr("hermes_cli.secrets_cli.load_config", lambda: {})
        monkeypatch.setattr("hermes_cli.secrets_cli.save_env_value", lambda *a: None)
        monkeypatch.setattr("hermes_cli.secrets_cli.get_env_path", lambda: "/tmp/.env")
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.fetch_bitwarden_secrets",
            lambda **kw: ({"KEY": "val"}, []),
        )

        from hermes_cli.secrets_cli import cmd_setup

        assert cmd_setup(self._make_args(
            server_url="https://vault.bitwarden.com",
            project_id="aaaa-bbbb",
        )) == 0

    def test_tty_does_not_trigger_guard(self, monkeypatch):
        """With TTY, the guard should not trigger (interactive mode allowed)."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.find_bws", lambda install_if_missing=False: "/usr/bin/bws"
        )
        monkeypatch.setattr(
            "hermes_cli.secrets_cli._bws_version", lambda _: "2.0.0"
        )
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.masked_secret_prompt", lambda prompt: "0.valid-token"
        )
        monkeypatch.setattr("hermes_cli.secrets_cli.load_config", lambda: {})
        monkeypatch.setattr("hermes_cli.secrets_cli.save_env_value", lambda *a: None)
        monkeypatch.setattr("hermes_cli.secrets_cli.get_env_path", lambda: "/tmp/.env")
        monkeypatch.setattr(
            "hermes_cli.secrets_cli._resolve_server_url",
            lambda *a: "https://vault.bitwarden.com",
        )
        # Provide project_id directly to avoid interactive project prompt
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.fetch_bitwarden_secrets",
            lambda **kw: ({"KEY": "val"}, []),
        )

        from hermes_cli.secrets_cli import cmd_setup

        # With TTY + all flags → should complete without hitting guard
        result = cmd_setup(self._make_args(
            server_url="https://vault.bitwarden.com",
            project_id="aaaa-bbbb",
        ))
        assert result == 0

    def test_setup_parser_rejects_access_token_argv(self):
        from hermes_cli.secrets_cli import register_cli

        parser = argparse.ArgumentParser()
        register_cli(parser)

        with pytest.raises(SystemExit):
            parser.parse_args(["setup", "--access-token", "synthetic-token"])

    def test_project_helper_uses_minimal_env_and_hides_raw_stderr(
        self, monkeypatch, capsys, tmp_path
    ):
        from hermes_cli.secrets_cli import _list_projects

        marker = "sk-synthetichelperstderr123456789"
        monkeypatch.setenv("UNRELATED_SYNTHETIC_SECRET", marker)

        def fake_run(_argv, **kwargs):
            assert "UNRELATED_SYNTHETIC_SECRET" not in kwargs["env"]
            return __import__("subprocess").CompletedProcess(
                [], 1, stdout="", stderr=f"bad token {marker}"
            )

        monkeypatch.setattr("agent.secret_sources.base.subprocess.run", fake_run)
        from rich.console import Console

        result = _list_projects(
            tmp_path / "bws", "0.synthetic-token", Console(),
            server_url="https://vault.bitwarden.com",
        )

        assert result is None
        assert marker not in capsys.readouterr().out
