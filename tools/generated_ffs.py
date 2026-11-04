"""Build disposable FFS/OFS smoke images without external files or drivers."""
from contextlib import ExitStack
from pathlib import Path
import struct

BUNDLED_FFS = Path(__file__).resolve().parents[1] / 'amifuse/drivers/FastFileSystem.OS3'


def make_adf(path):
    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.FSString import FSString
    from amitools.fs.blkdev.ADFBlockDevice import ADFBlockDevice
    path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as cleanup:
        raw = ADFBlockDevice(str(path))
        raw.create()
        cleanup.callback(raw.close)
        vol = ADFSVolume(raw)
        vol.create(FSString('SmokeOFS'), dos_type=0x444f5300)
        cleanup.callback(vol.close)
        vol.create_dir(FSString('Docs'))
        vol.write_file(b'Generated OFS smoke fixture\n', FSString('OFS_README.txt'))
        vol.write_file(bytes(range(256)) * 1024, FSString('Docs/OFS_LARGE.bin'))
    return path


def make_rdb(path, embedded=False):
    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.FSString import FSString
    from amitools.fs.blkdev.DiskGeometry import DiskGeometry
    from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
    from amitools.fs.rdb.RDisk import RDisk
    path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as cleanup:
        raw = RawBlockDevice(str(path), read_only=False)
        raw.create(64 * 16 * 32)
        cleanup.callback(raw.close)
        disk = RDisk(raw)
        cleanup.callback(disk.close)
        disk.create(DiskGeometry(64, 16, 32), rdb_cyls=1)
        if embedded:
            disk.add_filesystem(BUNDLED_FFS.read_bytes(), dos_type=0x444f5301)
        for name, cyls in [('QDH0', (1, 31)), ('QDH1', (32, 63))]:
            disk.add_partition(FSString(name), cyls, dos_type=0x444f5301)
        for part in disk.parts:
            blk = part.create_blkdev()
            blk.open()
            vol = ADFSVolume(blk)
            vol.create(FSString('Work'), dos_type=0x444f5301)
            try:
                vol.create_dir(FSString('Prefs'))
                # A small original planar icon exercises --icons on macOS.
                icon = bytearray(78 + 20 + 32)
                struct.pack_into('>HH', icon, 0, 0xE310, 1)
                struct.pack_into('>hh', icon, 12, 16, 16)
                icon[48] = 1  # WBDISK
                struct.pack_into('>hhh', icon, 82, 16, 16, 1)
                icon[92] = 1  # PlanePick
                icon[98:] = b'\xff\xff' * 16
                vol.write_file(bytes(icon), FSString('Disk.info'))
                vol.write_file(b'Generated FFS smoke fixture\n', FSString('README.txt'))
                vol.write_file(bytes(range(256)) * 4096, FSString('large.bin'))
            finally:
                vol.close()
        raw.flush()
    return path


def make_emu68(path):
    """Wrap the generated RDB in an MBR entry without changing its geometry."""
    make_rdb(path)
    rdb = path.read_bytes()
    mbr = bytearray(2048 * 512)
    mbr[446 + 4] = 0x76
    struct.pack_into('<II', mbr, 446 + 8, 2048, len(rdb) // 512)
    mbr[510:512] = b'\x55\xaa'
    path.write_bytes(mbr + rdb)
    return path
