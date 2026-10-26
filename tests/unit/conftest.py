"""Shared fixtures for the unit tests."""

import json
import os
import site
import sys

import pytest


@pytest.fixture(autouse=True)
def appdata_sandbox(monkeypatch, tmp_path):
    """Point APPDATA, and the AmiFUSE folder worked out from it, into tmp_path.

    windows_shell and launcher read APPDATA once, when they are imported, so
    setting the variable alone does not move their paths. Without this, tests
    that reach register(), unregister() or launcher._log() write, and delete,
    launch.vbs and launcher.log in the real %APPDATA%\\AmiFUSE. APPDATA itself
    is set too: tray reads it at call time, and child processes inherit it.
    """
    if sys.platform == "win32" and "PYTHONUSERBASE" not in os.environ:
        # A child Python works out its user site-packages from APPDATA too.
        monkeypatch.setenv("PYTHONUSERBASE", site.getuserbase())
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))

    from amifuse import launcher, windows_shell

    amifuse_dir = appdata / "AmiFUSE"
    monkeypatch.setattr(windows_shell, "_AMIFUSE_DIR", amifuse_dir)
    monkeypatch.setattr(windows_shell, "ICON_DIR", amifuse_dir / "icons")
    monkeypatch.setattr(windows_shell, "_LAUNCH_VBS", amifuse_dir / "launch.vbs")
    monkeypatch.setattr(launcher, "_LOG_DIR", amifuse_dir)
    return amifuse_dir


@pytest.fixture
def user_driver_dir(monkeypatch, tmp_path):
    """An empty user drivers folder under tmp_path, and no dev fallback.

    The folder comes from LOCALAPPDATA on Windows and HOME elsewhere;
    USERPROFILE covers tests that pretend a Windows host is another OS. The
    dev fallback is CI's AmiFUSE-testing clone, which would otherwise serve
    the volumes the bundled build does not cover.
    """
    from amifuse import driver_resolver

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setattr(
        driver_resolver, "DEV_FALLBACK_DRIVER", tmp_path / "no-dev" / "FastFileSystem")
    folder = driver_resolver.user_driver_dir()
    assert tmp_path in folder.parents
    folder.mkdir(parents=True)
    return folder


@pytest.fixture
def bundled_dos0_to_dos5(monkeypatch, tmp_path):
    """The bundled provenance record, narrowed to DOS0-DOS5.

    The bundled build covers DOS0-DOS7, so a DOS6/DOS7 volume needs this to
    reach the lookup's answer for a DOS type the bundled build does not cover.
    """
    from amifuse import driver_resolver

    info = json.loads(driver_resolver.BUNDLED_PROVENANCE.read_text(encoding="utf-8"))
    info["dos_types"] = [f"DOS{n}" for n in range(6)]
    record = tmp_path / "dos0-dos5" / driver_resolver.BUNDLED_PROVENANCE.name
    record.parent.mkdir()
    record.write_text(json.dumps(info), encoding="utf-8")
    monkeypatch.setattr(driver_resolver, "BUNDLED_PROVENANCE", record)
    return record
