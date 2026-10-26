"""A Parceiro disk with two partitions, whose MBR also lists two 0x76 entries.

open_rdisk finds the disk's own RDB whichever 0x76 entry is selected, so the
loops over those entries meet that one RDB once per entry. Each partition is
listed once, and the RDB is described once.
"""

import json
import struct
from contextlib import ExitStack

from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk

from amifuse.rdb_inspect import list_partitions, open_rdisk
from tests.unit.test_mbr_rdb_block_size import _make_mbr, _with_partition_list


def _with_next(block, next_blk):
    """A PART block pointing at next_blk, with its checksum fixed."""
    data = bytearray(block)
    struct.pack_into(">I", data, 16, next_blk)  # pb_Next
    summed = struct.unpack_from(">I", data, 4)[0]  # pb_SummedLongs
    struct.pack_into(">I", data, 8, 0)  # pb_ChkSum
    total = sum(struct.unpack_from(f">{summed}I", data, 0)) & 0xFFFFFFFF
    struct.pack_into(">I", data, 8, -total & 0xFFFFFFFF)
    return bytes(data)


def _parceiro(tmp_path):
    """The disk's own RDB at block 2, its PART blocks DH0 and DH1 at blocks 3
    and 4, beside an MBR listing two empty 0x76 entries."""
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

    image = _make_mbr(tmp_path, [(200, bytes(160 * 512)),
                                 (400, bytes(160 * 512))])
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
