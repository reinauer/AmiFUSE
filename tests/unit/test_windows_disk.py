"""Physical-drive behavior with a sector-enforcing Win32 API double.

Uses real RDB parsing and the runtime backend; never opens a host disk.
"""

import struct
import json
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from amitools.util import Win32Disk as win
from amitools.fs.blkdev.ImageFile import ImageFile
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.FSString import FSString
from amitools.fs.rdb.RDisk import RDisk
from amitools.vamos.disk import HostFileLock, DiskImage
from amifuse.driver_runtime import BlockDeviceBackend
from amifuse.rdb_inspect import detect_mbr, open_rdisk, detect_adf, detect_iso


DEVICE = r"\\.\PhysicalDrive2"


def winerror(code):
    exc = OSError("simulated Windows error %d" % code)
    exc.winerror = code
    return exc


class FakeWin32:
    GENERIC_READ = win._Win32.GENERIC_READ
    GENERIC_WRITE = win._Win32.GENERIC_WRITE
    GET_LENGTH = win._Win32.GET_LENGTH
    GET_GEOMETRY = win._Win32.GET_GEOMETRY
    GET_EXTENTS = win._Win32.GET_EXTENTS
    LOCK_VOLUME = win._Win32.LOCK_VOLUME
    DISMOUNT_VOLUME = win._Win32.DISMOUNT_VOLUME

    def __init__(self):
        self.data = bytearray(320 * 512)
        self.sector = 512
        self.events = []
        self.handles = {}
        self.next_handle = 1
        self.volume_map = {"system": {0}, "cf": {2}, "empty": winerror(21)}
        self.fail_lock = None
        self.fail_geometry = False
        self.fail_flush = False
        self.fail_open = None

    def open(self, path, access, share=3):
        self.events.append(("open", path, access, share))
        if self.fail_open == path:
            raise winerror(5)
        for old_path, old_access, old_share in self.handles.values():
            if old_path == path and (share == 0 or old_share == 0):
                raise winerror(32)
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = (path, access, share)
        return handle

    def close(self, handle):
        self.events.append(("close", self.handles[handle][0]))
        del self.handles[handle]

    def volumes(self):
        yield from self.volume_map

    def volume_disks(self, handle):
        value = self.volume_map[self.handles[handle][0]]
        if isinstance(value, Exception):
            raise value
        return value

    def ioctl(self, handle, code, size=0):
        path = self.handles[handle][0]
        self.events.append(("ioctl", path, code))
        if code == self.GET_LENGTH:
            return struct.pack("<q", len(self.data))
        if code == self.GET_GEOMETRY:
            if self.fail_geometry:
                raise winerror(21)
            return struct.pack("<qIIII", 1, 12, 1, 1, self.sector)
        if code == self.LOCK_VOLUME and self.fail_lock == path:
            raise winerror(32)
        return b""

    def read(self, handle, offset, size):
        assert offset % self.sector == size % self.sector == 0
        assert self.handles[handle][0] == DEVICE
        self.events.append(("read", offset, size))
        result = bytes(self.data[offset:offset + size])
        assert len(result) == size
        return result

    def write(self, handle, offset, data):
        assert offset % self.sector == len(data) % self.sector == 0
        assert self.handles[handle][1] & self.GENERIC_WRITE
        assert offset + len(data) <= len(self.data)
        self.events.append(("write", offset, len(data)))
        self.data[offset:offset + len(data)] = data

    def flush(self, handle):
        assert handle in self.handles
        self.events.append(("flush", self.handles[handle][0]))
        if self.fail_flush:
            raise winerror(1117)


@pytest.fixture
def api(monkeypatch):
    fake = FakeWin32()
    monkeypatch.setattr(win, "_Win32", lambda: fake)
    monkeypatch.setattr(win, "sys", SimpleNamespace(platform="win32"))
    yield fake
    assert not win._sessions
    assert not fake.handles


def make_rdb(tmp_path, api):
    path = tmp_path / "disk.hdf"
    raw = RawBlockDevice(str(path), block_bytes=512)
    raw.create(320)
    rdisk = RDisk(raw)
    try:
        rdisk.create(DiskGeometry(10, 1, 32), rdb_cyls=1)
        rdisk.add_partition(FSString("DH0"), (1, 9))
    finally:
        rdisk.close()
        raw.close()
    api.data[:] = path.read_bytes()


@pytest.mark.parametrize("path", [DEVICE, r"\\?\physicaldrive02", PureWindowsPath(DEVICE)])
def test_device_names(path):
    assert win.physical_drive_number(path) == 2


@pytest.mark.parametrize("path", ["PhysicalDrive2", "disk.hdf", r"\\.\C:",
                                  r"\\.\PhysicalDrive2\file", r"\\.\PhysicalDrive-1"])
def test_regular_paths_are_not_devices(path):
    assert win.physical_drive_number(path) is None


@pytest.mark.parametrize("read_only", [True, False])
@pytest.mark.parametrize("sector", [512, 4096])
def test_runtime_rdb_mount_and_persistence(tmp_path, api, read_only, sector):
    make_rdb(tmp_path, api)
    api.sector = sector
    original = bytes(api.data)
    backend = BlockDeviceBackend(Path(DEVICE), read_only=read_only)
    backend.open()
    try:
        assert backend.exclusive
        assert backend.read_blocks(0)[:4] == b"RDSK"
        assert backend.read_blocks(319) == bytes(512)
        if read_only:
            with pytest.raises(PermissionError):
                backend.write_blocks(319, b"x" * 512)
        else:
            backend.write_blocks(319, b"x" * 512)
            backend.sync()
            assert backend.read_blocks(319) == b"x" * 512
        # Inspection must borrow the session handle, including the MBR probe.
        assert detect_mbr(Path(DEVICE)) is None
        dev, rdisk, _ = open_rdisk(Path(DEVICE))
        rdisk.close()
        dev.close()
        competitor = DiskImage(Path(r"\\?\physicaldrive02"))
        with pytest.raises(IOError, match="exclusively lock"):
            competitor.open()
        competitor.close()
    finally:
        backend.close()
    assert not backend.exclusive
    assert not api.handles
    assert api.data[:-512] == original[:-512]
    # Reopen after all handles close and verify writes reached the disk.
    again = DiskImage(Path(DEVICE)).open()
    try:
        assert again.read_blocks(319) == (bytes(512) if read_only else b"x" * 512)
    finally:
        again.close()
    opens = [e for e in api.events if e[:2] == ("open", DEVICE)]
    assert len(opens) == 2
    assert all(e[3] == 0 for e in opens)
    if read_only:
        assert not any(e[0] in ("write", "flush") for e in api.events)
        assert all(e[2] == api.GENERIC_READ for e in opens)


def test_windows_volume_locks_and_close_order(api):
    lock = win.DiskLock(DEVICE, read_only=False)
    lock.close()
    assert ("ioctl", "cf", api.LOCK_VOLUME) in api.events
    assert ("ioctl", "cf", api.DISMOUNT_VOLUME) in api.events
    assert ("ioctl", "system", api.LOCK_VOLUME) not in api.events
    flush = max(i for i, e in enumerate(api.events) if e == ("flush", DEVICE))
    assert flush < api.events.index(("close", DEVICE))
    assert api.events[-1] == ("close", "cf")


@pytest.mark.parametrize("failure", ["lock", "geometry", "access", "unknown_volume"])
def test_open_failures_release_every_handle(api, failure):
    if failure == "lock":
        api.fail_lock = "cf"
    elif failure == "geometry":
        api.fail_geometry = True
    elif failure == "access":
        api.fail_open = DEVICE
    else:
        api.volume_map["unknown"] = winerror(5)
    lock = HostFileLock(DEVICE, read_only=False)
    with pytest.raises(IOError, match="exclusively lock"):
        lock.acquire()
    assert not lock.is_locked
    assert not api.handles
    assert not any(e[0] == "write" for e in api.events)


def test_flush_failure_releases_disk_and_volumes(api):
    lock = HostFileLock(DEVICE, read_only=False)
    lock.acquire()
    api.fail_flush = True
    with pytest.raises(OSError):
        lock.release()
    assert not lock.is_locked
    assert not api.handles


def test_independent_cursors_and_read_only_borrow(api):
    api.data[:1024] = b"a" * 512 + b"b" * 512
    lock = win.DiskLock(DEVICE, read_only=False)
    try:
        with win.open_disk(DEVICE) as a, win.open_disk(DEVICE) as b:
            a.seek(512)
            assert a.read(7) == b"b" * 7
            assert b.read(7) == b"a" * 7
            assert a.tell() == 519
            with pytest.raises(PermissionError):
                a.write(b"!")
            # A reader closing must not release the mount's disk handle.
        assert any(path == DEVICE for path, _, _ in api.handles.values())
    finally:
        lock.close()


def test_cannot_upgrade_read_only_session(api):
    lock = win.DiskLock(DEVICE)
    try:
        with pytest.raises(PermissionError):
            win.open_disk(DEVICE, read_only=False)
    finally:
        lock.close()


def test_sector_preservation_and_bounds(api):
    api.sector = 4096
    api.data[:] = b"a" * len(api.data)
    with win.open_disk(DEVICE, read_only=False) as stream:
        stream.seek(4090)
        assert stream.write(b"b" * 20) == 20
        assert api.data[4089:4111] == b"a" + b"b" * 20 + b"a"
        assert api.data[:4090] == b"a" * 4090
        assert api.data[4110:] == b"a" * (len(api.data) - 4110)
        stream.seek(-4, 2)
        assert stream.read(100) == b"a" * 4
        assert stream.read() == b""
        with pytest.raises(ValueError, match="exceeds"):
            stream.write(b"x")
        with pytest.raises(ValueError):
            stream.seek(-1)
    with pytest.raises(ValueError):
        stream.read(1)


def test_reject_create_and_resize(api):
    image = ImageFile(DEVICE)
    with pytest.raises(IOError, match="truncate"):
        image.create(1)
    with pytest.raises(IOError, match="resize"):
        image.resize(1)
    assert not api.events


def test_device_probes_skip_host_file_metadata(api, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("physical disk passed to file metadata API")

    monkeypatch.setattr(win.os.path, "getsize", unexpected)
    assert ImageFile.get_image_size(DEVICE) == len(api.data)
    assert detect_adf(Path(DEVICE)) is None
    assert detect_iso(Path(DEVICE)) is None


def test_mbr_probe_with_4k_sectors(api):
    api.sector = 4096
    api.data[510:512] = b"\x55\xaa"
    api.data[450] = 0x76
    struct.pack_into("<II", api.data, 454, 8, 300)
    result = detect_mbr(Path(DEVICE))
    assert result.has_amiga_partitions
    assert result.partitions[0].start_lba == 8
    assert ("read", 0, 4096) in api.events


def test_cli_inspect_accepts_device_without_file_exists(tmp_path, api, monkeypatch, capsys):
    from amifuse import fuse_fs

    make_rdb(tmp_path, api)
    monkeypatch.setattr("sys.argv", ["amifuse", "inspect", DEVICE, "--json"])
    fuse_fs.main()
    result = json.loads(capsys.readouterr().out)
    assert result["image"] == DEVICE
    assert "DH0" in json.dumps(result)


def test_rdb_inside_mbr_uses_locked_device(tmp_path, api):
    make_rdb(tmp_path, api)
    # Put a complete RDB in an Emu68-style MBR partition at LBA 2048,
    # outside the initial 16-block direct-RDB/Parceiro scan.
    api.data[:0] = bytes(2048 * 512)
    api.sector = 4096
    api.data[510:512] = b"\x55\xaa"
    api.data[450] = 0x76
    struct.pack_into("<II", api.data, 454, 2048, 320)
    original = bytes(api.data)
    backend = BlockDeviceBackend(Path(DEVICE), read_only=False)
    try:
        backend.open()
        assert backend.read_blocks(0)[:4] == b"RDSK"
        backend.write_blocks(319, b"z" * 512)
        backend.sync()
    finally:
        backend.close()
    assert api.data[:-512] == original[:-512]
    assert api.data[-512:] == b"z" * 512
