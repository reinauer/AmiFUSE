"""rdb-inspect --json when no RDB opens: an error and exit status 1, not []."""

import pytest

from amifuse import rdb_inspect
from tests.unit.test_mbr_rdb_block_size import _make_mbr, _make_rdb


def _no_rdb_opens(args, capsys):
    """Run rdb-inspect, which must fail. Returns its error message; a script
    reading stdout gets nothing it can take for a result."""
    with pytest.raises(SystemExit) as caught:
        rdb_inspect.main(args)
    assert capsys.readouterr().out == ""
    assert isinstance(caught.value.code, str)  # Printed; exit status 1.
    return caught.value.code


@pytest.mark.parametrize("name, content", [
    ("notadisk.img", b"not a disk image\n"),
    ("empty.hdf", bytes(1024 * 1024)),
    ("missing.hdf", None),
])
def test_image_without_rdb_is_the_same_error_as_in_text_mode(
        tmp_path, capsys, name, content):
    image = tmp_path / name
    if content is not None:
        image.write_bytes(content)
    error = _no_rdb_opens(["--json", str(image)], capsys)
    assert error.startswith("Error: ")
    assert error == _no_rdb_opens([str(image)], capsys)


def test_every_failing_rdb_of_a_disk_is_named(tmp_path, capsys):
    # A 1024-byte-block RDB at an LBA that is not a multiple of two, then an
    # entry with no RDB at all.
    image = _make_mbr(tmp_path, [(63, _make_rdb(tmp_path, 1024, "DH0")),
                                 (385, bytes(160 * 512))])
    assert _no_rdb_opens(["--json", str(image)], capsys).splitlines() == [
        f"Error: MBR with 2 Amiga partition(s) found, but none contain a "
        f"valid RDB: {image}",
        f"MBR partition [0]: MBR partition 0 does not contain a valid RDB: "
        f"{image}; partition 0 at LBA 63 is not aligned to the RDB's "
        f"1024-byte blocks",
        f"MBR partition [1]: MBR partition 1 does not contain a valid RDB: "
        f"{image}",
    ]


def _text_error(image, capsys):
    with pytest.raises(SystemExit) as caught:
        rdb_inspect.main([str(image)])
    capsys.readouterr()
    return str(caught.value)


def _layout(tmp_path, layout):
    from tests.unit.test_disk_rdb_beside_entries import _broken_disk_rdb

    empty = bytes(160 * 512)
    return {
        "one empty entry": lambda: _make_mbr(tmp_path, [(64, empty)]),
        "two empty entries": lambda: _make_mbr(
            tmp_path, [(64, empty), (400, empty)]),
        "misaligned and empty entries": lambda: _make_mbr(
            tmp_path, [(63, _make_rdb(tmp_path, 1024, "DH0")), (385, empty)]),
        "own RDB beside two empty entries": lambda: _broken_disk_rdb(
            tmp_path, (None, None)),
        "own RDB beside one empty entry": lambda: _broken_disk_rdb(
            tmp_path, (None,)),
    }[layout]()


@pytest.mark.parametrize("layout", [
    "one empty entry",
    "two empty entries",
    "misaligned and empty entries",
    "own RDB beside two empty entries",
    "own RDB beside one empty entry",
])
def test_first_line_is_the_error_text_mode_ends_with(tmp_path, capsys, layout):
    image = _layout(tmp_path, layout)

    first = _no_rdb_opens(["--json", str(image)], capsys).splitlines()[0]

    assert first == _text_error(image, capsys)
