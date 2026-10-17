"""inspect on an image with several 0x76 partitions, some of them unreadable."""

import pytest

from amifuse.rdb_inspect import open_rdisk, print_inspect_report
from tests.unit.test_mbr_rdb_block_size import _make_mbr, _make_rdb

# 0x76 partitions whose RDB cannot be opened, each ending before LBA 385.
UNREADABLE = {
    # A 1024-byte-block RDB at an LBA that is not a multiple of two.
    "misaligned": lambda tmp_path: (63, _make_rdb(tmp_path, 1024, "DH0")),
    # No RDB at all.
    "empty": lambda tmp_path: (64, bytes(160 * 512)),
}


@pytest.mark.parametrize("first", sorted(UNREADABLE))
def test_later_rdb_is_listed_when_first_cannot_be_opened(tmp_path, capsys,
                                                         first):
    """An unreadable first RDB is reported inline and the next one is listed.

    The MBR table header used to come from opening the first 0x76
    partition, so its failure ended the command before any RDB was shown.
    """
    image = _make_mbr(tmp_path, [UNREADABLE[first](tmp_path),
                                 (385, _make_rdb(tmp_path, 512, "DH1"))])
    print_inspect_report(image, None, False)
    out = capsys.readouterr().out
    assert out.startswith("MBR Partition Table detected (Emu68-style)\n")
    assert "\nMBR partition [0]: Error: MBR partition 0 does not contain" in out
    assert "=== Amiga RDB in MBR partition [1] (offset: 385 sectors) ===" in out
    assert "DH1" in out


def test_no_readable_rdb_exits_with_error(tmp_path, capsys):
    """When no RDB opens, each failure is listed and the command fails.

    Reporting failures inline must not turn an image with nothing to show
    into a successful exit.
    """
    image = _make_mbr(tmp_path, [UNREADABLE["misaligned"](tmp_path),
                                 (385, bytes(160 * 512))])
    with pytest.raises(SystemExit) as exc_info:
        print_inspect_report(image, None, False)
    assert str(exc_info.value) == (
        f"Error: MBR with 2 Amiga partitions found, but none contain a "
        f"valid RDB: {image}"
    )
    out = capsys.readouterr().out
    assert "MBR partition [0]: Error:" in out
    assert "not aligned to the RDB's 1024-byte blocks" in out
    assert "MBR partition [1]: Error:" in out


@pytest.mark.parametrize("mbr_partition_index,expected", [
    (None, "MBR with 2 Amiga partition(s) found, but none contain a valid RDB"),
    # The second 0x76 partition is MBR slot 2, behind a FAT32 entry.
    (1, "MBR partition 2 does not contain a valid RDB"),
])
def test_open_error_counts_the_partitions_it_tried(tmp_path,
                                                   mbr_partition_index,
                                                   expected):
    """The error names the selected partition instead of a narrowed count.

    Selecting one 0x76 partition used to report "1 Amiga partition(s)"
    for an image with more than one.
    """
    image = _make_mbr(tmp_path, [(64, bytes(160 * 512))] * 3)
    data = bytearray(image.read_bytes())
    data[446 + 4] = 0x0C  # slot 0 becomes W95 FAT32 (LBA)
    image.write_bytes(bytes(data))
    with pytest.raises(OSError) as exc_info:
        open_rdisk(image, mbr_partition_index=mbr_partition_index)
    assert str(exc_info.value) == f"{expected}: {image}"
