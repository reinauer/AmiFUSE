"""Shared fixtures for the unit tests."""

import json

import pytest


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
