"""The handler bootstrap must open the disk at the backend's block size."""

from unittest.mock import Mock

import pytest

from amifuse.bootstrap import BootstrapAllocator
from amifuse.fuse_fs import HandlerBridge
from amifuse.rdb_inspect import ADF_DD_SIZE, detect_adf
from tests.unit.test_mbr_rdb_block_size import _make_mbr, _make_rdb


class _Opened(Exception):
    pass


def _bootstrap_open(monkeypatch, image, reader, **kwargs):
    # Run the real backend and the real bootstrap reader, then stop before
    # any handler state is allocated; the 68k runtime is a mock.
    seen = {}
    real = getattr(BootstrapAllocator, reader)

    def opened(self):
        _, blk, rdisk, part = real(self)
        seen["block_bytes"] = blk.block_bytes
        seen["offset"] = getattr(blk, "offset", 0)
        if rdisk is not None:
            seen["drive"] = str(part.get_drive_name())
            rdisk.close()
        blk.close()
        raise _Opened

    monkeypatch.setattr(BootstrapAllocator, reader, opened)
    monkeypatch.setattr("amifuse.fuse_fs.VamosHandlerRuntime", Mock)
    with pytest.raises(_Opened):
        HandlerBridge(image, image.parent / "handler", **kwargs)
    return seen


@pytest.mark.parametrize("forced", [False, True])
def test_plain_rdb_with_large_blocks(tmp_path, monkeypatch, forced):
    image = tmp_path / "rdb.hdf"
    image.write_bytes(_make_rdb(tmp_path, 1024))
    seen = _bootstrap_open(monkeypatch, image, "_read_partition_env",
                           block_size=1024 if forced else None)
    assert seen == {"block_bytes": 1024, "offset": 0, "drive": "DH0"}


@pytest.mark.parametrize("start_lba,forced", [
    (2048, False), (2048, True),
    # A forced 4096 at LBA 64 also needs the fix for #93.
    (64, False),
])
def test_rdb_with_large_blocks_inside_mbr_partition(tmp_path, monkeypatch,
                                                    start_lba, forced):
    image = _make_mbr(tmp_path, [(start_lba, _make_rdb(tmp_path, 4096))])
    seen = _bootstrap_open(monkeypatch, image, "_read_partition_env",
                           block_size=4096 if forced else None)
    assert seen == {"block_bytes": 4096, "offset": start_lba * 512 // 4096,
                    "drive": "DH0"}


def test_adf_keeps_512_byte_blocks(tmp_path, monkeypatch):
    # ls passes the requested size, None by default, for floppies too.
    image = tmp_path / "disk.adf"
    image.write_bytes(b"DOS\0".ljust(ADF_DD_SIZE, b"\0"))
    seen = _bootstrap_open(monkeypatch, image, "_read_adf_env",
                           block_size=None, adf_info=detect_adf(image))
    assert seen == {"block_bytes": 512, "offset": 0}
