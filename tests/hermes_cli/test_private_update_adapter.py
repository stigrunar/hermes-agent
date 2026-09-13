"""Production dispatch tests for the fixed external update adapter."""

from __future__ import annotations

import hashlib
import json
import subprocess
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

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["pass_fds"] = kwargs["pass_fds"]
        assert open(command[1], "rb").read() == b"print('helper')\n"
        return subprocess.CompletedProcess(command, 0, stdout="terminal receipt\n")

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)
    args = SimpleNamespace(
        candidate_commit="a" * 40,
        candidate_tree="b" * 40,
        release_request_id="request-1",
    )
    assert adapter.run_private_update_adapter(args, paths=paths) == 0
    command = captured["command"]
    assert command[command.index("--expected-commit") + 1] == "a" * 40
    assert command[command.index("--expected-tree") + 1] == "b" * 40
    assert command[command.index("--expected-request-id") + 1] == "request-1"
    assert "--expected-request-sha256" in command
    assert len(captured["pass_fds"]) == 1


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
