"""Managed callback retains owner capabilities without granting them to children."""
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("context", ["owner", "delegate", "cron", "descendant"])
def test_callback_ownership_survives_real_process(context, tmp_path, monkeypatch):
    from agent.codex_runtime import _codex_runtime_contract
    from agent.delegation_context import delegated_child_context, non_dispatcher_owned_context
    from agent.transports.codex_app_server import hermes_subprocess_env

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "test-owner")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1" if context == "descendant" else "")
    (tmp_path / "config.yaml").write_text("toolsets: []\n")
    scope = {"owner": nullcontext, "delegate": delegated_child_context,
             "cron": non_dispatcher_owned_context, "descendant": nullcontext}[context]
    with scope():
        contract, _ = _codex_runtime_contract(
            SimpleNamespace(model="gpt-5.6-sol", reasoning_config={"effort": "high"}), [], str(tmp_path))
        native_env = hermes_subprocess_env(inherit_credentials=False)
    callback = contract["config_overrides"]["mcp_servers.hermes-tools"]
    probe = '''import json, os
from agent.delegation_context import is_dispatcher_owned_worker_context
from tools import kanban_tools
print(json.dumps({"owner": bool(os.getenv("HERMES_KANBAN_TASK")) and is_dispatcher_owned_worker_context(), "visible": kanban_tools._check_kanban_mode(), "run": os.getenv("HERMES_KANBAN_RUN_ID")}))
'''
    for env, expected in ((dict(os.environ, **callback["env"]), context == "owner"), (native_env, False)):
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        result = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True)
        proof = json.loads(result.stdout.strip().splitlines()[-1])
        assert proof["owner"] is expected
        assert proof["visible"] is expected
        if expected:
            assert proof["run"] == "7"
