"""The filesystem handler that mount and format extract from an RDB.

An extracted handler is removed on every exit from mount, and the DOS type
the image supplies reaches neither the temp file's name nor the terminal
unescaped.
"""

import errno
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from amifuse import fuse_fs, platform, rdb_inspect


PFS3 = 0x50465303
HANDLER = b"\x00\x00\x03\xf3handler"

# DOS types whose tag bytes are hostile as a file name or on a terminal.
HOSTILE = {
    "escape-sequence": (0x1B5B324A, "\\x1b[2J"),
    "nul": (0x00414201, "\\x00AB1"),
    "colon": (0x413A4201, "A:B1"),
    "slash-dotdot": (0x2F2E2E01, "/..1"),
    "backslash-dotdot": (0x5C2E2E01, "\\x5c..1"),
    "c1-csi": (0x9B324A20, "\\x9b2J "),
    # A backslash and "x1b", which must not pass for an escaped ESC.
    "faked-escape": (0x5C783162, "\\x5cx1b"),
}


@pytest.fixture
def temp_dir(monkeypatch, tmp_path):
    """The temp directory extract_embedded_driver writes to, empty."""
    path = tmp_path / "temp"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


@pytest.fixture
def extracted(monkeypatch):
    """An RDB whose partition DH0 has a handler of DOS type extracted.dos_type.

    Records the temp files extract_embedded_driver creates.
    """
    state = SimpleNamespace(dos_type=PFS3, paths=[])

    def open_rdisk(*args, **kwargs):
        fs = MagicMock()
        fs.fshd.dos_type = state.dos_type
        fs.get_data.return_value = HANDLER
        rdisk = MagicMock()
        rdisk.find_partition_by_string.return_value.part_blk.dos_env.dos_type = (
            state.dos_type)
        rdisk.fs = [fs]
        return MagicMock(), rdisk, None

    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        state.paths.append(Path(path))
        return fd, path

    monkeypatch.setattr(rdb_inspect, "find_partition_mbr_index", lambda *a: None)
    monkeypatch.setattr(rdb_inspect, "open_rdisk", open_rdisk)
    monkeypatch.setattr(tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(
        fuse_fs, "get_partition_info",
        lambda *a, **kw: {"name": "DH0", "dostype": state.dos_type})
    return state


@pytest.fixture
def bridge(monkeypatch, tmp_path, temp_dir, extracted):
    """mount_fuse on an RDB image with an embedded handler, up to FUSE.

    Returns the HandlerBridge the mount gets. Each test makes one step fail.
    """
    monkeypatch.setattr(rdb_inspect, "detect_adf", lambda image: None)
    monkeypatch.setattr(rdb_inspect, "detect_iso", lambda image: None)
    monkeypatch.setattr(platform, "check_fuse_available", lambda: None)
    monkeypatch.setattr(platform, "validate_mountpoint", lambda mp: None)
    monkeypatch.setattr(platform, "get_fuse_mountpoint", lambda mp: str(mp))
    monkeypatch.setattr(platform, "should_auto_create_mountpoint", lambda mp: True)
    monkeypatch.setattr(platform, "pre_generate_volume_icon", lambda *a, **kw: None)
    monkeypatch.setattr(fuse_fs, "FUSE", MagicMock(side_effect=AssertionError))
    bridge = MagicMock()
    bridge.state.crashed = False
    bridge.volume_name.return_value = "Work"
    monkeypatch.setattr(fuse_fs, "HandlerBridge", MagicMock(return_value=bridge))
    return bridge


def _no_free_drive(mp, bridge):
    mp.setattr(platform, "get_default_mountpoint", lambda name: None)


def _invalid_mountpoint(mp, bridge):
    mp.setattr(platform, "validate_mountpoint", lambda path: "R: is in use")


def _untranslatable_mountpoint(mp, bridge):
    mp.setattr(platform, "get_fuse_mountpoint", MagicMock(side_effect=ValueError("no")))


def _mountpoint_not_created(mp, bridge):
    mp.setattr(platform, "should_auto_create_mountpoint", lambda path: False)
    mp.setattr(Path, "mkdir", MagicMock(
        side_effect=PermissionError(errno.EACCES, "Access is denied")))


def _bridge_refused(mp, bridge):
    mp.setattr(fuse_fs, "HandlerBridge", MagicMock(
        side_effect=OSError(errno.EBUSY, "image is already mounted")))


def _crash_at_startup(mp, bridge):
    bridge.state.crashed = True


def _crash_reading_volume_name(mp, bridge):
    def volume_name():
        bridge.state.crashed = True
        return "Work"

    bridge.volume_name.side_effect = volume_name


def _volume_icon_fails(mp, bridge):
    mp.setattr(platform, "pre_generate_volume_icon",
               MagicMock(side_effect=RuntimeError("icon")))


MOUNT_FAILURES = {
    "no-free-drive": (_no_free_drive, SystemExit),
    "invalid-mountpoint": (_invalid_mountpoint, SystemExit),
    "untranslatable-mountpoint": (_untranslatable_mountpoint, SystemExit),
    "mountpoint-not-created": (_mountpoint_not_created, SystemExit),
    "bridge-refused": (_bridge_refused, SystemExit),
    "crash-at-startup": (_crash_at_startup, SystemExit),
    "crash-reading-volume-name": (_crash_reading_volume_name, SystemExit),
    "volume-icon-fails": (_volume_icon_fails, RuntimeError),
}


@pytest.mark.parametrize("failure", MOUNT_FAILURES)
def test_mount_stopping_before_fuse_removes_extracted_handler(
        monkeypatch, tmp_path, temp_dir, extracted, bridge, failure):
    """Each exit between the extraction and the mount removes the handler."""
    break_step, expected = MOUNT_FAILURES[failure]
    # No mountpoint is given, so mount picks one.
    monkeypatch.setattr(platform, "get_default_mountpoint",
                        lambda name: tmp_path / "mnt")
    break_step(monkeypatch, bridge)

    with pytest.raises(expected):
        fuse_fs.mount_fuse(
            Path("work.hdf"), None, None, None,
            partition="DH0", icons=True, foreground=True,
        )

    assert len(extracted.paths) == 1
    assert not extracted.paths[0].exists()
    assert list(temp_dir.iterdir()) == []


@pytest.fixture
def fuse_runs(monkeypatch):
    """Let mount_fuse reach FUSE, which returns as after an unmount."""
    import amifuse.windows_unmount as windows_unmount

    monkeypatch.setattr(windows_unmount, "UnmountControl", MagicMock())
    monkeypatch.setattr(fuse_fs, "AmigaFuseFS", MagicMock())
    monkeypatch.setattr(fuse_fs, "FUSE", MagicMock())
    return fuse_fs.FUSE


def test_failing_bridge_close_still_removes_extracted_handler(
        tmp_path, temp_dir, extracted, bridge, fuse_runs):
    """An error closing the bridge after unmount does not keep the handler."""
    bridge.close.side_effect = RuntimeError("shutdown")

    with pytest.raises(RuntimeError, match="shutdown"):
        fuse_fs.mount_fuse(
            Path("work.hdf"), None, tmp_path / "mnt", None,
            partition="DH0", foreground=True)

    fuse_runs.assert_called_once()
    assert not extracted.paths[0].exists()
    assert list(temp_dir.iterdir()) == []


def test_failed_removal_keeps_the_mount_error(
        monkeypatch, capsys, tmp_path, temp_dir, extracted, bridge, fuse_runs):
    """A handler that cannot be removed is reported, and does not hide why
    the mount ended."""
    failure = RuntimeError("WinFsp refused the mount")
    fuse_runs.side_effect = failure
    real_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        if self.parent == temp_dir:
            raise PermissionError(errno.EACCES, "Access is denied", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    # debug re-raises the FUSE error on every platform.
    with pytest.raises(RuntimeError) as caught:
        fuse_fs.mount_fuse(
            Path("work.hdf"), None, tmp_path / "mnt", None,
            partition="DH0", foreground=True, debug=True)

    assert caught.value is failure
    bridge.close.assert_called_once_with()
    assert (f"[amifuse] could not remove {extracted.paths[0]}: "
            in capsys.readouterr().err)


@pytest.mark.parametrize("dos_type, shown", HOSTILE.values(), ids=HOSTILE)
def test_extracted_handler_is_named_by_number(temp_dir, extracted, dos_type, shown):
    """The temp file's name holds the DOS type as hex, not its tag bytes."""
    extracted.dos_type = dos_type

    path, tag, number = fuse_fs.extract_embedded_driver(
        Path("work.hdf"), None, "DH0")

    try:
        assert path.parent == temp_dir
        assert path.name.startswith("amifuse_")
        assert path.name.endswith(f"_{dos_type:08x}.handler")
        # The whole handler, in one plain file and no alternate data stream.
        assert list(temp_dir.iterdir()) == [path]
        assert path.read_bytes() == HANDLER
        assert (tag, number) == (shown, dos_type)
    finally:
        path.unlink()


@pytest.mark.parametrize("dos_type, shown", [
    (PFS3, "PFS3"),
    (0x444F5301, "DOS1"),
    (0x53465300, "SFS0"),
    *HOSTILE.values(),
], ids=["PFS3", "DOS1", "SFS0", *HOSTILE])
def test_dos_type_tag_escapes_non_printing_characters(dos_type, shown):
    assert fuse_fs._dos_type_tag(dos_type) == shown


def test_mount_prints_dos_type_escaped(
        monkeypatch, capsys, tmp_path, extracted, bridge):
    """The mount banner shows the RDB driver's DOS type escaped."""
    extracted.dos_type = 0x1B5B324A
    _bridge_refused(monkeypatch, bridge)

    with pytest.raises(SystemExit):
        fuse_fs.mount_fuse(
            Path("work.hdf"), None, tmp_path / "mnt", None,
            partition="DH0", foreground=True)

    out = capsys.readouterr().out
    assert "Filesystem driver: \\x1b[2J/0x1b5b324a (from RDB)" in out
    assert "\x1b" not in out


def test_format_prints_dos_type_escaped(monkeypatch, capsys, tmp_path, temp_dir,
                                        extracted):
    """format shows the partition's DOS type escaped, and removes the handler."""
    extracted.dos_type = 0x1B5B324A
    image = tmp_path / "work.hdf"
    image.write_bytes(b"")
    _bridge_refused(monkeypatch, None)

    with pytest.raises(SystemExit):
        fuse_fs.format_volume(image, None, None, "DH0")

    out = capsys.readouterr().out
    assert "Filesystem driver: \\x1b[2J/0x1b5b324a (from RDB)" in out
    assert "DOS type: \\x1b[2J (0x1b5b324a)" in out
    assert "\x1b" not in out
    assert list(temp_dir.iterdir()) == []
