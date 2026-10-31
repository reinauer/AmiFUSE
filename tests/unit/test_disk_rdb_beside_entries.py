"""A Parceiro disk's own RDB and its 0x76 entries' own RDBs open apart.

The disk's own RDB, beside the MBR, failing to open does not stop the
entries' RDBs (#205). Beside the disk's own RDB, an entry whose RDB is found
but fails to open is reported, while an entry holding no RDB is left out
(#206).
"""

import json
import struct

import pytest

from amifuse.driver_runtime import BlockDeviceBackend
from amifuse.rdb_inspect import (find_partition_mbr_index, list_partitions,
                                 open_rdisk)
from tests.unit.test_mbr_rdb_block_size import _make_rdb
from tests.unit.test_parceiro_partitions import _disk_rdb


def _corrupt(image, block):
    """Flip a byte of a 512-byte block's drive name, so its checksum fails."""
    data = bytearray(image.read_bytes())
    data[block * 512 + 40] ^= 0xFF
    image.write_bytes(bytes(data))


def _broken_disk_rdb(tmp_path, entries=("PI0", "PI1")):
    """The disk's own RDB at block 2 with DH1's PART block (block 4) corrupt,
    beside two 0x76 entries at LBA 200 and 400 holding the RDBs named."""
    image = _disk_rdb(tmp_path, entries, 2)
    _corrupt(image, 4)
    return image


def _names(image, **kwargs):
    blkdev, rdisk, mbr_ctx = open_rdisk(image, **kwargs)
    try:
        return ([str(p.get_drive_name()) for p in rdisk.parts],
                mbr_ctx.mbr_partition.index)
    finally:
        rdisk.close()
        blkdev.close()


def _disk_error(image):
    return f"Failed to parse RDB at {image}: Corrupt partition block at block 4"


def test_entries_open_beside_a_disk_rdb_that_fails(tmp_path):
    from amifuse.rdb_inspect import DiskRDBError

    image = _broken_disk_rdb(tmp_path)
    with pytest.raises(DiskRDBError) as caught:
        open_rdisk(image)
    assert str(caught.value) == _disk_error(image)
    assert _names(image, mbr_partition_index=0) == (["PI0"], 0)
    assert _names(image, mbr_partition_index=1) == (["PI1"], 1)

    listed = list_partitions(image)
    assert [p.name for p in listed.partitions] == ["PI0", "PI1"]
    # The launcher names the RDB whose partitions are missing.
    assert listed.fallback_reason == f"RDB beside the MBR: {_disk_error(image)}"
    assert find_partition_mbr_index(image, None, "PI1") == 1


def test_inspect_reports_the_disk_rdb_once(user_driver_dir, tmp_path, capsys):
    from amifuse.fuse_fs import main

    image = _broken_disk_rdb(tmp_path)
    main(["inspect", str(image)])
    out = capsys.readouterr().out
    assert out.startswith("MBR + RDB coexistence detected (Parceiro-style)\n")
    assert out.count("Corrupt partition block at block 4") == 1
    assert f"\nRDB beside the MBR: Error: {_disk_error(image)}\n" in out
    assert "=== Amiga RDB in MBR partition [0] (offset: 200 sectors) ===" in out
    assert "=== Amiga RDB in MBR partition [1] (offset: 400 sectors) ===" in out
    section = out.split("Filesystem driver per partition (without --driver):\n")[1]
    assert [line.split(":")[0] for line in section.splitlines()] == [
        "  PI0", "  PI1"]

    main(["inspect", "--json", str(image)])
    env = json.loads(capsys.readouterr().out)
    assert env["status"] == "ok"
    assert [(r["mbr"]["partition_index"], [p["name"] for p in r["partitions"]])
            for r in env["rdbs"]] == [(0, ["PI0"]), (1, ["PI1"])]
    assert env["notes"] == [f"RDB beside the MBR: {_disk_error(image)}"]


def test_disk_rdb_that_fails_alone_ends_inspect_with_its_error(
        user_driver_dir, tmp_path, capsys):
    from amifuse.fuse_fs import main

    for entries in ((None, None), (None,)):
        image = _broken_disk_rdb(tmp_path, entries)
        with pytest.raises(SystemExit) as caught:
            main(["inspect", str(image)])
        assert str(caught.value) == f"Error: No RDB on this disk opens: {image}"
        out = capsys.readouterr().out
        assert out.startswith(
            "MBR + RDB coexistence detected (Parceiro-style)\n")
        assert out.count(f"RDB beside the MBR: Error: {_disk_error(image)}") == 1
        assert "MBR partition [" not in out

        with pytest.raises(OSError) as caught:
            list_partitions(image)
        assert str(caught.value) == _disk_error(image)

        with pytest.raises(SystemExit):
            main(["inspect", "--json", str(image)])
        env = json.loads(capsys.readouterr().out)
        assert env["error"]["code"] == "OPEN_FAILED"
        assert env["error"]["message"] == _disk_error(image)


def test_single_entry_opens_beside_a_disk_rdb_that_fails(user_driver_dir,
                                                         tmp_path, capsys):
    from amifuse.fuse_fs import main

    image = _broken_disk_rdb(tmp_path, ("PI0",))  # one 0x76 entry
    assert [p.name for p in list_partitions(image).partitions] == ["PI0"]
    assert find_partition_mbr_index(image, None, "PI0") == 0

    main(["inspect", str(image)])
    out = capsys.readouterr().out
    assert out.startswith("MBR + RDB coexistence detected (Parceiro-style)\n")
    assert f"\nRDB beside the MBR: Error: {_disk_error(image)}\n" in out
    assert "=== Amiga RDB in MBR partition [0] (offset: 200 sectors) ===" in out

    main(["inspect", "--json", str(image)])
    env = json.loads(capsys.readouterr().out)
    assert env["mbr"]["partition_index"] == 0
    assert [p["name"] for p in env["partitions"]] == ["PI0"]
    assert env["notes"] == [f"RDB beside the MBR: {_disk_error(image)}"]


def test_entry_rdb_in_forced_block_size_mounts_beside_the_disk_rdb(tmp_path):
    # The disk's own RDB and PI0's use 512-byte blocks, PI1's 1024-byte
    # blocks, spanning 320 sectors.
    image = _disk_rdb(tmp_path, ("PI0", None), 2)
    data = bytearray(image.read_bytes())
    rdb = _make_rdb(tmp_path, 1024, "PI1")
    data[400 * 512:] = rdb
    struct.pack_into("<I", data, 446 + 16 + 12, len(rdb) // 512)
    image.write_bytes(bytes(data))

    assert find_partition_mbr_index(image, None, "PI1") == 1
    assert find_partition_mbr_index(image, 1024, "PI1") == 1
    assert _names(image, block_size=1024, mbr_partition_index=1) == (["PI1"], 1)
    backend = BlockDeviceBackend(image, 1024, partition_scope=True,
                                 partition="PI1")
    backend.open()
    try:
        assert backend.mbr_partition_index == 1
        assert backend.rdb.find_partition_by_string("PI1") is not None
    finally:
        backend.close()


def test_entry_rdb_that_fails_is_reported_beside_the_disk_rdb(
        user_driver_dir, tmp_path, capsys):
    from amifuse.fuse_fs import main

    # Entry 0 holds no RDB; PI1's PART block, block 1 of entry 1, is corrupt.
    image = _disk_rdb(tmp_path, (None, "PI1"), 2)
    _corrupt(image, 401)
    error = (f"MBR partition 1 does not contain a valid RDB: {image}; "
             f"partition 1 at LBA 400: Corrupt partition block at block 1")
    from amifuse.rdb_inspect import NoEntryRDBError

    with pytest.raises(OSError) as caught:
        open_rdisk(image, mbr_partition_index=1)
    assert str(caught.value) == error
    assert not isinstance(caught.value, NoEntryRDBError)
    with pytest.raises(NoEntryRDBError):
        open_rdisk(image, mbr_partition_index=0)

    main(["inspect", str(image)])
    out = capsys.readouterr().out
    assert f"\nMBR partition [1]: Error: {error}\n" in out
    assert "MBR partition [0]" not in out
    section = out.split("Filesystem driver per partition (without --driver):\n")[1]
    assert [line.split(":")[0] for line in section.splitlines()] == [
        "  DH0", "  DH1"]

    main(["inspect", "--json", str(image)])
    env = json.loads(capsys.readouterr().out)
    assert env["status"] == "ok"
    assert [p["name"] for p in env["partitions"]] == ["DH0", "DH1"]
    assert env["notes"] == [f"MBR partition [1]: {error}"]


def test_single_entry_rdb_that_fails_is_reported_beside_the_disk_rdb(
        user_driver_dir, tmp_path, capsys):
    from amifuse.fuse_fs import main

    image = _disk_rdb(tmp_path, ("PI0",), 2)  # one 0x76 entry, at LBA 200
    _corrupt(image, 201)
    error = (f"MBR partition 0 does not contain a valid RDB: {image}; "
             f"partition 0 at LBA 200: Corrupt partition block at block 1")

    main(["inspect", str(image)])
    out = capsys.readouterr().out
    assert out.startswith("MBR + RDB coexistence detected (Parceiro-style)\n")
    assert f"\nMBR partition [0]: Error: {error}\n" in out

    main(["inspect", "--json", str(image)])
    env = json.loads(capsys.readouterr().out)
    assert [p["name"] for p in env["partitions"]] == ["DH0", "DH1"]
    assert env["notes"] == [f"MBR partition [0]: {error}"]
