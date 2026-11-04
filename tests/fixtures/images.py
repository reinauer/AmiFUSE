"""Images built at test time, for the bundled FastFileSystem.

With these and paths.BUNDLED_FFS a test needs no AmiFUSE-testing checkout.
Mark such a test no_fixtures, so that it runs without one.
"""
from __future__ import annotations

from contextlib import ExitStack

from amitools.fs import DosType
from amitools.fs.ADFSVolume import ADFSVolume
from amitools.fs.FSString import FSString
from amitools.fs.blkdev.ADFBlockDevice import ADFBlockDevice
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk

from amifuse.fuse_fs import format_volume
from tests.fixtures.paths import BUNDLED_FFS


def make_adf(path, dos_type=DosType.DOS1, files=None, hd=False):
    """Create a floppy image holding files, a dict of name to bytes."""
    blkdev = ADFBlockDevice(str(path), hd=hd)
    blkdev.create()
    volume = ADFSVolume(blkdev)
    volume.create(FSString("Test"), dos_type=dos_type)
    for name, data in (files or {}).items():
        volume.write_file(data, FSString(name))
    volume.close()
    blkdev.close()
    return path


def make_ffs_partition(path, dos_type=DosType.DOS1, volume="Test"):
    """Create a disk image with one 16 MiB partition, DH0, that the bundled
    handler has formatted."""
    raw = RawBlockDevice(str(path), read_only=False)
    with ExitStack() as cleanup:
        raw.create(64 * 16 * 32)
        cleanup.callback(raw.close)
        disk = RDisk(raw)
        cleanup.callback(disk.close)
        disk.create(DiskGeometry(64, 16, 32), rdb_cyls=1)
        disk.add_partition(FSString("DH0"), (1, 63), dos_type=dos_type)
        raw.flush()
    format_volume(path, BUNDLED_FFS, None, "DH0", volume)
    return path
