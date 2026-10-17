"""inspect on an image with several 0x76 partitions, some of them unreadable."""

import pytest

from amifuse import rdb_inspect
from amifuse.rdb_inspect import (format_rdisk_report, open_rdisk,
                                 print_inspect_report)
from tests.unit.test_mbr_rdb_block_size import (_make_mbr, _make_rdb,
                                                _with_partition_list)

# 0x76 partitions whose RDB cannot be opened, each ending before LBA 385.
UNREADABLE = {
    # A 1024-byte-block RDB at an LBA that is not a multiple of two.
    "misaligned": lambda tmp_path: (63, _make_rdb(tmp_path, 1024, "DH0")),
    # No RDB at all.
    "empty": lambda tmp_path: (64, bytes(160 * 512)),
}


# An unreadable first RDB is reported inline and the next one is listed.
# The MBR table header used to come from opening the first 0x76 partition,
# so its failure ended the command before any RDB was shown.
@pytest.mark.parametrize("first", sorted(UNREADABLE))
def test_later_rdb_is_listed_when_first_cannot_be_opened(tmp_path, capsys,
                                                         first):
    image = _make_mbr(tmp_path, [UNREADABLE[first](tmp_path),
                                 (385, _make_rdb(tmp_path, 512, "DH1"))])
    print_inspect_report(image, None, False)
    out = capsys.readouterr().out
    # The header describes the partition that opened, not the one that failed.
    assert out.startswith("MBR Partition Table detected (Emu68-style)\n"
                          "  Active partition: MBR slot 1\n"
                          "  Partition offset: 385 sectors (0 MB)\n"
                          "  Partition size: 160 sectors (0 MB)\n")
    assert "\nMBR partition [0]: Error: MBR partition 0 does not contain" in out
    assert "=== Amiga RDB in MBR partition [1] (offset: 385 sectors) ===" in out
    assert "DH1" in out


# When every RDB opens, the output is the same as before the change.
def test_output_is_unchanged_when_every_rdb_opens(tmp_path, capsys):
    image = _make_mbr(tmp_path, [(64, _make_rdb(tmp_path, 1024, "DH0")),
                                 (385, _make_rdb(tmp_path, 512, "DH1"))])
    reports = []
    for index in range(2):
        blkdev, rdisk, _ = open_rdisk(image, mbr_partition_index=index)
        try:
            reports.append("".join(f"{line}\n"
                                   for line in format_rdisk_report(rdisk)))
        finally:
            rdisk.close()
            blkdev.close()
    print_inspect_report(image, None, False)
    # The header and section lines as printed while the header still came
    # from opening the first 0x76 partition on its own.
    assert capsys.readouterr().out == (
        "MBR Partition Table detected (Emu68-style)\n"
        "  Active partition: MBR slot 0\n"
        "  Partition offset: 64 sectors (0 MB)\n"
        "  Partition size: 320 sectors (0 MB)\n"
        "\n"
        "  MBR partitions:\n"
        "    [0] Type: Amiga RDB, Start: 64, Size: 320 (0 MB)\n"
        "    [1] Type: Amiga RDB, Start: 385, Size: 160 (0 MB)\n"
        "\n"
        "=== Amiga RDB in MBR partition [0] (offset: 64 sectors) ===\n"
        "\n"
        f"{reports[0]}"
        "\n"
        "=== Amiga RDB in MBR partition [1] (offset: 385 sectors) ===\n"
        "\n"
        f"{reports[1]}"
    )


# When no RDB opens, each failure is listed and the command fails. Reporting
# failures inline must not turn an image with nothing to show into a
# successful exit, and with nothing open no partition is called active.
def test_no_readable_rdb_exits_with_error(tmp_path, capsys):
    image = _make_mbr(tmp_path, [UNREADABLE["misaligned"](tmp_path),
                                 (385, bytes(160 * 512))])
    with pytest.raises(SystemExit) as exc_info:
        print_inspect_report(image, None, False)
    assert str(exc_info.value) == (
        f"Error: MBR with 2 Amiga partition(s) found, but none contain a "
        f"valid RDB: {image}"
    )
    out = capsys.readouterr().out
    assert out.startswith("MBR Partition Table detected (Emu68-style)\n"
                          "\n"
                          "  MBR partitions:\n"
                          "    [0] Type: Amiga RDB, Start: 63, Size: 320 (0 MB)\n"
                          "    [1] Type: Amiga RDB, Start: 385, Size: 160 (0 MB)\n"
                          "\nMBR partition [0]: Error:")
    assert "Active partition" not in out
    assert "not aligned to the RDB's 1024-byte blocks" in out
    assert "MBR partition [1]: Error:" in out


# Parceiro-style: the disk's own RDB sits at block 2, next to an MBR that
# also lists two 0x76 partitions. open_rdisk finds that RDB first whichever
# partition is selected, and it carries no MBR partition of its own.
def test_disk_level_rdb_beside_two_0x76_entries_is_reported_once(tmp_path,
                                                                   capsys):
    rdb = _make_rdb(tmp_path, 512, "DH0")  # RDSK at block 0, PART at 1
    image = _make_mbr(tmp_path, [(200, bytes(160 * 512)),
                                 (400, bytes(160 * 512))])
    data = bytearray(image.read_bytes())
    data[2 * 512:3 * 512] = _with_partition_list(rdb, 512, 3)[:512]
    data[3 * 512:4 * 512] = rdb[512:1024]
    image.write_bytes(bytes(data))
    print_inspect_report(image, None, False)
    out = capsys.readouterr().out
    assert out.startswith("MBR + RDB coexistence detected (Parceiro-style)\n")
    assert out.count("'DH0'") == 1
    assert "=== Amiga RDB" not in out


# The MBR is parsed once for the header; a later read of block 0 that fails
# (another process holding a physical drive, say) only affects the RDB being
# opened at the time.
def test_failed_second_mbr_read_is_not_a_traceback(tmp_path, capsys,
                                                   monkeypatch):
    image = _make_mbr(tmp_path, [(64, _make_rdb(tmp_path, 512, "DH0")),
                                 (385, _make_rdb(tmp_path, 512, "DH1"))])
    detect_mbr = rdb_inspect.detect_mbr
    calls = []

    def second_read_fails(path):
        calls.append(path)
        return None if len(calls) == 2 else detect_mbr(path)

    monkeypatch.setattr(rdb_inspect, "detect_mbr", second_read_fails)
    print_inspect_report(image, None, False)
    out = capsys.readouterr().out
    assert len(calls) >= 2
    assert out.startswith("MBR Partition Table detected (Emu68-style)\n")
    assert "DH1" in out


# The error names the selected partition instead of a narrowed count:
# selecting one 0x76 partition used to report "1 Amiga partition(s)" for an
# image with more than one.
@pytest.mark.parametrize("mbr_partition_index,expected", [
    (None, "MBR with 2 Amiga partition(s) found, but none contain a valid RDB"),
    # The second 0x76 partition is MBR slot 2, behind a FAT32 entry.
    (1, "MBR partition 2 does not contain a valid RDB"),
])
def test_open_error_counts_the_partitions_it_tried(tmp_path,
                                                   mbr_partition_index,
                                                   expected):
    image = _make_mbr(tmp_path, [(64, bytes(160 * 512))] * 3)
    data = bytearray(image.read_bytes())
    data[446 + 4] = 0x0C  # slot 0 becomes W95 FAT32 (LBA)
    image.write_bytes(bytes(data))
    with pytest.raises(OSError) as exc_info:
        open_rdisk(image, mbr_partition_index=mbr_partition_index)
    assert str(exc_info.value) == f"{expected}: {image}"
