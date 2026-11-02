"""A damaged drive name, which amitools reads as None, in messages and the
report: the partition is called by its number, not "None"."""

import pytest

from amitools.fs.DosType import DOS3, DOS7
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk

from amifuse.driver_resolver import DriverNotFound
from amifuse.driver_runtime import BlockDeviceBackend
from amifuse.fuse_fs import extract_embedded_driver
from amifuse.rdb_inspect import format_rdisk_report, open_rdisk
from tests.unit.test_driver_resolver import _flip_byte, _rdb
from tests.unit.test_partition_name_before_number import (_damage_first_name,
                                                          _make_rdb_parts)


def _damaged(tmp_path, parts=(("DH0", DOS3),), **dos_env):
    """An RDB with its first partition's drive name damaged, and dos_env
    set in that partition's environment."""
    image = tmp_path / "bad.hdf"
    image.write_bytes(_make_rdb_parts(tmp_path, 512, list(parts)))
    raw = RawBlockDevice(str(image), read_only=False)
    raw.open()
    disk = RDisk(raw)
    try:
        assert disk.open()
        part = disk.get_partition(0).part_blk
        for field, value in dos_env.items():
            setattr(part.dos_env, field, value)
        part.write()
    finally:
        disk.close()
        raw.close()
    image.write_bytes(_damage_first_name(tmp_path, image.read_bytes()))
    return image


def _report(image, **options):
    blkdev, rdisk, _ = open_rdisk(image)
    try:
        return format_rdisk_report(rdisk, **options)
    finally:
        rdisk.close()
        blkdev.close()


@pytest.mark.parametrize("dos_env, message", [
    ({"high_cyl": 0}, "Partition #0 has invalid geometry"),
    ({"low_cyl": 0}, "Partition #0 overlaps RDB metadata"),
])
def test_backend_refusal_names_the_partition_by_number(tmp_path, dos_env,
                                                       message):
    image = _damaged(tmp_path, **dos_env)
    backend = BlockDeviceBackend(image, partition_scope=True)
    with pytest.raises(ValueError) as caught:
        backend.open()
    assert str(caught.value) == message


def test_damaged_driver_error_names_the_partition_by_number(tmp_path):
    image = _rdb(tmp_path, "DOS3", "DOS3")
    _flip_byte(image, b"LSEG")
    image.write_bytes(_damage_first_name(tmp_path, image.read_bytes()))
    with pytest.raises(DriverNotFound) as caught:
        extract_embedded_driver(image, None, None)
    assert str(caught.value).startswith(
        "Partition #0 (DOS3) has a damaged filesystem driver in its RDB: ")


@pytest.mark.parametrize("full", [False, True])
def test_report_does_not_name_a_damaged_partition(tmp_path, full):
    # The second partition really is named None, and keeps that name.
    image = _damaged(tmp_path, [("DH0", DOS3), ("None", DOS7)])
    lines = _report(image, full=full)
    parts = [line.split()[1:3] for line in lines
             if line.startswith("Partition:")]
    assert parts == [["#0", "(damaged"], ["#1", "'None'"]]
    first = next(line for line in lines if line.startswith("Partition: #0"))
    # The columns after the name are amitools' own.
    assert first.startswith("Partition: #0 (damaged drive name)        1")
    assert "DOS3/0x444f5303" in first
