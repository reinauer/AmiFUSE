"""Unit tests for OS-9 RBF disk images without an RDB (amifuse.rbf_image).

The images are built here: an OS-9000 ID block in either byte order, an
OS-9/68k LSN 0 with its root directory, a PC boot sector in front of the
ID block, and an MBR holding the volume. Driver resolution for such
volumes, and for RDB partitions of the OS-9 DOS types, goes to
os9-rbf-handler in the user's drivers folder.
"""

import struct
from pathlib import Path

import pytest

from amifuse import rbf_image
from amifuse.bootstrap import SyntheticRbfDosEnv
from amifuse.driver_resolver import DriverNotFound
from amifuse.rbf_image import (
    DOS_OS9_68K, DOS_OS9K_BE, DOS_OS9K_LE, find_rbf_volumes, select_rbf_volume,
)


def _os9k_volume(blocks: int, little: bool, name: bytes = b"Vol9K") -> bytes:
    """An OS-9000 volume of 512-byte blocks: just the ID block."""
    order = "<" if little else ">"
    idb = bytearray(512)
    idb[0:4] = b"\xad\xb0\xb0\xad"
    struct.pack_into(order + "I", idb, 8, blocks)            # total blocks
    struct.pack_into(order + "H", idb, 20, 512)              # block size
    struct.pack_into(order + "I", idb, 28, 1)                # bitmap FD
    struct.pack_into(order + "I", idb, 40, 2)                # root FD
    idb[56:56 + len(name)] = name
    idb[88] = ord("L" if little else "B")
    return bytes(idb) + bytes((blocks - 1) * 512)


def _os9_68k_volume(sectors: int, ss: int = 256, name: bytes = b"Vol68k",
                    record_size: bool = True) -> bytes:
    """An OS-9/68k volume: LSN 0, a one-sector bitmap, the root FD at
    LSN 2 and its directory at LSN 3 with ".." and "." entries."""
    img = bytearray(sectors * ss)
    lsn0 = img
    lsn0[0:3] = sectors.to_bytes(3, "big")                   # DD_TOT
    struct.pack_into(">H", lsn0, 0x04, (sectors + 7) // 8)   # DD_MAP
    struct.pack_into(">H", lsn0, 0x06, 1)                    # DD_BIT
    lsn0[0x08:0x0B] = (2).to_bytes(3, "big")                 # DD_DIR
    lsn0[0x1F:0x1F + len(name)] = name[:-1] + bytes([name[-1] | 0x80])
    if record_size:
        struct.pack_into(">H", lsn0, 0x68, ss)               # DD_LSNSIZE
    fd = 2 * ss
    img[fd] = 0xBF                                           # dir, all access
    struct.pack_into(">I", img, fd + 0x09, 64)               # FD_SIZ
    img[fd + 0x10:fd + 0x13] = (3).to_bytes(3, "big")        # FD_SEG LSN
    struct.pack_into(">H", img, fd + 0x13, 1)                # FD_SEG count
    d = 3 * ss
    img[d:d + 2] = b".\xae"
    struct.pack_into(">I", img, d + 28, 2)
    img[d + 32] = 0xAE
    struct.pack_into(">I", img, d + 32 + 28, 2)
    return bytes(img)


def _with_mbr(volume: bytes, start: int = 63, ptype: int = 0x09) -> bytes:
    """A disk with one MBR partition holding a boot sector and volume."""
    mbr = bytearray(512)
    entry = 0x1BE
    mbr[entry] = 0x80
    mbr[entry + 4] = ptype
    nsec = 1 + len(volume) // 512
    struct.pack_into("<II", mbr, entry + 8, start, nsec)
    mbr[0x1FE:0x200] = b"\x55\xaa"
    gap = bytes((start - 1) * 512)
    return bytes(mbr) + gap + bytes(512) + volume


def _write(tmp_path: Path, data: bytes, name: str = "disk.img") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("little, dos_type", [(True, DOS_OS9K_LE), (False, DOS_OS9K_BE)])
def test_os9000_volume_at_block_0(tmp_path, little, dos_type):
    vols = find_rbf_volumes(_write(tmp_path, _os9k_volume(64, little)))
    assert len(vols) == 1
    vol = vols[0]
    assert (vol.dos_type, vol.volume_name, vol.block_size) == (dos_type, "Vol9K", 512)
    assert (vol.low_block, vol.num_blocks, vol.fs_blocks) == (0, 64, 64)
    assert vol.mbr_index is None and not vol.boot_sector


def test_os9000_volume_after_a_pc_boot_sector(tmp_path):
    vol = find_rbf_volumes(_write(tmp_path, bytes(512) + _os9k_volume(63, True)))[0]
    assert vol.boot_sector
    assert (vol.low_block, vol.num_blocks) == (1, 63)


def test_os9000_volume_in_an_mbr_partition(tmp_path):
    vol = find_rbf_volumes(_write(tmp_path, _with_mbr(_os9k_volume(100, True))))[0]
    assert vol.mbr_index == 0 and vol.boot_sector
    assert vol.low_block == 64          # partition start 63, then the boot sector
    assert vol.num_blocks == 100


@pytest.mark.parametrize("ss", [256, 512])
@pytest.mark.parametrize("record_size", [True, False])
def test_os9_68k_volume(tmp_path, ss, record_size):
    vol = find_rbf_volumes(_write(tmp_path, _os9_68k_volume(80, ss, record_size=record_size)))[0]
    assert (vol.dos_type, vol.volume_name) == (DOS_OS9_68K, "Vol68k")
    # Without DD_LSNSIZE, the first size whose root directory checks out.
    assert vol.block_size == ss
    assert vol.low_block == 0


def test_os9_68k_without_dot_entries_is_not_detected(tmp_path):
    img = bytearray(_os9_68k_volume(80))
    img[3 * 256:3 * 256 + 64] = bytes(64)
    assert find_rbf_volumes(_write(tmp_path, bytes(img))) == []


def test_volume_larger_than_the_image_is_not_detected(tmp_path):
    img = _os9k_volume(64, True)[:32 * 512]
    assert find_rbf_volumes(_write(tmp_path, img)) == []


@pytest.mark.parametrize("data", [
    bytes(1024 * 1024),
    b"DOS\x00" + bytes(901116),
    bytes(512) + b"RDSK" + bytes(4096),
], ids=["blank", "adf", "rdb"])
def test_other_images_hold_no_rbf_volume(tmp_path, data):
    assert find_rbf_volumes(_write(tmp_path, data)) == []


def test_mbr_with_an_amiga_rdb_partition_is_left_to_the_rdb_code(tmp_path):
    img = _with_mbr(_os9k_volume(100, True), ptype=0x76)
    assert find_rbf_volumes(_write(tmp_path, img)) == []


def test_missing_image_holds_no_volume(tmp_path):
    assert find_rbf_volumes(tmp_path / "missing.img") == []


# ---------------------------------------------------------------------------
# Selection and geometry
# ---------------------------------------------------------------------------

def test_select_by_mbr_entry_or_volume_name(tmp_path):
    vol = find_rbf_volumes(_write(tmp_path, _with_mbr(_os9k_volume(100, True))))[0]
    assert select_rbf_volume([vol], None) is vol
    assert select_rbf_volume([vol], "0") is vol
    assert select_rbf_volume([vol], "vol9k") is vol
    with pytest.raises(ValueError, match="0 \\(Vol9K\\)"):
        select_rbf_volume([vol], "1")


def test_synthetic_dos_env_spans_the_volume(tmp_path):
    vol = find_rbf_volumes(_write(tmp_path, bytes(512) + _os9k_volume(63, False)))[0]
    de = SyntheticRbfDosEnv(vol)
    assert (de.surfaces, de.blk_per_trk, de.block_size) == (1, 1, 128)
    assert (de.low_cyl, de.high_cyl) == (1, 63)
    assert de.dos_type == DOS_OS9K_BE


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------

def test_driver_comes_from_the_users_drivers_folder(user_driver_dir, tmp_path):
    from amifuse.fuse_fs import resolve_driver

    image = _write(tmp_path, _os9k_volume(64, True))
    vol = find_rbf_volumes(image)[0]
    with pytest.raises(DriverNotFound, match="os9-rbf-handler"):
        resolve_driver(image, None, None, None, rbf_info=vol)
    handler = user_driver_dir / rbf_image.DRIVER_NAME
    handler.write_bytes(b"\x00\x00\x03\xf3$VER: os9-rbf-handler 1.0 (1.1.2026)\x00")
    resolved = resolve_driver(image, None, None, None, rbf_info=vol)
    assert (resolved.path, resolved.source) == (handler, "user")
    assert resolved.version == "os9-rbf-handler 1.0 (1.1.2026)"


def test_explicit_driver_wins(user_driver_dir, tmp_path):
    from amifuse.fuse_fs import resolve_driver

    image = _write(tmp_path, _os9k_volume(64, True))
    vol = find_rbf_volumes(image)[0]
    resolved = resolve_driver(image, tmp_path / "h", None, None, rbf_info=vol)
    assert resolved.source == "explicit"


def test_os9_volume_folds_ascii(fuse_mock):
    from amifuse.fuse_fs import HandlerBridge

    for dos_type in (DOS_OS9_68K, DOS_OS9K_BE, DOS_OS9K_LE):
        bridge = HandlerBridge.__new__(HandlerBridge)
        bridge._dos_type = dos_type
        bridge._first_block = 0
        bridge.get_disk_info = lambda: None
        assert bridge.name_fold() == "ascii"
