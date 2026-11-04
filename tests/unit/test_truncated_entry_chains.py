"""RDB chain links are bounded by bytes present in a truncated MBR image."""

import pytest

from amifuse.rdb_inspect import find_partition, open_rdisk
from tests.unit.test_mbr_rdb_block_size import _make_mbr
from tests.unit.test_rdb_chain_loops import FSHD, LSEG, _edit, _rdb


@pytest.mark.parametrize("chain", [1, FSHD, LSEG], ids=["PART", "FSHD", "LSEG"])
@pytest.mark.parametrize("target", [1000, 5000, 20000], ids=["image-end", "missing", "entry-end"])
def test_link_into_missing_entry_tail_keeps_partitions(tmp_path, chain, target):
    image = _make_mbr(tmp_path, [(200, _rdb(tmp_path).read_bytes())])
    _edit(image, 200 + chain, {4: target})
    image.write_bytes(image.read_bytes()[:1200 * 512])
    raw, disk, _ = open_rdisk(image)
    try:
        assert [str(p.get_drive_name()) for p in disk.parts] == ["DH0"]
        assert any(f"block {target}, past the end" in w for w in disk.rdb_warnings)
        if chain == 1:
            assert disk.damaged_part_list
        elif chain == LSEG:
            assert not disk.fs
    finally:
        disk.close()
        raw.close()
    assert find_partition(image, None, "DH0") is not None
