"""An RDB filesystem or LSEG link to a block past the end of the device.

amitools raises the same IOError for such a block as for a failing read, so
the lenient parse must stop at the link without reading it: the partitions
open and the entries before it stay.
Each image has one DOS3 partition and the bundled FastFileSystem in its RDB,
with the first filesystem header at block 2 and its first LSEG at block 3.
"""

import struct
import subprocess
import sys
from pathlib import Path

import pytest
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.block.rdb.RDBlock import RDBlock

from amifuse import driver_resolver
from amifuse.rdb_inspect import (
    RDBContentError,
    _parse_rdb,
    open_rdisk,
    print_inspect_report,
)

AMITOOLS_ROOT = Path(__file__).resolve().parents[2] / "amitools"
FSHD, LSEG = 2, 3
DOS1, DOS3 = 0x444F5301, 0x444F5303
PAST_END = 0x100000  # The images have 16384 blocks.


def _rdb(tmp_path, fs=("DOS3", "DOS1")):
    """The image, with the driver in the RDB once for each DOS type in fs."""
    image = tmp_path / "pastend.hdf"
    args = ["create", "size=8Mi", "+", "init", f"rdb_cyls={8 * len(fs)}",
            "+", "add", "size=6MiB", "name=DH0", "fs=DOS3"]
    for dos_type in fs:
        args += ["+", "fsadd", str(driver_resolver.BUNDLED_DRIVER),
                 f"dostype={dos_type}"]
    proc = subprocess.run(
        [sys.executable, "-m", "amitools.tools.rdbtool", str(image), *args],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = image.read_bytes()
    assert len(data) // 512 < PAST_END
    assert data[FSHD * 512:FSHD * 512 + 4] == b"FSHD"
    assert data[LSEG * 512:LSEG * 512 + 4] == b"LSEG"
    return image


def _edit(image, blk, changes):
    """Set the longs given as {index: value} in block blk, and redo its
    checksum, so only the link is wrong."""
    data = bytearray(image.read_bytes())
    longs = list(struct.unpack_from(">128I", data, blk * 512))
    for index, value in changes.items():
        longs[index] = value
    longs[2] = 0
    longs[2] = -sum(longs) & 0xFFFFFFFF
    struct.pack_into(">128I", data, blk * 512, *longs)
    image.write_bytes(bytes(data))


def _open(image):
    blkdev, rdisk, _ = open_rdisk(image)
    try:
        return ([str(p.get_drive_name()) for p in rdisk.parts],
                [entry.fshd.dos_type for entry in rdisk.fs],
                rdisk.rdb_warnings, getattr(rdisk, "damaged_fs", []))
    finally:
        rdisk.close()
        blkdev.close()


def test_filesystem_link_past_the_end_ends_the_list(tmp_path):
    """The first header's next link points past the end: the first entry
    is kept, and the list ends there, as at a corrupt header."""
    image = _rdb(tmp_path)
    _edit(image, FSHD, {4: PAST_END})
    parts, fs, warnings, damaged = _open(image)
    assert parts == ["DH0"]
    assert fs == [DOS3]
    assert warnings == [
        f"Filesystem list links to block {PAST_END}, past the end of the "
        f"device (remaining filesystem entries skipped)",
    ]
    assert damaged == [(None, warnings[0])]


def test_lseg_link_past_the_end_drops_the_driver(tmp_path):
    """The first LSEG block's next link points past the end: that entry's
    driver is dropped, and the next entry is still read."""
    image = _rdb(tmp_path)
    _edit(image, LSEG, {4: PAST_END})
    parts, fs, warnings, damaged = _open(image)
    assert parts == ["DH0"]
    assert fs == [DOS1]
    assert warnings == [
        f"Filesystem #0 (DOS3/0x444f5303): LSEG chain links to block "
        f"{PAST_END}, past the end of the device (driver data unavailable)",
    ]
    assert damaged == [(DOS3, warnings[0])]


def test_link_past_a_0x76_entrys_end(tmp_path):
    """Inside an MBR 0x76 entry, the end is the entry's: a filesystem link
    past it, to a block the image still holds, ends the list there too."""
    rdb = _rdb(tmp_path)
    slice_blocks = rdb.stat().st_size // 512
    _edit(rdb, FSHD, {4: slice_blocks + 10})
    lba = 64
    data = bytearray(lba * 512) + rdb.read_bytes() + bytearray(64 * 512)
    data[446 + 4] = 0x76
    struct.pack_into("<II", data, 446 + 8, lba, slice_blocks)
    data[510:512] = b"\x55\xaa"
    image = tmp_path / "mbr.hdf"
    image.write_bytes(bytes(data))
    parts, fs, warnings, damaged = _open(image)
    assert parts == ["DH0"]
    assert fs == [DOS3]
    assert warnings == [
        f"Filesystem list links to block {slice_blocks + 10}, past the end "
        f"of the device (remaining filesystem entries skipped)",
    ]
    assert damaged == [(None, warnings[0])]


def test_inspect_reports_the_link(tmp_path, capsys):
    image = _rdb(tmp_path)
    _edit(image, FSHD, {4: PAST_END})
    print_inspect_report(image, None, False)
    out = capsys.readouterr().out
    assert "DH0" in out
    assert (f"Filesystem list links to block {PAST_END}, past the end of "
            f"the device (remaining filesystem entries skipped)") in out


def test_failing_read_still_raises(tmp_path):
    """A read error is not taken for a link past the end: the lenient parse
    reads the block, and its error propagates as a read error."""
    image = _rdb(tmp_path)
    raw = RawBlockDevice(str(image), read_only=True)
    raw.open()
    try:
        rdb_block = RDBlock(raw, 0)  # rdbtool puts the RDB at block 0.
        assert rdb_block.read()
        real_read = raw.read_block
        failed = []

        def read_block(blk_num, num_blks=1, **kwargs):
            if blk_num == FSHD:
                failed.append(blk_num)
                raise IOError("device read failed")
            return real_read(blk_num, num_blks, **kwargs)

        raw.read_block = read_block
        with pytest.raises(OSError) as caught:
            _parse_rdb(raw, rdb_block)
        assert not isinstance(caught.value, RDBContentError)
        assert str(caught.value) == "device read failed"
        assert failed == [FSHD]  # The lenient parse alone reads it.
    finally:
        raw.close()
