"""`ls --recursive` ends on an ISO whose directory leads back up (#364).

ISO 9660 keeps a directory's place in its record and nothing else, so a
damaged record can name a directory above it. ODFileSystem follows it, with
no link and no special name to warn the walk. The images are built here, as
the damage is only a few bytes. No host FUSE driver is needed.
"""

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from amifuse import fuse_fs, rdb_inspect
from tests.fixtures.paths import DRIVERS_DIR

SECTOR = 2048
ROOT, A, B, C = 20, 21, 22, 23

pytestmark = pytest.mark.timeout(60)


@pytest.fixture(autouse=True)
def needs_cpu():
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")


@pytest.fixture
def driver():
    path = DRIVERS_DIR / "ODFileSystem" if DRIVERS_DIR else None
    if path is None or not path.exists():
        pytest.skip("ODFileSystem not found")
    return path


def _both(fmt, value):
    return struct.pack("<" + fmt, value) + struct.pack(">" + fmt, value)


def _record(name, lba, is_dir=True, size=SECTOR):
    body = (_both("I", lba) + _both("I", size)
            + bytes([126, 1, 1, 0, 0, 0, 0])  # 2026-01-01
            + bytes([2 if is_dir else 0, 0, 0]) + _both("H", 1)
            + bytes([len(name)]) + name)
    body += bytes(len(body) % 2 == 0)
    return bytes([len(body) + 2, 0]) + body


def _dir(lba, parent, *records):
    return _record(b"\0", lba) + _record(b"\1", parent) + b"".join(records)


def _path_table(fmt, entries):
    table = b""
    for name, lba, parent in entries:
        table += bytes([len(name), 0]) + struct.pack(fmt + "IH", lba, parent) + name
        table += bytes(len(name) % 2)
    return table


def _iso(path, b_lba, chain=False):
    """/A/B and /A/C, with B's record in A naming sector b_lba."""
    sectors = {
        ROOT: _dir(ROOT, ROOT, _record(b"A", A)),
        A: _dir(A, ROOT, _record(b"B", b_lba), _record(b"C", C),
                _record(b"F.TXT;1", 25, is_dir=False, size=5)),
        B: _dir(B, A),
        C: _dir(C, A),
        25: b"hello",
    }
    entries = [(b"\0", ROOT, 1), (b"A", A, 1), (b"B", B, 2), (b"C", C, 2)]
    if chain:
        # Both ancestors list A with the same key and timestamp on
        # ODFileSystem; the third listing ends in a file instead of a loop.
        sectors[A] = _dir(A, ROOT, _record(b"A", B))
        sectors[B] = _dir(B, A, _record(b"F.TXT;1", 25, is_dir=False, size=5))
        entries = [(b"\0", ROOT, 1), (b"A", A, 1), (b"A", B, 2)]
    little, big = _path_table("<", entries), _path_table(">", entries)
    pvd = bytearray(b" " * SECTOR)
    pvd[:7] = b"\x01CD001\x01"
    pvd[40:44] = b"LOOP"
    pvd[80:88] = _both("I", 26)
    pvd[120:124] = _both("H", 1)
    pvd[124:128] = _both("H", 1)
    pvd[128:132] = _both("H", SECTOR)
    pvd[132:140] = _both("I", len(little))
    pvd[140:144] = struct.pack("<I", 18)
    pvd[148:152] = struct.pack(">I", 19)
    pvd[156:190] = _record(b"\0", ROOT)
    pvd[881] = 1
    sectors[16] = bytes(pvd)
    sectors[17] = b"\xffCD001\x01"
    sectors[18] = little
    sectors[19] = big
    data = bytearray(26 * SECTOR)
    for lba, content in sectors.items():
        data[lba * SECTOR:lba * SECTOR + len(content)] = content
    path.write_bytes(data)
    return path


def _ls(image, driver, capfd):
    args = SimpleNamespace(image=image, json=False, debug=False, partition=None,
                           block_size=None, driver=driver)
    bridge, temp_driver = fuse_fs._create_bridge_from_args(args, "ls")
    try:
        entries = fuse_fs._ls_recursive(bridge, "/")
    finally:
        fuse_fs._cleanup_bridge(bridge, temp_driver)
    return [ent["path"] for ent in entries], capfd.readouterr().err


def test_a_sound_image_is_walked_whole(tmp_path, driver, capfd):
    paths, err = _ls(_iso(tmp_path / "ok.iso", B), driver, capfd)

    assert sorted(paths) == ["/A", "/A/B", "/A/C", "/A/F.TXT"]
    assert "leads back" not in err


def test_a_record_leading_back_up_ends_the_walk(tmp_path, driver, capfd):
    paths, err = _ls(_iso(tmp_path / "loop.iso", A), driver, capfd)

    assert sorted(paths) == ["/A", "/A/B", "/A/B/B", "/A/B/C", "/A/B/F.TXT",
                             "/A/C", "/A/F.TXT"]
    assert "contents of /A/B/B not listed (it leads back" in err


def test_same_name_chain_with_shared_keys_is_walked_whole(tmp_path, driver, capfd):
    paths, err = _ls(_iso(tmp_path / "chain.iso", B, chain=True), driver, capfd)

    assert paths == ["/A", "/A/A", "/A/A/F.TXT"]
    assert "leads back" not in err
