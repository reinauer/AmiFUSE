"""Shared fixtures for the unit tests."""

import pytest


@pytest.fixture
def user_driver_dir(monkeypatch, tmp_path):
    """An empty user drivers folder under tmp_path, and no dev fallback.

    The folder comes from LOCALAPPDATA on Windows and HOME elsewhere;
    USERPROFILE covers tests that pretend a Windows host is another OS. The
    dev fallback is CI's AmiFUSE-testing clone, which would otherwise serve
    DOS6/DOS7 volumes.
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
