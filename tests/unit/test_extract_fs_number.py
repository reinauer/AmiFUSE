"""--extract-fs N takes the entry the report shows as FS #N.

The lenient parse numbers entries by their place in the RDB's list, dropped
ones included, and the report prints that number; --extract-fs used to take
the N-th entry kept.
"""

from pathlib import Path

import pytest

from amifuse import rdb_inspect
from amifuse.rdb_inspect import extract_fs, open_rdisk
from tests.unit.test_driver_resolver import _flip_byte, _rdb

DOS1, DOS3 = 0x444f5301, 0x444f5303


def _first_entry_damaged(tmp_path):
    """FS #0 (DOS1) is dropped for a bad LSEG block; FS #1 (DOS3) is kept."""
    image = _rdb(tmp_path, "DOS3", ("DOS1", "DOS3"))
    _flip_byte(image, b"LSEG")
    return image


def _kept_driver(image, dos_type):
    blkdev, rdisk, _ = open_rdisk(image)
    try:
        return next(fs.get_data() for fs in rdisk.fs
                    if fs.fshd.dos_type == dos_type)
    finally:
        rdisk.close()
        blkdev.close()


def _refused(args, capsys):
    with pytest.raises(SystemExit) as caught:
        rdb_inspect.main(args)
    capsys.readouterr()
    return caught.value.code


def test_extract_fs_takes_the_number_the_report_shows(tmp_path, capsys):
    image = _first_entry_damaged(tmp_path)
    out = tmp_path / "fs1.bin"
    rdb_inspect.main(["--extract-fs", "1", "--out", str(out), str(image)])
    printed = capsys.readouterr().out
    assert "  FS #1: DOS3/0x444f5303 " in printed
    assert f"Wrote filesystem #1 ({hex(DOS3)}) to {out}\n" in printed
    assert out.read_bytes() == _kept_driver(image, DOS3)


def test_default_output_name_has_the_reports_number(
        tmp_path, capsys, monkeypatch):
    image = _first_entry_damaged(tmp_path)
    monkeypatch.chdir(tmp_path)
    rdb_inspect.main(["--extract-fs", "1", str(image)])
    written, = Path().glob("fs1_*.bin")
    assert written.read_bytes() == _kept_driver(image, DOS3)


def test_damaged_entry_is_refused_and_named(tmp_path, capsys):
    image = _first_entry_damaged(tmp_path)
    out = tmp_path / "fs0.bin"
    assert _refused(["--extract-fs", "0", "--out", str(out), str(image)],
                    capsys) == (
        "Cannot extract filesystem #0: Filesystem #0 (DOS1/0x444f5301): "
        "corrupt data block in LSEG chain (driver data unavailable)")
    assert not out.exists()


@pytest.mark.parametrize("num", [1, 2, 3])
def test_entries_a_corrupt_header_hides_are_not_said_to_be_missing(
        tmp_path, capsys, num):
    # The list ends at the corrupt header of FS #1, so FS #2 may exist.
    image = _rdb(tmp_path, "DOS3", ("DOS3", "DOS1", "DOS7"))
    block = _flip_byte(image, b"FSHD", nth=1)
    assert _refused(["--extract-fs", str(num), str(image)], capsys) == (
        f"Cannot extract filesystem #{num}: Corrupt filesystem header at "
        f"block {block} (remaining filesystem entries skipped)")


def test_negative_number_is_not_an_entry(tmp_path, capsys):
    # get_filesystem() indexed the list, so -1 was the last entry.
    image = _rdb(tmp_path, "DOS3", "DOS3")
    assert _refused(["--extract-fs", "-1", "--out", str(tmp_path / "x"),
                     str(image)], capsys) == "No filesystem #-1 in RDB."


def test_extract_fs_function_takes_the_reports_number(tmp_path):
    image = _first_entry_damaged(tmp_path)
    blkdev, rdisk, _ = open_rdisk(image)
    try:
        out = extract_fs(rdisk, 1, tmp_path / "fs1.bin")
    finally:
        rdisk.close()
        blkdev.close()
    assert out.read_bytes() == _kept_driver(image, DOS3)
