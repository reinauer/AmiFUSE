"""A Parceiro disk with two partitions, whose MBR also lists two 0x76 entries.

The disk's own RDB, beside the MBR, comes first; each 0x76 entry may hold an
RDB of its own. Every RDB is listed and described once, and an entry with no
RDB adds nothing.
"""

import json
import struct
from contextlib import ExitStack

from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk

from amifuse.driver_runtime import BlockDeviceBackend
from amifuse.rdb_inspect import (find_partition_mbr_index, list_partitions,
                                 open_rdisk)
from tests.unit.test_mbr_rdb_block_size import (_make_mbr, _make_rdb,
                                                _with_partition_list)


def _with_next(block, next_blk):
    """A PART block pointing at next_blk, with its checksum fixed."""
    data = bytearray(block)
    struct.pack_into(">I", data, 16, next_blk)  # pb_Next
    summed = struct.unpack_from(">I", data, 4)[0]  # pb_SummedLongs
    struct.pack_into(">I", data, 8, 0)  # pb_ChkSum
    total = sum(struct.unpack_from(f">{summed}I", data, 0)) & 0xFFFFFFFF
    struct.pack_into(">I", data, 8, -total & 0xFFFFFFFF)
    return bytes(data)


def _parceiro(tmp_path, entries=(None, None)):
    """The disk's own RDB at block 2, its PART blocks DH0 and DH1 at blocks 3
    and 4, beside an MBR listing two 0x76 entries. Each entry holds an RDB
    with a partition of the name given, or is empty for None."""
    rdb_image = tmp_path / "rdb.hdf"
    raw = RawBlockDevice(str(rdb_image), read_only=False, block_bytes=512)
    with ExitStack() as cleanup:
        raw.create(160)
        cleanup.callback(raw.close)
        rdisk = RDisk(raw)
        cleanup.callback(rdisk.close)
        rdisk.create(DiskGeometry(10, 1, 16), rdb_cyls=1)
        rdisk.add_partition(FSString("DH0"), (1, 4))
        rdisk.add_partition(FSString("DH1"), (5, 9))
        raw.flush()
    rdb = rdb_image.read_bytes()  # RDSK at block 0, PART blocks at 1 and 2

    image = _make_mbr(tmp_path, [
        (lba, bytes(160 * 512) if name is None else _make_rdb(tmp_path, 512, name))
        for lba, name in zip((200, 400), entries)])
    data = bytearray(image.read_bytes())
    data[2 * 512:3 * 512] = _with_partition_list(rdb, 512, 3)[:512]
    data[3 * 512:4 * 512] = _with_next(rdb[512:1024], 4)
    data[4 * 512:5 * 512] = rdb[1024:1536]
    image.write_bytes(bytes(data))

    blkdev, rdisk, mbr_ctx = open_rdisk(image)
    try:
        assert mbr_ctx.scheme == "parceiro"
        assert [str(p.get_drive_name()) for p in rdisk.parts] == ["DH0", "DH1"]
    finally:
        rdisk.close()
        blkdev.close()
    return image


def test_list_partitions_lists_each_partition_once(tmp_path):
    listed = list_partitions(_parceiro(tmp_path))
    assert [p.name for p in listed.partitions] == ["DH0", "DH1"]
    assert listed.fallback_reason is None


def test_launcher_mounts_every_partition(tmp_path):
    from amifuse.launcher import _enumerate_mount_units

    units, fallback_reason = _enumerate_mount_units(str(_parceiro(tmp_path)))
    assert [(u.name, u.label) for u in units] == [("DH0", "DH0"),
                                                  ("DH1", "DH1")]
    assert fallback_reason is None


def test_inspect_json_describes_the_rdb_once(user_driver_dir, tmp_path,
                                             capsys):
    from amifuse.fuse_fs import main

    main(["inspect", "--json", str(_parceiro(tmp_path))])
    env = json.loads(capsys.readouterr().out)
    assert "rdbs" not in env
    assert env["mbr"]["scheme"] == "parceiro"
    assert [p["name"] for p in env["partitions"]] == ["DH0", "DH1"]
    for part in env["partitions"]:
        assert part["driver"] is not None
        assert "driver_note" not in part


def test_inspect_text_gives_every_partition_a_driver(user_driver_dir,
                                                     tmp_path, capsys):
    from amifuse.fuse_fs import main

    main(["inspect", str(_parceiro(tmp_path))])
    out = capsys.readouterr().out
    section = out.split("Filesystem driver per partition (without --driver):\n")[1]
    assert [line.split(":")[0] for line in section.splitlines()] == [
        "  DH0", "  DH1"]


def test_rdb_inspect_json_describes_the_rdb_once(tmp_path, capsys):
    from amifuse.rdb_inspect import main

    main(["--json", str(_parceiro(tmp_path))])
    desc = json.loads(capsys.readouterr().out)
    assert desc["mbr"]["scheme"] == "parceiro"
    assert [p["name"] for p in desc["partitions"]] == ["DH0", "DH1"]


def _names(image, **kwargs):
    blkdev, rdisk, mbr_ctx = open_rdisk(image, **kwargs)
    try:
        part = mbr_ctx.mbr_partition
        return ([str(p.get_drive_name()) for p in rdisk.parts],
                None if part is None else part.index)
    finally:
        rdisk.close()
        blkdev.close()


def test_entry_opens_its_own_rdb(tmp_path):
    image = _parceiro(tmp_path, ("PI0", "PI1"))
    assert _names(image) == (["DH0", "DH1"], None)
    assert _names(image, mbr_partition_index=0) == (["PI0"], 0)
    assert _names(image, mbr_partition_index=1) == (["PI1"], 1)


def test_list_partitions_lists_every_rdb(tmp_path):
    listed = list_partitions(_parceiro(tmp_path, ("PI0", "PI1")))
    assert [p.name for p in listed.partitions] == ["DH0", "DH1", "PI0", "PI1"]
    assert listed.fallback_reason is None


def test_name_lookup_finds_each_rdb(tmp_path):
    image = _parceiro(tmp_path, ("PI0", "PI1"))
    # None opens the disk's own RDB, as open_rdisk does by default.
    assert [find_partition_mbr_index(image, None, name)
            for name in ("DH1", "PI0", "PI1", "XX")] == [None, 0, 1, None]


def test_mount_by_name_opens_the_rdb_holding_it(tmp_path):
    image = _parceiro(tmp_path, ("PI0", "PI1"))
    for name, index in (("DH1", None), ("PI1", 1)):
        backend = BlockDeviceBackend(image, partition_scope=True,
                                     partition=name)
        backend.open()
        try:
            assert backend.mbr_partition_index == index
            assert backend.rdb.find_partition_by_string(name) is not None
        finally:
            backend.close()


def test_inspect_json_describes_every_rdb(user_driver_dir, tmp_path, capsys):
    from amifuse.fuse_fs import main

    main(["inspect", "--json", str(_parceiro(tmp_path, (None, "PI1")))])
    rdbs = json.loads(capsys.readouterr().out)["rdbs"]
    assert [(r["mbr"]["scheme"], r["mbr"].get("partition_index"),
             [p["name"] for p in r["partitions"]]) for r in rdbs] == [
        ("parceiro", None, ["DH0", "DH1"]), ("emu68", 1, ["PI1"])]
    for rdb in rdbs:
        for part in rdb["partitions"]:
            assert part["driver"] is not None


def test_inspect_text_reports_every_rdb(user_driver_dir, tmp_path, capsys):
    from amifuse.fuse_fs import main

    main(["inspect", str(_parceiro(tmp_path, (None, "PI1")))])
    out = capsys.readouterr().out
    assert out.startswith("MBR + RDB coexistence detected (Parceiro-style)")
    assert "=== Amiga RDB in MBR partition [1] (offset: 400 sectors) ===" in out
    assert "MBR partition [0]" not in out
    section = out.split("Filesystem driver per partition (without --driver):\n")[1]
    assert [line.split(":")[0] for line in section.splitlines()] == [
        "  DH0", "  DH1", "  PI1"]


def test_clash_note_marks_the_copy_mount_does_not_use(user_driver_dir,
                                                      tmp_path, capsys):
    from amifuse.fuse_fs import main

    image = _parceiro(tmp_path, ("PI0", "DH0"))
    listed = list_partitions(image)
    assert [p.name for p in listed.partitions] == ["DH0"]
    assert "colliding partition names ('DH0')" in listed.fallback_reason

    main(["inspect", "--json", str(image)])
    rdbs = json.loads(capsys.readouterr().out)["rdbs"]
    disk, entry = rdbs[0]["partitions"][0], rdbs[2]["partitions"][0]
    # mount --partition DH0 opens the RDB this lookup selects; None is the
    # disk's own.
    selected = find_partition_mbr_index(image, None, "DH0")
    assert selected in (None, 1)
    used, other = (disk, entry) if selected is None else (entry, disk)
    assert used["driver"] is not None
    assert other["driver"] is None
    assert other["driver_note"].startswith("another RDB on this disk")


def test_entries_sharing_an_rdb_list_it_once(tmp_path):
    image = _make_mbr(tmp_path, [(200, _make_rdb(tmp_path, 512, "PI0")),
                                 (200, _make_rdb(tmp_path, 512, "PI0"))])
    listed = list_partitions(image)
    assert [p.name for p in listed.partitions] == ["PI0"]
    assert listed.fallback_reason is None
