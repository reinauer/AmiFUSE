"""truncate() follow-ups on the real handlers.

Each test runs the FUSE callbacks a mounted volume receives on a writable
HandlerBridge over a copy of a fixture image, then reads the result back
in a fresh session.
"""

import errno
import os
import shutil

import pytest

from amifuse.fuse_fs import AmigaFuseFS, FuseOSError, SeekResult
from tests.integration.test_handler_bridge_write import _bridge, _make_ffs_adf

pytestmark = pytest.mark.integration

# pfs3aio keeps a handle's offset in its fileentry, which fh_Args points to.
_FH_ARGS = 36
_PFS3_FE_OFFSET = 74


def _volume(fixture_root, tmp_path, driver_name, image_name):
    """A copy of a volume, as (image, driver); image_name None makes FFS."""
    driver = fixture_root / "drivers" / driver_name
    if not driver.exists():
        pytest.skip(f"{driver_name} handler not found")
    if image_name is None:
        image = tmp_path / "ffs.adf"
        _make_ffs_adf(image)
        return image, driver
    source = fixture_root / "fixtures" / "readonly" / image_name
    if not source.exists():
        pytest.skip(f"{image_name} not found")
    image = tmp_path / image_name
    shutil.copy2(source, image)
    return image, driver


def _make_file(ops, path, data):
    handle = ops.create(path, 0o644)
    assert ops.write(path, data, 0, handle) == len(data)
    ops.release(path, handle)


def _read_back(image, driver, path):
    bridge = _bridge(image, driver)
    try:
        return bridge.read_file(path, 1 << 20, 0)
    finally:
        bridge.close()


def test_sfs_write_at_new_end_on_nearly_full_volume(fixture_root, tmp_path):
    """SmartFilesystem failed the write after the shrink with EIO, and
    again on retry, when another file filled the volume to 96 KiB free
    while this one was open."""
    image, driver = _volume(fixture_root, tmp_path, "SmartFilesystem",
                            "sfs.hdf")
    bridge = _bridge(image, driver, read_only=False)

    def free():
        info = bridge.get_disk_info()
        return ((info["num_blocks"] - info["num_blocks_used"])
                * info["bytes_per_block"])

    try:
        ops = AmigaFuseFS(bridge)
        handle = ops.create("/f.bin", 0o644)
        assert ops.write("/f.bin", b"h" * 1000, 0, handle) == 1000
        fill = ops.create("/fill.bin", 0o644)
        offset = 0
        while free() > 96 * 1024:
            chunk = bytes(min(65536, free() - 96 * 1024))
            offset += ops.write("/fill.bin", chunk, offset, fill)
        ops.release("/fill.bin", fill)
        assert ops.write("/f.bin", b"x" * 4096, 1000, handle) == 4096
        assert ops.truncate("/f.bin", 1000, handle) == 0
        assert ops.write("/f.bin", b"tail", 1000, handle) == 4
        ops.release("/f.bin", handle)
        bridge.flush_volume()
    finally:
        bridge.close()

    assert _read_back(image, driver, "/f.bin") == b"h" * 1000 + b"tail"


def _answer_first_shrink_late(bridge):
    """Make the first ACTION_SET_FILE_SIZE's wait end before its reply.

    That wait runs out at once, as one that reached its limit does, so the
    handler answers during the next wait. A wait cut to a few cycles would
    not do: a handler stopped mid-work runs on against the busy limit.
    Returns a list that holds True once the cut wait ran.
    """
    send = bridge.launcher.send_set_file_size
    run = bridge._run_until_replies
    state = []

    def send_set_file_size(*args, **kwargs):
        if not state:
            state.append(False)
        return send(*args, **kwargs)

    def run_until_replies(*args, **kwargs):
        if state == [False]:
            state[0] = True
            return []
        return run(*args, **kwargs)

    bridge.launcher.send_set_file_size = send_set_file_size
    bridge._run_until_replies = run_until_replies
    return state


@pytest.mark.parametrize("driver_name, image_name", [
    ("FastFileSystem", None), ("pfs3aio", "pfs.hdf"),
], ids=["ffs", "pfs3"])
def test_shrink_answered_after_the_wait(fixture_root, tmp_path, driver_name,
                                        image_name):
    """The handler answered the shrink just after the wait ran out, and
    truncate() failed with EIO for a shrink it made. FastFileSystem stops
    it at the reader and needs the retry as well."""
    image, driver = _volume(fixture_root, tmp_path, driver_name, image_name)
    data = bytes((i * 7) & 0xFF for i in range(60000))
    other = bytes((i * 3) & 0xFF for i in range(5000))
    bridge = _bridge(image, driver, read_only=False)
    try:
        ops = AmigaFuseFS(bridge)
        _make_file(ops, "/a.bin", data)
        _make_file(ops, "/o.bin", other)
        handle = ops.open("/a.bin", os.O_RDWR)
        reader = ops.open("/a.bin", os.O_RDONLY)
        assert ops.read("/a.bin", 2000, 0, reader) == data[:2000]
        other_reader = ops.open("/o.bin", os.O_RDONLY)
        assert ops.read("/o.bin", 1000, 0, other_reader) == other[:1000]
        cut = _answer_first_shrink_late(bridge)

        assert ops.truncate("/a.bin", 100, handle) == 0
        assert cut == [True]
        for path, fh in (("/a.bin", handle), ("/a.bin", reader),
                         ("/o.bin", other_reader)):
            ops.release(path, fh)
        bridge.flush_volume()
    finally:
        bridge.close()

    assert _read_back(image, driver, "/a.bin") == data[:100]
    assert _read_back(image, driver, "/o.bin") == other


def test_pfs3_reader_past_2_gib_after_shrink(fixture_root, tmp_path):
    """pfs3aio moves a reader past 2 GiB on the file to the new end. Its
    next read at its old offset returned the bytes written there since.

    Each reader seeks to 2000, then gets 0x80000000 written into pfs3aio's
    offset for it, standing in for a reader past 2 GiB of a file that size.
    """
    image, driver = _volume(fixture_root, tmp_path, "pfs3aio", "pfs.hdf")
    content = bytes(range(256)) * 12  # 3072 bytes
    bridge = _bridge(image, driver, read_only=False)
    try:
        ops = AmigaFuseFS(bridge)
        _make_file(ops, "/a.bin", content)
        _make_file(ops, "/b.bin", content)
        reader = ops.open("/a.bin", os.O_RDONLY)
        # On another file, which the shrink does not move.
        other_reader = ops.open("/b.bin", os.O_RDONLY)
        for fh in (reader, other_reader):
            fh_addr = ops._fh_cache[fh]["fh_addr"]
            assert bridge.seek_handle(fh_addr, 2000)[0] is SeekResult.OK
            fileentry = bridge.mem.r32(fh_addr + _FH_ARGS)
            bridge.mem.w32(fileentry + _PFS3_FE_OFFSET, 0x80000000)
            ops._fh_cache[fh]["pos"] = 0x80000000
        handle = ops.open("/a.bin", os.O_RDWR)

        assert ops.truncate("/a.bin", 100, handle) == 0
        assert ops.write("/a.bin", b"N" * 5000, 100, handle) == 5000
        # No seek reaches 0x80000000, so the read fails.
        with pytest.raises(FuseOSError) as exc_info:
            ops.read("/a.bin", 100, 0x80000000, reader)
        assert exc_info.value.args[0] == errno.EIO
        assert ops._fh_cache[other_reader]["pos"] == 0x80000000
        for path, fh in (("/a.bin", handle), ("/a.bin", reader),
                         ("/b.bin", other_reader)):
            ops.release(path, fh)
        bridge.flush_volume()
    finally:
        bridge.close()

    assert _read_back(image, driver, "/a.bin") == content[:100] + b"N" * 5000
