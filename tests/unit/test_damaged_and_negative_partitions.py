"""--partition on a damaged drive name (#192) and a negative number (#193).

amitools reads a drive name whose length byte is above 31 as None. Such a
partition has no name: it is not listed under the name "None", and the
launcher does not mount it by one. A negative number selects no partition,
as one past the end does, instead of counting from the end.
"""

import pytest

from amitools.fs.DosType import DOS3, DOS7

from amifuse import rdb_inspect
from amifuse.driver_runtime import BlockDeviceBackend
from amifuse.fuse_fs import _partition_driver, get_partition_info
from amifuse.launcher import _enumerate_mount_units
from amifuse.rdb_inspect import (PartInfo, PartitionList, find_partition,
                                 list_partitions)
from tests.unit.test_partition_name_before_number import (_damage_first_name,
                                                          _make_rdb_parts)


def _damaged(tmp_path, parts):
    """A plain RDB of parts whose first drive name reads as None."""
    image = tmp_path / "disk.hdf"
    image.write_bytes(_damage_first_name(
        tmp_path, _make_rdb_parts(tmp_path, 512, parts)))
    return image


def test_damaged_name_is_listed_without_a_name(tmp_path):
    image = _damaged(tmp_path, [("DH0", DOS3), ("DH1", DOS7)])
    assert [(p.index, p.name) for p in list_partitions(image).partitions] == [
        (0, None), (1, "DH1")]
    # #176 made the lookup skip the damaged name: DH1 opens by name and by
    # number.
    for selector in ("DH1", "dh1", "1"):
        assert get_partition_info(image, None, selector) == {
            "name": "DH1", "dostype": DOS7}
    # The damaged partition still opens by number, named by it.
    for selector in ("0", None):
        assert get_partition_info(image, None, selector) == {
            "name": "#0", "dostype": DOS3}
    assert find_partition(image, None, "None") is None


def test_launcher_skips_the_damaged_partition(tmp_path):
    image = _damaged(tmp_path, [("DH0", DOS3), ("DH1", DOS7)])
    units, fallback_reason = _enumerate_mount_units(str(image))
    # DH1 is mounted by name: without --partition the mount opens the
    # damaged partition 0.
    assert [(u.name, u.label) for u in units] == [("DH1", "DH1")]
    # The summary dialog tells of the skipped partition.
    assert fallback_reason == (
        "A partition was not mounted, as its drive name is damaged.")


def test_launcher_mounts_a_lone_damaged_partition_without_a_name(tmp_path):
    image = _damaged(tmp_path, [("DH0", DOS3)])
    units, _ = _enumerate_mount_units(str(image))
    assert [(u.name, u.label) for u in units] == [(None, "disk image")]


def test_launcher_with_every_name_damaged_mounts_partition_0(monkeypatch):
    # Not an empty fan-out, which would report no free drive letter.
    parts = [PartInfo(index=i, name=None, dostype=DOS3, dostype_str="DOS3")
             for i in range(3)]
    monkeypatch.setattr(rdb_inspect, "detect_adf", lambda img: None)
    monkeypatch.setattr(rdb_inspect, "detect_iso", lambda img: None)
    monkeypatch.setattr(rdb_inspect, "list_partitions",
                        lambda img: PartitionList(partitions=parts))
    units, fallback_reason = _enumerate_mount_units("disk.hdf")
    assert [(u.name, u.label) for u in units] == [(None, "disk image")]
    assert fallback_reason == (
        "2 partitions were not mounted, as their drive names are damaged.")


def test_inspect_names_no_driver_for_the_damaged_partition(
        tmp_path, user_driver_dir):
    image = _damaged(tmp_path, [("DH0", DOS3), ("DH1", DOS7)])
    # Without --partition the mount opens partition 0 of the disk's first
    # RDB, which need not be this one.
    assert _partition_driver(image, None, None) == (
        None, "damaged drive name: no --partition name selects it")
    info, _text = _partition_driver(image, None, "DH1")
    assert info is not None


@pytest.mark.parametrize("selector", ["-1", "-2", "-3"])
def test_negative_number_selects_no_partition(tmp_path, selector):
    image = tmp_path / "disk.hdf"
    image.write_bytes(_make_rdb_parts(tmp_path, 512,
                                      [("DH0", DOS3), ("DH1", DOS7)]))
    assert find_partition(image, None, selector) is None
    assert get_partition_info(image, None, selector) == {"name": "AmigaFS",
                                                         "dostype": None}
    backend = BlockDeviceBackend(image, partition_scope=True,
                                 partition=selector)
    with pytest.raises(ValueError) as caught:
        backend.open()
    assert str(caught.value) == f"Partition not found: {selector}"
