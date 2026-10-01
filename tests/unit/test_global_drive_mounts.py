"""Windows mount-manager drives share one identity with their DOS aliases."""

from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amifuse import platform


DRIVE_ALIASES = ["R:", "r:/", "R:\\", "r:\\.",
                 "\\\\.\\R:", "\\\\.\\r:\\", "//./r:/", "\\\\?\\R:\\"]


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32"))


@pytest.mark.parametrize("mountpoint", DRIVE_ALIASES)
def test_elevated_drive_aliases_use_global_mount_without_mkdir(windows, monkeypatch, mountpoint):
    monkeypatch.setattr(platform, "is_windows_admin", lambda: True)
    monkeypatch.setattr(platform, "_windows_allocated_drive_letters", lambda: {"C"})
    assert platform.windows_drive_mountpoint(mountpoint) == "R:"
    assert platform.should_auto_create_mountpoint(mountpoint)
    assert platform.validate_mountpoint(mountpoint) is None
    assert platform.get_fuse_mountpoint(mountpoint) == "\\\\.\\R:"


@pytest.mark.parametrize("mountpoint", DRIVE_ALIASES)
def test_allocated_drive_rejected_for_every_alias(windows, monkeypatch, mountpoint):
    monkeypatch.setattr(platform, "_windows_allocated_drive_letters", lambda: {"R"})
    assert "Drive R: is already allocated" in platform.validate_mountpoint(mountpoint)


@pytest.mark.parametrize("mountpoint", ["R:", "r:/", "R:\\"])
def test_unelevated_plain_drive_stays_local(windows, monkeypatch, mountpoint):
    monkeypatch.setattr(platform, "is_windows_admin", lambda: False)
    assert platform.get_fuse_mountpoint(mountpoint) == "R:"


@pytest.mark.parametrize("mountpoint", ["\\\\.\\R:", "\\\\?\\R:\\", "//./r:"])
def test_unelevated_explicit_global_drive_reports_privilege_requirement(windows, monkeypatch, mountpoint):
    monkeypatch.setattr(platform, "is_windows_admin", lambda: False)
    with pytest.raises(ValueError, match="Administrator"):
        platform.get_fuse_mountpoint(mountpoint)


@pytest.mark.parametrize("mountpoint", ["R:\\data", "R:data", "\\\\host\\share",
                                       "\\\\.\\PhysicalDrive2", "1:"])
def test_other_paths_are_not_drive_mounts(windows, monkeypatch, mountpoint):
    monkeypatch.setattr(platform, "is_windows_admin", Mock(side_effect=AssertionError))
    assert platform.windows_drive_mountpoint(mountpoint) is None
    assert platform.get_fuse_mountpoint(mountpoint) == mountpoint


@pytest.mark.parametrize("requested", DRIVE_ALIASES)
def test_mount_owner_matching_accepts_global_and_local_aliases(windows, monkeypatch, requested):
    monkeypatch.setattr(platform, "find_amifuse_mounts", lambda: [
        {"pid": i, "mountpoint": value} for i, value in enumerate(DRIVE_ALIASES)
    ] + [{"pid": 50, "mountpoint": "S:"},
         {"pid": 51, "mountpoint": "R:\\subdir"}])
    assert platform._find_mount_owner_pids(requested) == list(range(len(DRIVE_ALIASES)))


def test_windows_pathlib_device_root_has_no_trailing_slash_at_fuse(windows, monkeypatch):
    monkeypatch.setattr(platform, "is_windows_admin", lambda: True)
    assert platform.get_fuse_mountpoint(PureWindowsPath("\\\\.\\R:")) == "\\\\.\\R:"


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_unix_paths_unchanged(monkeypatch, host):
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform=host))
    monkeypatch.setattr(platform, "is_windows_admin", Mock(side_effect=AssertionError))
    assert platform.get_fuse_mountpoint(Path("/mnt/amiga")) == "/mnt/amiga"


@pytest.mark.parametrize("requested", DRIVE_ALIASES)
def test_unmount_checks_normal_drive_root(windows, monkeypatch, requested):
    from amifuse import fuse_fs

    monkeypatch.setattr(fuse_fs, "sys", SimpleNamespace(platform="win32"))
    probe = Mock(return_value=True)
    stop = Mock(return_value=[42])
    monkeypatch.setattr(fuse_fs.os.path, "ismount", probe)
    monkeypatch.setattr(platform, "_find_mount_owner_pids", lambda mp: [42])
    monkeypatch.setattr(platform, "stop_mount_processes", stop)
    fuse_fs.cmd_unmount(SimpleNamespace(mountpoint=PureWindowsPath(requested)))
    probe.assert_called_once_with(Path("R:\\"))
    stop.assert_called_once_with([42])
