import json
import os
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from plugins.memory.byterover import (
    ByteRoverMemoryProvider,
    _curate_detail_has_write,
    _resolve_brv_path,
)
import plugins.memory.byterover as byterover


@pytest.fixture(autouse=True)
def clear_brv_cache():
    with byterover._brv_path_lock:
        byterover._cached_brv_path = None
    yield
    with byterover._brv_path_lock:
        byterover._cached_brv_path = None


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho fake brv\n")
    path.chmod(0o755)


def test_resolve_prefers_profile_shim_over_path(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    shim = hermes_home / "home" / ".local" / "bin" / "brv"
    old = tmp_path / "usr" / "bin" / "brv"
    _make_executable(shim)
    _make_executable(old)

    token = set_hermes_home_override(hermes_home)
    monkeypatch.setenv("PATH", str(old.parent))
    try:
        assert _resolve_brv_path() == str(shim)
    finally:
        reset_hermes_home_override(token)


def test_curate_detail_requires_completed_operation():
    assert _curate_detail_has_write(
        "ID: cur-1\nStatus: completed\n\nOperations:\n  ✓ [UPSERT] facts/x.md — Upserted\n\nSummary: 1 updated"
    )
    assert not _curate_detail_has_write(
        "ID: cur-2\nStatus: completed\n\nOperations:\n\nSummary: —"
    )
    assert not _curate_detail_has_write(
        "ID: cur-3\nStatus: error\n\nOperations:\n  ✓ [UPSERT] facts/x.md\n"
    )


def test_tool_curate_reports_unverified_without_log_id(monkeypatch, tmp_path):
    provider = ByteRoverMemoryProvider()
    provider._cwd = str(tmp_path)

    def fake_run(args, timeout=0, cwd=None):
        if args[:2] == ["curate", "--"]:
            return {"success": True, "output": "Done", "stderr": ""}
        raise AssertionError(args)

    monkeypatch.setattr(byterover, "_run_brv", fake_run)
    payload = json.loads(provider._tool_curate({"content": "important fact"}))
    assert payload["verified"] is False
    assert "not verified" in payload["result"]


def test_tool_curate_reports_verified_when_detail_has_operation(monkeypatch, tmp_path):
    provider = ByteRoverMemoryProvider()
    provider._cwd = str(tmp_path)

    def fake_run(args, timeout=0, cwd=None):
        if args[:2] == ["curate", "--"]:
            return {"success": True, "output": "Completed cur-12345", "stderr": ""}
        if args == ["curate", "view", "cur-12345"]:
            return {
                "success": True,
                "output": "ID: cur-12345\nStatus: completed\n\nOperations:\n  ✓ [UPSERT] facts/x.md — Upserted\n\nSummary: 1 updated",
                "stderr": "",
            }
        raise AssertionError(args)

    monkeypatch.setattr(byterover, "_run_brv", fake_run)
    payload = json.loads(provider._tool_curate({"content": "important fact"}))
    assert payload["verified"] is True
    assert payload["log_id"] == "cur-12345"
