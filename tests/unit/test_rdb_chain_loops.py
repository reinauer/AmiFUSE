"""An RDB whose filesystem list or LSEG chain links back on itself.

amitools' strict open stops at the damaged LSEG block of these images, and
the lenient parse that follows must stop at the loop instead of following it
forever. Each image has one DOS3 partition and the bundled FastFileSystem in
its RDB, with the filesystem header at block 2 and the first LSEG at block 3.
"""

import struct
import subprocess
import sys
from pathlib import Path

import pytest

from amifuse import driver_resolver
from amifuse.rdb_inspect import open_rdisk, print_inspect_report

AMITOOLS_ROOT = Path(__file__).resolve().parents[2] / "amitools"
FSHD, LSEG = 2, 3
DOS1, DOS3 = 0x444F5301, 0x444F5303


def _rdb(tmp_path, fs=("DOS3",)):
    """The image, with the driver in the RDB once for each DOS type in fs."""
    image = tmp_path / "loop.hdf"
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
    assert data[FSHD * 512:FSHD * 512 + 4] == b"FSHD"
    assert data[LSEG * 512:LSEG * 512 + 4] == b"LSEG"
    assert data[(LSEG + 1) * 512:(LSEG + 1) * 512 + 4] == b"LSEG"
    return image


def _edit(image, blk, changes, summed=128):
    """Set the longs given as {index: value} in block blk, and redo its
    checksum over the first summed longs (128 is a standard checksum, fewer
    a Parceiro one)."""
    data = bytearray(image.read_bytes())
    longs = list(struct.unpack_from(">128I", data, blk * 512))
    for index, value in changes.items():
        longs[index] = value
    longs[2] = 0
    longs[2] = -sum(longs[:summed]) & 0xFFFFFFFF
    struct.pack_into(">128I", data, blk * 512, *longs)
    image.write_bytes(bytes(data))


def _corrupt(image, blk):
    """Flip one bit in block blk's data, so its checksum fails."""
    data = bytearray(image.read_bytes())
    data[blk * 512 + 43] ^= 1
    image.write_bytes(bytes(data))


def _fs_list_loop(tmp_path):
    """The filesystem header links to itself, and its first LSEG block is
    corrupt, so the strict open fails before it follows the link."""
    image = _rdb(tmp_path)
    _edit(image, FSHD, {4: FSHD})
    _corrupt(image, LSEG)
    return image


def _open(image):
    blkdev, rdisk, _ = open_rdisk(image)
    try:
        return ([str(p.get_drive_name()) for p in rdisk.parts], rdisk.fs,
                rdisk.rdb_warnings, getattr(rdisk, "damaged_fs", []))
    finally:
        rdisk.close()
        blkdev.close()


@pytest.mark.timeout(10)
def test_filesystem_list_loop_ends_the_list(tmp_path):
    """The loop is a broken link, so it may hide entries, as a corrupt
    header does."""
    parts, fs, warnings, damaged = _open(_fs_list_loop(tmp_path))
    assert parts == ["DH0"]
    assert fs == []
    assert warnings == [
        "Filesystem #0 (DOS3/0x444f5303): corrupt data block in LSEG chain "
        "(driver data unavailable)",
        "Filesystem list loops back to block 2 "
        "(remaining filesystem entries skipped)",
    ]
    assert damaged == [(DOS3, warnings[0]), (None, warnings[1])]


@pytest.mark.timeout(10)
@pytest.mark.parametrize("from_blk", [LSEG, LSEG + 1], ids=["self", "later"])
def test_lseg_chain_loop_drops_the_driver(tmp_path, from_blk):
    """An LSEG block with a Parceiro checksum links back to the first LSEG
    block: itself, or from the block after it."""
    image = _rdb(tmp_path)
    _edit(image, from_blk, {1: 64, 4: LSEG}, summed=64)
    parts, fs, warnings, damaged = _open(image)
    assert parts == ["DH0"]
    assert fs == []
    assert warnings == [
        "Filesystem #0 (DOS3/0x444f5303): LSEG chain loops back to block 3 "
        "(driver data unavailable)",
    ]
    assert damaged == [(DOS3, warnings[0])]


@pytest.mark.timeout(10)
def test_lseg_chain_loop_after_a_damaged_entry(tmp_path):
    """The strict open stops at the first entry's corrupt LSEG block, before
    the second entry, whose LSEG chain links back to itself with standard
    checksums throughout."""
    image = _rdb(tmp_path, ("DOS3", "DOS1"))
    _corrupt(image, LSEG)
    fshd = struct.unpack_from(">I", image.read_bytes(), FSHD * 512 + 16)[0]
    lseg = struct.unpack_from(">I", image.read_bytes(), fshd * 512 + 18 * 4)[0]
    _edit(image, lseg, {4: lseg})
    parts, fs, warnings, damaged = _open(image)
    assert parts == ["DH0"]
    assert fs == []
    assert warnings == [
        "Filesystem #0 (DOS3/0x444f5303): corrupt data block in LSEG chain "
        "(driver data unavailable)",
        f"Filesystem #1 (DOS1/0x444f5301): LSEG chain loops back to block "
        f"{lseg} (driver data unavailable)",
    ]
    assert [dos_type for dos_type, _ in damaged] == [DOS3, DOS1]


@pytest.mark.timeout(10)
def test_inspect_reports_the_loop(tmp_path, capsys):
    print_inspect_report(_fs_list_loop(tmp_path), None, False)
    out = capsys.readouterr().out
    assert "DH0" in out
    assert ("Filesystem list loops back to block 2 "
            "(remaining filesystem entries skipped)") in out


def test_parceiro_lseg_block_is_accepted(tmp_path):
    """A Parceiro checksum without a loop still gives the driver, with
    the first LSEG block's data cut to its size field."""
    image = _rdb(tmp_path)
    _, clean, _, _ = _open(image)
    data = bytes(clean[0].get_data())
    _edit(image, LSEG, {1: 64}, summed=64)
    parts, fs, warnings, damaged = _open(image)
    assert parts == ["DH0"]
    assert [entry.fshd.dos_type for entry in fs] == [DOS3]
    assert bytes(fs[0].get_data()) == data[:(64 - 5) * 4] + data[123 * 4:]
    assert warnings == [
        "Filesystem #0 (DOS3/0x444f5303): Parceiro checksum in 1 LSEG "
        "block(s) (accepted)",
    ]
    assert damaged == []
