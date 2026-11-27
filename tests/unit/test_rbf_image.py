"""Unit tests for OS-9 RBF disk images without an RDB (amifuse.rbf_image).

The images are built here: an OS-9000 ID block in either byte order, an
OS-9/68k or OS-9/6809 LSN 0 with its root directory, the CoCo half-sector
layout, a PC boot sector in front of the ID block, and an MBR holding the
volume. Driver resolution for such volumes, and for RDB partitions of the
OS-9 DOS types, goes to os9-rbf-handler in the user's drivers folder.
"""

import json
import struct
from contextlib import ExitStack
from pathlib import Path

import pytest
from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk

from amifuse import rbf_image
from amifuse.bootstrap import SyntheticRbfDosEnv
from amifuse.driver_resolver import DriverNotFound
from amifuse.rbf_image import (
    DOS_OS9_6809, DOS_OS9_68K, DOS_OS9K_BE, DOS_OS9K_LE, find_rbf_volumes,
    is_os9_type, select_rbf_volume,
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
                    record_size: bool = True, sync: bool = False) -> bytes:
    """An OS-9/68k volume: LSN 0, a one-sector bitmap, the root FD at
    LSN 2 and its directory at LSN 3 with ".." and "." entries. Without
    record_size and sync, LSN 0 has none of the fields OS-9/68k added, and
    with 256-byte sectors the volume is an OS-9/6809 one."""
    img = bytearray(sectors * ss)
    lsn0 = img
    lsn0[0:3] = sectors.to_bytes(3, "big")                   # DD_TOT
    struct.pack_into(">H", lsn0, 0x04, (sectors + 7) // 8)   # DD_MAP
    struct.pack_into(">H", lsn0, 0x06, 1)                    # DD_BIT
    lsn0[0x08:0x0B] = (2).to_bytes(3, "big")                 # DD_DIR
    lsn0[0x1F:0x1F + len(name)] = name[:-1] + bytes([name[-1] | 0x80])
    if record_size:
        struct.pack_into(">H", lsn0, 0x68, ss)               # DD_LSNSIZE
    if sync:
        lsn0[0x60:0x64] = b"Cruz"                            # DD_SYNC
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


def _os9_6809_volume(sectors: int, name: bytes = b"Vol6809") -> bytes:
    """An OS-9/6809 volume: 256-byte sectors, LSN 0 without the OS-9/68k
    fields, and 3-byte FD LSNs after 29-byte names."""
    img = bytearray(_os9_68k_volume(sectors, 256, name, record_size=False))
    d = 3 * 256
    for ent in (d, d + 32):                 # FD LSN in the last 3 bytes only
        img[ent + 28:ent + 32] = b"\x00" + (2).to_bytes(3, "big")
    return bytes(img)


def _half_sectors(volume: bytes) -> bytes:
    """The CoCo hard disk layout: each 256-byte sector in the first half of
    its own 512-byte block."""
    return b"".join(volume[i:i + 256] + bytes(256) for i in range(0, len(volume), 256))


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


def _with_mbr_entries(*volumes) -> bytes:
    """A disk with one MBR partition per (with_boot_sector, volume), in
    order, each starting on a 64-sector boundary."""
    mbr = bytearray(512)
    body = bytearray()
    start = 64
    for i, (boot, volume) in enumerate(volumes):
        data = (bytes(512) if boot else b"") + volume
        data += bytes(-len(data) % (64 * 512))
        entry = 0x1BE + 16 * i
        mbr[entry + 4] = 0x09
        struct.pack_into("<II", mbr, entry + 8, start + len(body) // 512, len(data) // 512)
        body += data
    mbr[0x1FE:0x200] = b"\x55\xaa"
    return bytes(mbr) + bytes(63 * 512) + bytes(body)


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
    # An LSN 0 with no OS-9/68k field at all (no DD_LSNSIZE, no sync code)
    # is OS-9/6809 with 256-byte sectors, or the half-sector layout with
    # 512-byte blocks, as the handler judges it; see the tests below.
    vol = find_rbf_volumes(_write(tmp_path, _os9_68k_volume(
        80, ss, record_size=record_size, sync=not record_size)))[0]
    assert (vol.dos_type, vol.volume_name) == (DOS_OS9_68K, "Vol68k")
    assert vol.format_name == "OS-9/68k"
    # Without DD_LSNSIZE, the first size whose root directory checks out.
    assert vol.block_size == ss
    assert vol.low_block == 0 and not vol.half_sectors


def test_os9_6809_volume(tmp_path):
    vol = find_rbf_volumes(_write(tmp_path, _os9_6809_volume(80)))[0]
    assert (vol.dos_type, vol.volume_name) == (DOS_OS9_6809, "Vol6809")
    assert vol.format_name == "OS-9/6809"
    assert (vol.block_size, vol.low_block, vol.num_blocks, vol.fs_blocks) == (256, 0, 80, 80)
    assert not vol.half_sectors


def test_os9_6809_volume_in_half_sectors(tmp_path):
    vol = find_rbf_volumes(_write(tmp_path, _half_sectors(_os9_6809_volume(80))))[0]
    assert (vol.dos_type, vol.volume_name) == (DOS_OS9_6809, "Vol6809")
    # The handler is given 512-byte blocks and finds the layout itself.
    assert (vol.block_size, vol.low_block, vol.num_blocks, vol.fs_blocks) == (512, 0, 80, 80)
    assert vol.half_sectors
    assert "256-byte sectors in 512-byte blocks" in vol.where()
    assert SyntheticRbfDosEnv(vol).block_size == 128


def test_os9_6809_volume_in_an_mbr_partition(tmp_path):
    img = _with_mbr_entries((False, _half_sectors(_os9_6809_volume(80))))
    vol = find_rbf_volumes(_write(tmp_path, img))[0]
    assert (vol.dos_type, vol.mbr_index, vol.half_sectors) == (DOS_OS9_6809, 0, True)
    assert vol.low_block == 64


def test_os9_dos_types_are_those_of_handler_release_1_1():
    assert [DOS_OS9_6809, DOS_OS9_68K, DOS_OS9K_BE, DOS_OS9K_LE] == [
        0x4F533900, 0x4F533901, 0x4F533902, 0x4F533903]
    assert all(is_os9_type(t) for t in range(0x4F533900, 0x4F533904))
    assert not is_os9_type(0x4F533904) and not is_os9_type(0x444F5300)
    assert not is_os9_type(None)


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

    for dos_type in (DOS_OS9_6809, DOS_OS9_68K, DOS_OS9K_BE, DOS_OS9K_LE):
        bridge = HandlerBridge.__new__(HandlerBridge)
        bridge._dos_type = dos_type
        bridge._first_block = 0
        bridge.get_disk_info = lambda: None
        assert bridge.name_fold() == "ascii"


# ---------------------------------------------------------------------------
# Without the handler: what the CLI says
# ---------------------------------------------------------------------------

def _os9_rdb(path: Path, dos_type: int = DOS_OS9_68K) -> Path:
    """An RDB disk with one OS-9 partition and no driver in the RDB."""
    raw = RawBlockDevice(str(path), read_only=False, block_bytes=512)
    with ExitStack() as cleanup:
        raw.create(320)
        cleanup.callback(raw.close)
        rdisk = RDisk(raw)
        cleanup.callback(rdisk.close)
        rdisk.create(DiskGeometry(10, 1, 32), rdb_cyls=1)
        rdisk.add_partition(FSString("OS9"), (1, 9), dos_type=dos_type)
        raw.flush()
    return path


@pytest.mark.parametrize("dos_type", [DOS_OS9_6809, DOS_OS9_68K, DOS_OS9K_BE, DOS_OS9K_LE])
def test_rdb_os9_partition_without_driver_in_the_rdb(user_driver_dir, tmp_path, dos_type):
    """Before OS-9 support this raised "No embedded filesystem driver found"."""
    from amifuse.fuse_fs import resolve_driver

    image = _os9_rdb(tmp_path / "disk.hdf", dos_type)
    with pytest.raises(DriverNotFound) as caught:
        resolve_driver(image, None, None, "OS9")
    message = str(caught.value)
    assert f"Partition OS9 has no filesystem driver for OS9{dos_type & 0xFF}" in message
    assert rbf_image.DRIVER_NAME in message and str(user_driver_dir) in message
    assert rbf_image.RELEASES_URL in message

    handler = user_driver_dir / rbf_image.DRIVER_NAME
    handler.write_bytes(b"\x00\x00\x03\xf3$VER: os9-rbf-handler 1.0 (1.1.2026)\x00")
    resolved = resolve_driver(image, None, None, "OS9")
    assert (resolved.path, resolved.source) == (handler, "user")


def test_inspect_json_without_the_handler(user_driver_dir, tmp_path, capsys):
    from amifuse import fuse_fs

    image = _write(tmp_path, _os9k_volume(64, False))
    fuse_fs.main(["inspect", "--json", str(image)])     # exits 0: no SystemExit
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["image_type"] == "os9"
    assert out["driver"] is None
    assert rbf_image.DRIVER_NAME in out["note"]
    (vol,) = out["volumes"]
    assert (vol["volume_name"], vol["dos_type_raw"]) == ("Vol9K", "0x4f533902")
    assert vol["half_sectors"] is False


def test_inspect_text_without_the_handler(user_driver_dir, tmp_path, capsys):
    from amifuse import fuse_fs

    image = _write(tmp_path, _os9k_volume(64, True))
    fuse_fs.main(["inspect", str(image)])
    out = capsys.readouterr().out
    assert "OS-9 Disk Image" in out and "OS-9000 (little-endian)" in out
    assert "DOS type: OS93 (0x4f533903)" in out
    assert f"Place {rbf_image.DRIVER_NAME}" not in out
    assert f"named exactly {rbf_image.DRIVER_NAME}" in out


def test_inspect_text_of_a_half_sector_disk(user_driver_dir, tmp_path, capsys):
    from amifuse import fuse_fs

    image = _write(tmp_path, _half_sectors(_os9_6809_volume(80)))
    fuse_fs.main(["inspect", str(image)])
    out = capsys.readouterr().out
    assert "Format: OS-9/6809" in out and "DOS type: OS90 (0x4f533900)" in out
    assert "Block size: 512 bytes (256-byte sectors, one in each)" in out


def test_ls_without_the_handler(user_driver_dir, tmp_path, capsys):
    from amifuse import fuse_fs

    image = _write(tmp_path, _os9k_volume(64, True))
    with pytest.raises(SystemExit) as caught:
        fuse_fs.main(["ls", str(image)])
    assert rbf_image.DRIVER_NAME in str(caught.value)

    with pytest.raises(SystemExit) as caught:
        fuse_fs.main(["ls", "--json", str(image)])
    assert caught.value.code == 1
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["code"] == "DRIVER_NOT_FOUND"
    assert rbf_image.DRIVER_NAME in err["message"]


def test_ls_with_a_volume_the_image_does_not_hold(user_driver_dir, tmp_path, capsys):
    from amifuse import fuse_fs

    image = _write(tmp_path, _with_mbr(_os9k_volume(100, True)))
    with pytest.raises(SystemExit) as caught:
        fuse_fs.main(["ls", str(image), "--partition", "Other"])
    assert "No OS-9 volume 'Other'" in str(caught.value)

    with pytest.raises(SystemExit) as caught:
        fuse_fs.main(["ls", "--json", str(image), "--partition", "1"])
    assert caught.value.code == 1
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["code"] == "PARTITION_NOT_FOUND"
    assert "0 (Vol9K)" in err["message"]


def test_launcher_mounts_each_volume_of_an_mbr(tmp_path):
    from amifuse.launcher import _enumerate_mount_units

    image = _write(tmp_path, _with_mbr_entries(
        (True, _os9k_volume(100, True, b"PcVol")),
        (False, _os9_68k_volume(160, 512, b"M68Vol"))))
    vols = find_rbf_volumes(image)
    assert [(v.mbr_index, v.dos_type) for v in vols] == [(0, DOS_OS9K_LE), (1, DOS_OS9_68K)]
    units, fallback = _enumerate_mount_units(str(image))
    assert fallback is None
    assert [(u.name, u.label) for u in units] == [("0", "PcVol"), ("1", "M68Vol")]


@pytest.mark.parametrize("volume", [
    _os9k_volume(64, True), _os9_68k_volume(160, 512)], ids=["os9000", "os9-68k"])
def test_launcher_mounts_a_lone_volume_without_partition(tmp_path, volume):
    from amifuse.launcher import _enumerate_mount_units

    units, fallback = _enumerate_mount_units(str(_write(tmp_path, volume)))
    assert fallback is None
    assert [(u.name, u.label) for u in units] == [(None, "OS-9 disk")]


def test_format_refuses_an_os9_disk_without_an_rdb(user_driver_dir, tmp_path):
    from amifuse import fuse_fs

    image = _write(tmp_path, _os9_68k_volume(160, 512))
    before = image.read_bytes()
    with pytest.raises(SystemExit) as caught:
        fuse_fs.main(["format", str(image), "NewVol"])
    assert str(caught.value) == (
        f"Error: {image} is an OS-9 disk without an RDB. amifuse format "
        "formats RDB partitions only; format the disk with OS-9 tools or "
        "from AmigaOS.")
    assert image.read_bytes() == before


def test_format_of_an_os9_floppy_of_adf_size_asks_for_dos_type(user_driver_dir, tmp_path):
    """An OS-9 floppy the size of an ADF keeps the floppy message."""
    from amifuse import fuse_fs

    image = _write(tmp_path, _os9_68k_volume(1760, 512))
    assert find_rbf_volumes(image)
    with pytest.raises(SystemExit) as caught:
        fuse_fs.main(["format", str(image), "NewVol"])
    assert "--dos-type" in str(caught.value)
