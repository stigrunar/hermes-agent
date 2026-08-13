"""Strict resource argv and parallel fallback policy for Kanban workers."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


def _target():
    return kb._SystemdUserManagerTarget(
        1000, Path("/run/user/1000"), Path("/run/user/1000/bus")
    )


def test_scope_argv_carries_validated_resource_controls(monkeypatch):
    monkeypatch.setattr(kb.sys, "platform", "linux")
    task = SimpleNamespace(id="task", current_run_id=3)
    config = {
        "worker_scope": {
            "enabled": True,
            "slice": "ops-workers.slice",
            "memory_high": "1536M",
            "memory_max": "2G",
            "memory_swap_max": "256M",
            "tasks_max": 384,
            "oom_policy": "stop",
        }
    }

    argv, unit, target = kb._systemd_scope_argv(
        ["/bin/sleep", "10"],
        task,
        cgroup_path="/user.slice/user-1000.slice/user@1000.service/app.slice/hermes.service",
        manager_target=_target(),
        systemd_run="/usr/bin/systemd-run",
        user_manager_ready=True,
        kanban_cfg=config,
    )

    assert unit is not None and target is not None
    delimiter = argv.index("--")
    prefix = argv[:delimiter]
    assert "--slice=ops-workers.slice" in prefix
    assert "--property=MemoryHigh=1536M" in prefix
    assert "--property=MemoryMax=2G" in prefix
    assert "--property=MemorySwapMax=256M" in prefix
    assert "--property=TasksMax=384" in prefix
    assert "--property=OOMPolicy=stop" in prefix
    # systemd 255 rejects MemoryOOMGroup as an unknown assignment. OOMPolicy
    # plus exact scope stop/reaping is the supported unit-kill contract.
    assert not any(arg.startswith("--property=MemoryOOMGroup=") for arg in prefix)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("slice", "bad.service"),
        ("memory_high", "2G --property=Delegate=yes"),
        ("memory_max", "infinity"),
        ("memory_swap_max", "-1"),
        ("tasks_max", True),
        ("tasks_max", 0),
        ("oom_policy", "continue"),
        ("memory_high", "4G"),
    ],
)
def test_malformed_scope_resources_are_rejected(key, value):
    config = {key: value}
    if key == "memory_high" and value == "4G":
        config["memory_max"] = "3G"
    with pytest.raises(ValueError):
        kb._worker_scope_config({"worker_scope": config})


def test_parallel_dispatch_fails_closed_without_scope(monkeypatch):
    monkeypatch.setattr(kb.sys, "platform", "darwin")
    task = SimpleNamespace(id="task", current_run_id=3)

    with pytest.raises(RuntimeError, match="Kanban dispatch requires"):
        kb._systemd_scope_argv(
            ["/bin/sleep", "10"], task, require_scope=True,
        )

    direct, unit, target = kb._systemd_scope_argv(
        ["/bin/sleep", "10"], task, require_scope=False,
    )
    assert direct == ["/bin/sleep", "10"]
    assert unit is None and target is None


def test_host_required_scope_fails_closed_for_first_worker(monkeypatch):
    """A multi-board wrapper can require scopes before local overlap exists."""
    monkeypatch.setattr(kb.sys, "platform", "darwin")
    task = SimpleNamespace(id="task", current_run_id=3)

    with pytest.raises(RuntimeError, match="Kanban dispatch requires"):
        kb._systemd_scope_argv(
            ["/bin/sleep", "10"],
            task,
            require_scope=False,
            kanban_cfg={"worker_scope": {"required": True}},
        )


def test_required_scope_must_be_boolean():
    with pytest.raises(ValueError, match="required must be a boolean"):
        kb._worker_scope_config({"worker_scope": {"required": "yes"}})
