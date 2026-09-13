"""Production dispatch tests for the fixed external update adapter."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from gateway.private_update_request import resolve_private_update_paths
from hermes_cli import main as cli_main
from hermes_cli import private_update_adapter as adapter
from hermes_cli import update_receipt


def _write(path, data: bytes, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return hashlib.sha256(data).hexdigest()


def test_native_mode_reaches_existing_preflight_without_external_work(monkeypatch):
    calls = []
    monkeypatch.setattr(adapter, "dispatch_private_immutable_update", lambda _args: False)
    monkeypatch.setattr(cli_main, "_update_preflight_handled", lambda _args: calls.append("native") or True)
    cli_main.cmd_update(SimpleNamespace())
    assert calls == ["native"]


def test_external_mode_dispatches_before_native_preflight(monkeypatch):
    monkeypatch.setattr(adapter, "dispatch_private_immutable_update", lambda _args: True)
    monkeypatch.setattr(
        cli_main,
        "_update_preflight_handled",
        lambda _args: pytest.fail("native mutable preparation was reached"),
    )
    cli_main.cmd_update(SimpleNamespace())


@pytest.mark.parametrize("option", ["plan", "check"])
def test_external_mode_read_only_options_never_reach_activation_adapter(monkeypatch, option):
    calls = []
    monkeypatch.setattr(
        adapter,
        "dispatch_private_immutable_update",
        lambda _args: pytest.fail("read-only option reached activation adapter"),
    )
    monkeypatch.setattr(
        cli_main,
        "_update_preflight_handled",
        lambda args: calls.append((option, getattr(args, option))) or False,
    )
    cli_main.cmd_update(SimpleNamespace(**{option: True}))
    assert calls == [(option, True)]


def test_check_refusal_does_not_write_a_refusal_receipt(monkeypatch):
    refusal = SimpleNamespace(message="managed elsewhere")
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr(
        "hermes_cli.update_contract.evaluate_update_admission", lambda _root: refusal
    )
    monkeypatch.setattr(
        "hermes_cli.update_contract.record_refusal_receipt",
        lambda _refusal: pytest.fail("--check wrote a refusal receipt"),
    )
    with pytest.raises(SystemExit) as error:
        cli_main._update_preflight_handled(SimpleNamespace(check=True, plan=False))
    assert error.value.code == 2


def test_adapter_pins_helper_descriptor_and_passes_exact_candidate(tmp_path, monkeypatch):
    paths = resolve_private_update_paths(
        {"updates": {"mode": "private_immutable_external"}}, default_root=tmp_path
    )
    helper_digest = _write(paths.helper_path, b"print('helper')\n", 0o700)
    policy_digest = _write(paths.policy_path, b"{}")
    _write(paths.request_path, b"{}")
    manifest = json.dumps({
        "schema": adapter.INSTALLATION_SCHEMA,
        "version": adapter.INSTALLATION_VERSION,
        "helper_sha256": helper_digest,
        "policy_sha256": policy_digest,
    }).encode()
    _write(paths.manifest_path, manifest)
    captured = {}

    monkeypatch.setattr(adapter.shutil, "which", lambda name: "/usr/bin/systemd-run")

    def fake_run(command, **kwargs):
        captured["command"] = command
        assert kwargs["input"] == b"print('helper')\n"
        assert len(kwargs["pass_fds"]) == 2
        systemd_fd, python_fd = kwargs["pass_fds"]
        assert kwargs["executable"] == f"/proc/self/fd/{systemd_fd}"
        assert hashlib.sha256(os.pread(systemd_fd, 64 * 1024 * 1024, 0)).hexdigest()
        assert hashlib.sha256(os.pread(python_fd, 64 * 1024 * 1024, 0)).hexdigest()
        helper_path = command[command.index("--") + 4]
        assert open(helper_path, "rb").read() == b"print('helper')\n"
        return subprocess.CompletedProcess(command, 0, stdout=b"terminal receipt\n")

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)
    args = SimpleNamespace(
        candidate_commit="a" * 40,
        candidate_tree="b" * 40,
        release_request_id="request-1",
    )
    assert adapter.run_private_update_adapter(args, paths=paths) == 0
    command = captured["command"]
    assert command[:6] == [
        "/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect", "--",
    ]
    assert command[6].startswith(f"/proc/{os.getpid()}/fd/")
    assert command[7:10] == ["-c", adapter._HELPER_STDIN_BOOTSTRAP, str(paths.helper_path)]
    assert command[command.index("--expected-commit") + 1] == "a" * 40
    assert command[command.index("--expected-tree") + 1] == "b" * 40
    assert command[command.index("--expected-request-id") + 1] == "request-1"
    assert "--expected-request-sha256" in command


def test_helper_stdin_bootstrap_promotes_helper_path_to_argv_zero(tmp_path):
    helper_path = tmp_path / "private_release_supervisor.py"
    source = b"import json, sys; print(json.dumps(sys.argv))\n"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            adapter._HELPER_STDIN_BOOTSTRAP,
            str(helper_path),
            "--expected-helper-sha256",
            "a" * 64,
        ],
        input=source,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode()
    assert json.loads(completed.stdout) == [
        str(helper_path),
        "--expected-helper-sha256",
        "a" * 64,
    ]


def test_external_adapter_fails_closed_without_host_supervisor(tmp_path, monkeypatch):
    paths = resolve_private_update_paths(
        {"updates": {"mode": "private_immutable_external"}}, default_root=tmp_path
    )
    helper_digest = _write(paths.helper_path, b"pass\n", 0o700)
    policy_digest = _write(paths.policy_path, b"{}")
    _write(paths.request_path, b"{}")
    _write(paths.manifest_path, json.dumps({
        "schema": adapter.INSTALLATION_SCHEMA, "version": adapter.INSTALLATION_VERSION,
        "helper_sha256": helper_digest, "policy_sha256": policy_digest,
    }).encode())
    monkeypatch.setattr(adapter.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        adapter.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("helper ran directly")
    )
    with pytest.raises(adapter.PrivateUpdateAdapterError, match="systemd-run boundary"):
        adapter.run_private_update_adapter(SimpleNamespace(), paths=paths)


def test_validation_failure_finalizes_actionable_update_receipt(tmp_path, monkeypatch):
    receipt_dir = tmp_path / "update-receipts"
    monkeypatch.setattr(update_receipt, "_receipt_dir", lambda: receipt_dir)
    monkeypatch.setattr(adapter, "private_immutable_external_enabled", lambda: True)
    monkeypatch.setattr(
        adapter,
        "run_private_update_adapter",
        lambda _args: (_ for _ in ()).throw(adapter.PrivateUpdateAdapterError("policy digest mismatch")),
    )
    with pytest.raises(SystemExit) as error:
        adapter.dispatch_private_immutable_update(SimpleNamespace())
    assert error.value.code == 1
    receipt = json.loads((receipt_dir / "latest.json").read_text())
    assert receipt["outcome"] == "failed"
    assert receipt["steps"][-1] == {
        "name": "private_immutable_external",
        "ok": False,
        "detail": "policy digest mismatch",
        "at": receipt["steps"][-1]["at"],
    }
    assert receipt["stop_reason"] == "policy digest mismatch"


@pytest.mark.parametrize("outcome", ["success", "exit", "error"])
def test_private_dispatch_holds_shared_lock_and_releases_on_every_exit(tmp_path, monkeypatch, outcome):
    from hermes_cli import update_lock

    marker = tmp_path / "shared-update-marker"
    monkeypatch.setattr(update_lock, "update_marker_path", lambda: marker)
    monkeypatch.delenv(update_lock.HANDOFF_PID_ENV, raising=False)
    monkeypatch.setattr(cli_main, "_install_hangup_protection", lambda **_kwargs: None)
    monkeypatch.setattr(cli_main, "_finalize_update_output", lambda _state: None)
    calls = []

    def dispatch(_args):
        calls.append("dispatch")
        contender = update_lock.UpdateLock()
        assert not contender.acquire()
        assert contender.holder.pid == os.getpid()
        if outcome == "exit":
            raise SystemExit(7)
        if outcome == "error":
            raise RuntimeError("adapter failed")
        return True

    monkeypatch.setattr(adapter, "dispatch_private_immutable_update", dispatch)
    holder = update_lock.UpdateLock()
    assert holder.acquire()
    with pytest.raises(SystemExit) as refused:
        cli_main.cmd_update(SimpleNamespace())
    assert refused.value.code == update_lock.UPDATE_EXIT_CONCURRENT
    assert calls == []
    assert marker.exists()
    holder.release()
    if outcome == "success":
        cli_main.cmd_update(SimpleNamespace())
    elif outcome == "exit":
        with pytest.raises(SystemExit) as exited:
            cli_main.cmd_update(SimpleNamespace())
        assert exited.value.code == 7
    else:
        with pytest.raises(RuntimeError, match="adapter failed"):
            cli_main.cmd_update(SimpleNamespace())
    assert calls == ["dispatch"]
    assert not marker.exists()
    with update_lock.UpdateLock() as next_update:
        assert next_update.acquired
