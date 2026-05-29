import importlib
from pathlib import Path


MODULE = "plugins.memory.byterover"


def _reload(monkeypatch):
    mod = importlib.import_module(MODULE)
    setattr(mod, "_cached_brv_path", None)
    return importlib.reload(mod)


def test_resolve_brv_path_prefers_env_over_path(monkeypatch, tmp_path):
    env_brv = tmp_path / "env" / "brv"
    env_brv.parent.mkdir()
    env_brv.write_text("#!/bin/sh\n")
    env_brv.chmod(0o755)

    path_brv = tmp_path / "path" / "brv"
    path_brv.parent.mkdir()
    path_brv.write_text("#!/bin/sh\n")
    path_brv.chmod(0o755)

    monkeypatch.setenv("BRV_BIN", str(env_brv))
    monkeypatch.setenv("PATH", str(path_brv.parent))
    mod = _reload(monkeypatch)

    assert mod._resolve_brv_path() == str(env_brv)


def test_get_brv_cwd_uses_project_root_env(monkeypatch, tmp_path):
    project = tmp_path / "project"
    monkeypatch.setenv("BRV_PROJECT_ROOT", str(project))
    mod = _reload(monkeypatch)

    assert mod._get_brv_cwd() == project


def test_get_brv_cwd_falls_back_to_profile_home(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    monkeypatch.delenv("BRV_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    mod = _reload(monkeypatch)

    assert mod._get_brv_cwd() == hermes_home / "byterover"
