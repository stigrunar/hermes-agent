from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.computer_use import cua_backend


def test_resolves_installer_symlink_into_cua_driver_app(tmp_path: Path) -> None:
    app = tmp_path / "CuaDriver.app"
    executable = app / "Contents" / "MacOS" / "cua-driver"
    executable.parent.mkdir(parents=True)
    executable.write_text("driver")
    executable.chmod(0o755)
    link = tmp_path / "bin" / "cua-driver"
    link.parent.mkdir()
    link.symlink_to(executable)

    assert cua_backend._resolve_cua_driver_app_path(str(link)) == str(app)


@pytest.mark.parametrize("team", sorted(cua_backend._CUA_DRIVER_TEAM_IDS))
def test_accepts_exact_official_cua_driver_signer_teams(monkeypatch, team: str) -> None:
    monkeypatch.setattr(cua_backend.shutil, "which", lambda name: "/usr/bin/codesign")
    monkeypatch.setattr(
        cua_backend.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stderr=f"Identifier=com.trycua.driver\nTeamIdentifier={team}\n",
        ),
    )

    cua_backend._validate_cua_driver_app_signature("/Applications/CuaDriver.app")


def test_rejects_unrecognized_cua_driver_signer_team(monkeypatch) -> None:
    monkeypatch.setattr(cua_backend.shutil, "which", lambda name: "/usr/bin/codesign")
    monkeypatch.setattr(
        cua_backend.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stderr="Identifier=com.trycua.driver\nTeamIdentifier=NOTOFFICIAL\n",
        ),
    )

    with pytest.raises(RuntimeError, match="expected one of"):
        cua_backend._validate_cua_driver_app_signature("/Applications/CuaDriver.app")
