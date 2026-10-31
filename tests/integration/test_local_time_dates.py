"""Dates on real handlers, in local time (#146, #160).

The emulated clock tells local time, getattr() and readdir() show each
entry's own date, and utimens() sets it, also on a file open for writing,
which the handler stamps when it closes. The tests call the AmigaFuseFS
callbacks directly on copies of the fixture images and on an ADF the
bundled FastFileSystem serves; no host FUSE driver is needed. The host's
UTC offset is patched to seven hours west, so the tests do not depend on
the machine's time zone.
"""

import os
import shutil
import time
from pathlib import Path

import pytest

from amifuse import amiga_time
from amifuse.amiga_time import AMIGA_EPOCH, host_time
from amifuse.driver_resolver import BUNDLED_DRIVER
from amifuse.fuse_fs import AmigaFuseFS, HandlerBridge
from amifuse.rdb_inspect import detect_adf

OFFSET = -7 * 3600
BUNDLED = "bundled FastFileSystem"
# 2001-02-03 04:05:06 UTC. SmartFilesystem keeps whole seconds.
DATE = 981173106.0

HANDLERS = [
    pytest.param(("pfs.hdf", "pfs3aio", "/Identify.info"),
                 marks=pytest.mark.integration, id="pfs3aio"),
    pytest.param(("sfs.hdf", "SmartFilesystem", "/Prefs.info"),
                 marks=pytest.mark.integration, id="SmartFilesystem"),
    pytest.param(("ofs.adf", "FastFileSystem", "/OFS_README.txt"),
                 marks=pytest.mark.integration, id="FastFileSystem"),
    # Not integration-marked, as in test_bundled_ffs: the image is built here.
    pytest.param((None, BUNDLED, "/old.txt"), id="bundled"),
]

# The bundled image's file, dated 2010-02-03 12:34:56.40 local time.
OLD_STAMP = (11721, 12 * 60 + 34, 56 * 50 + 20)


@pytest.fixture(autouse=True)
def west_of_utc(monkeypatch):
    monkeypatch.setattr(amiga_time, "utc_offset", lambda t: OFFSET)


def _machine68k_or_skip():
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")


def _bundled_image(tmp_path: Path) -> Path:
    from amitools.fs import DosType
    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.FSString import FSString
    from amitools.fs.TimeStamp import TimeStamp
    from amitools.fs.blkdev.ADFBlockDevice import ADFBlockDevice

    _machine68k_or_skip()
    image = tmp_path / "bundled.adf"
    blkdev = ADFBlockDevice(str(image))
    blkdev.create()
    volume = ADFSVolume(blkdev)
    volume.create(FSString("Gen"), dos_type=DosType.DOS3)
    volume.write_file(b"old\n", FSString("old.txt"))
    volume.get_path_name(FSString("old.txt")).change_mod_ts(TimeStamp(*OLD_STAMP))
    volume.close()
    blkdev.close()
    return image


def _stored_stamp(image: Path, path: str):
    """The DateStamp amitools (xdftool) reads for path on an ADF."""
    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.FSString import FSString
    from amitools.fs.blkdev.ADFBlockDevice import ADFBlockDevice

    blkdev = ADFBlockDevice(str(image), read_only=True)
    blkdev.open()
    try:
        volume = ADFSVolume(blkdev)
        volume.open()
        ts = volume.get_path_name(FSString(path.lstrip("/"))).block.mod_ts
        volume.close()
    finally:
        blkdev.close()
    return ts.days, ts.mins, ts.ticks


@pytest.fixture(params=HANDLERS)
def volume(request, tmp_path):
    """(AmigaFuseFS on a writable copy, image, an existing file, handler name)."""
    image_name, driver_name, existing = request.param
    if driver_name == BUNDLED:
        image, driver = _bundled_image(tmp_path), BUNDLED_DRIVER
    else:
        fixture_root = request.getfixturevalue("fixture_root")
        source = fixture_root / "fixtures" / "readonly" / image_name
        driver = fixture_root / "drivers" / driver_name
        if not source.exists() or not driver.exists():
            pytest.skip(f"Missing {image_name} or {driver_name}")
        image = tmp_path / image_name
        shutil.copy2(source, image)
    bridge = HandlerBridge(image, driver, read_only=False, adf_info=detect_adf(image))
    try:
        yield AmigaFuseFS(bridge), image, existing, driver_name
    finally:
        bridge.close()


def _mtime(ops, path):
    return ops.getattr(path)["st_mtime"]


def test_clock_reads_tell_local_time():
    """DateStamp, GetSysTime and CurrentTime agree, at local time."""
    from amitools.vamos.machine.regs import REG_D1
    from amifuse.vamos_runner import VamosHandlerRuntime

    _machine68k_or_skip()
    rt = VamosHandlerRuntime()
    rt.setup()
    try:
        ctx = rt.slm.dos_ctx
        mem = ctx.mem
        buf = rt.alloc.alloc_memory(20, label="clock").addr
        lib_mgr = rt.slm.lib_mgr
        intuition = lib_mgr.get_vlib_by_addr(lib_mgr.open_lib("intuition.library", 0)).get_impl()
        expected = time.time() + OFFSET - AMIGA_EPOCH
        ctx.cpu.w_reg(REG_D1, buf)
        rt.slm.dos_impl.DateStamp(ctx)
        stamp = mem.r32(buf) * 86400 + mem.r32(buf + 4) * 60 + mem.r32(buf + 8) / 50
        sys_secs, _ = rt.timer_device.get_sys_time()
        intuition.CurrentTime(ctx, buf + 12, buf + 16)
        current = mem.r32(buf + 12)
    finally:
        rt.shutdown()
    for secs in (stamp, sys_secs, current):
        assert abs(secs - expected) < 5


def test_existing_entry_shows_its_stored_date(volume):
    """getattr() and readdir() report the date on the volume, as xdftool reads it."""
    ops, image, existing, _ = volume
    if image.suffix == ".adf":
        expected = host_time(*_stored_stamp(image, existing))
        assert abs(_mtime(ops, existing) - expected) < 0.01
    if image.name == "bundled.adf":
        assert _mtime(ops, existing) == pytest.approx(
            host_time(*OLD_STAMP), abs=0.01)
    ops.readdir("/", None)
    stat = ops.getattr(existing)
    assert stat["st_mtime"] == stat["st_ctime"] == stat["st_atime"]
    assert stat["st_mtime"] < time.time() - 86400


def test_new_file_reads_back_as_now(volume):
    """The handler stamps a new file with the local time, which reads back as now."""
    ops, *_ = volume
    fh = ops.create("/new.txt", 0o644)
    ops.write("/new.txt", b"x" * 1000, 0, fh)
    ops.release("/new.txt", fh)
    assert abs(ops.bridge.stat_path("/new.txt")["mtime"] - time.time()) < 10
    assert abs(_mtime(ops, "/new.txt") - time.time()) < 10


def test_utimens_sets_date_of_closed_file(volume):
    ops, image, existing, _ = volume
    assert ops.utimens(existing, (DATE, DATE)) == 0
    assert _mtime(ops, existing) == pytest.approx(DATE, abs=0.01)
    ops.bridge.flush_volume()
    if image.suffix == ".adf":
        assert host_time(*_stored_stamp(image, existing)) == pytest.approx(DATE, abs=0.01)


def test_date_before_1978_is_1978(volume):
    ops, _, existing, _ = volume
    ops.utimens(existing, (0, 0))
    assert _mtime(ops, existing) == pytest.approx(AMIGA_EPOCH - OFFSET, abs=0.01)


@pytest.mark.parametrize("flags", [None, os.O_RDWR], ids=["create", "open-rdwr"])
def test_date_set_on_open_written_file_survives_close(volume, flags):
    """The handler stamps the file when it closes; the last date asked for comes after."""
    ops, _, existing, _ = volume
    if flags is None:
        path = "/dated.txt"
        fh = ops.create(path, 0o644)
    else:
        path = existing
        ops.utimens(path, (DATE - 86400, DATE - 86400))
        fh = ops.open(path, flags)
    ops.write(path, b"data\n" * 200, 0, fh)
    assert ops.utimens(path, (DATE + 86400, DATE + 86400)) == 0
    ops.write(path, b"more\n", 1000, fh)
    assert ops.utimens(path, (DATE, DATE)) == 0
    ops.release(path, fh)
    assert _mtime(ops, path) == pytest.approx(DATE, abs=0.01)


@pytest.fixture
def winfsp(monkeypatch):
    """WinFsp's calls: a handle keeps the name it was opened under, and an
    access-time-only change comes with the date getattr() reports."""
    monkeypatch.setattr(AmigaFuseFS, "_winfsp", True)


@pytest.fixture
def libfuse(monkeypatch):
    """libfuse's calls: the current name, and both times as asked for."""
    monkeypatch.setattr(AmigaFuseFS, "_winfsp", False)


def _renames_open_files(driver_name):
    if driver_name in (BUNDLED, "FastFileSystem"):
        pytest.skip("FastFileSystem refuses to rename an open file")


def _open_written(ops, path):
    fh = ops.create(path, 0o644)
    ops.write(path, b"data\n" * 200, 0, fh)
    return fh


def _write_closed(ops, path):
    ops.release(path, _open_written(ops, path))


def test_unchanged_date_on_written_file_leaves_the_close_stamp(volume, winfsp):
    """WinFsp passes an access-time-only change with the date getattr() reports.

    On a file open for writing that is the date before the write, and the
    handler's stamp at close stands.
    """
    ops, _, existing, _ = volume
    ops.utimens(existing, (DATE, DATE))
    assert _mtime(ops, existing) == pytest.approx(DATE, abs=0.01)
    fh = ops.open(existing, os.O_RDWR)
    ops.write(existing, b"data\n" * 200, 0, fh)
    assert _mtime(ops, existing) == pytest.approx(DATE, abs=0.01)
    assert ops.utimens(existing, (time.time(), DATE)) == 0
    ops.release(existing, fh)
    assert abs(_mtime(ops, existing) - time.time()) < 10


def test_last_date_wins_on_an_open_file_under_libfuse(volume, libfuse):
    """libfuse passes the times asked for, so setting a file's own date back counts."""
    ops, _, existing, _ = volume
    ops.utimens(existing, (DATE, DATE))
    fh = ops.open(existing, os.O_RDWR)
    assert ops.utimens(existing, (DATE + 86400, DATE + 86400)) == 0
    assert ops.utimens(existing, (DATE, DATE)) == 0
    ops.release(existing, fh)
    assert _mtime(ops, existing) == pytest.approx(DATE, abs=0.01)


def test_date_for_the_name_an_open_file_was_renamed_from(volume, winfsp):
    """The date is held for the renamed file, not set on a new file under the
    old name, and dropped while another open handle has that name too."""
    ops, _, _, driver_name = volume
    _renames_open_files(driver_name)
    # Two open files answer to /a.txt: the request is dropped.
    fh_b = _open_written(ops, "/a.txt")
    ops.rename("/a.txt", "/b.txt")
    fh_a = _open_written(ops, "/a.txt")
    assert ops.utimens("/a.txt", (DATE, DATE)) == 0
    ops.release("/a.txt", fh_b)
    ops.release("/a.txt", fh_a)
    for path in ("/a.txt", "/b.txt"):
        assert abs(_mtime(ops, path) - time.time()) < 10
    # Only the renamed file is open: the date is held for it.
    fh_d = _open_written(ops, "/c.txt")
    ops.rename("/c.txt", "/d.txt")
    _write_closed(ops, "/c.txt")
    new_date = _mtime(ops, "/c.txt")
    assert ops.utimens("/c.txt", (DATE, DATE)) == 0
    assert _mtime(ops, "/c.txt") == pytest.approx(new_date, abs=0.01)
    ops.release("/c.txt", fh_d)
    assert _mtime(ops, "/d.txt") == pytest.approx(DATE, abs=0.01)
    assert _mtime(ops, "/c.txt") == pytest.approx(new_date, abs=0.01)


def test_directory_at_the_name_an_open_file_was_renamed_from(volume, winfsp):
    """A directory open under the old name makes the request ambiguous: it is dropped."""
    ops, _, _, driver_name = volume
    _renames_open_files(driver_name)
    fh = _open_written(ops, "/a")
    ops.rename("/a", "/b")
    ops.mkdir("/a", 0o755)
    dh = ops.opendir("/a")
    assert ops.utimens("/a", (DATE, DATE)) == 0
    assert _mtime(ops, "/a") != pytest.approx(DATE, abs=0.01)
    ops.releasedir("/a", dh)
    ops.release("/a", fh)
    assert abs(_mtime(ops, "/b") - time.time()) < 10
    assert ops.utimens("/a", (DATE, DATE)) == 0
    assert _mtime(ops, "/a") == pytest.approx(DATE, abs=0.01)


def test_unchanged_date_after_a_rename_leaves_the_close_stamp(volume, winfsp):
    """WinFsp's access-time-only change, under the old name, is compared
    with the renamed file's date, not the date of a file now at that name."""
    ops, _, _, driver_name = volume
    _renames_open_files(driver_name)
    _write_closed(ops, "/e.txt")
    ops.utimens("/e.txt", (DATE, DATE))
    assert _mtime(ops, "/e.txt") == pytest.approx(DATE, abs=0.01)
    fh = ops.open("/e.txt", os.O_RDWR)
    ops.write("/e.txt", b"more\n", 0, fh)
    ops.rename("/e.txt", "/f.txt")
    _write_closed(ops, "/e.txt")
    new_date = _mtime(ops, "/e.txt")
    assert ops.utimens("/e.txt", (time.time(), _mtime(ops, "/f.txt"))) == 0
    assert _mtime(ops, "/e.txt") == pytest.approx(new_date, abs=0.01)
    ops.release("/e.txt", fh)
    assert abs(_mtime(ops, "/f.txt") - time.time()) < 10
    assert _mtime(ops, "/e.txt") == pytest.approx(new_date, abs=0.01)


def test_name_renamed_from_is_the_new_file_under_libfuse(volume, libfuse):
    """The kernel's FUSE passes the current name: a request for it dates that file."""
    ops, _, _, driver_name = volume
    _renames_open_files(driver_name)
    fh = _open_written(ops, "/a.txt")
    ops.rename("/a.txt", "/b.txt")
    _write_closed(ops, "/a.txt")
    assert ops.utimens("/a.txt", (DATE, DATE)) == 0
    assert _mtime(ops, "/a.txt") == pytest.approx(DATE, abs=0.01)
    ops.release("/b.txt", fh)
    assert abs(_mtime(ops, "/b.txt") - time.time()) < 10


def test_held_date_follows_a_rename(volume):
    ops, _, _, driver_name = volume
    if driver_name in (BUNDLED, "FastFileSystem"):
        pytest.skip("FastFileSystem refuses to rename an open file")
    fh = ops.create("/a.txt", 0o644)
    ops.write("/a.txt", b"data\n", 0, fh)
    ops.utimens("/a.txt", (DATE, DATE))
    ops.rename("/a.txt", "/b.txt")
    ops.release("/b.txt", fh)
    assert _mtime(ops, "/b.txt") == pytest.approx(DATE, abs=0.01)
