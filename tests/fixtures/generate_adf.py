#!/usr/bin/env python3
"""Generate deterministic ADF test fixtures using amitools API.

Run standalone to regenerate: python tests/fixtures/generate_adf.py
Or import generate_ofs_adf() / generate_ffs_adf() from integration tests.
"""
from pathlib import Path

from amitools.fs.ADFSVolume import ADFSVolume
from amitools.fs.blkdev.ADFBlockDevice import ADFBlockDevice
from amitools.fs.FSString import FSString
from amitools.fs.TimeStamp import TimeStamp
from amitools.fs.RootMetaInfo import RootMetaInfo
import amitools.fs.DosType as DosType

FIXTURES_DIR = Path(__file__).parent / "images"
# Deterministic timestamp for reproducible fixtures
# TimeStamp.parse() uses "%d.%m.%Y %H:%M:%S" format
_FIXED_TS = TimeStamp()
_FIXED_TS.parse("01.01.2024 00:00:00")
_FIXED_META = RootMetaInfo(create_ts=_FIXED_TS, disk_ts=_FIXED_TS, mod_ts=_FIXED_TS)


def generate_ofs_adf(path: Path | None = None) -> Path:
    """Create an 880KB OFS ADF with test files."""
    path = path or (FIXTURES_DIR / "test_ofs.adf")
    blkdev = ADFBlockDevice(str(path))
    blkdev.create()  # DD 880KB
    vol = ADFSVolume(blkdev)
    vol.create(FSString("TestOFS"), meta_info=_FIXED_META, dos_type=DosType.DOS0)
    vol.write_file(b"Hello, Amiga!\n", FSString("hello.txt"))
    vol.create_dir(FSString("subdir"))
    vol.write_file(b"Nested file\n", FSString("subdir/nested.txt"))
    vol.close()
    blkdev.close()
    return path


def generate_ffs_adf(path: Path | None = None) -> Path:
    """Create an 880KB FFS ADF with test files."""
    path = path or (FIXTURES_DIR / "test_ffs.adf")
    blkdev = ADFBlockDevice(str(path))
    blkdev.create()  # DD 880KB
    vol = ADFSVolume(blkdev)
    vol.create(FSString("TestFFS"), meta_info=_FIXED_META, dos_type=DosType.DOS_FFS)
    vol.write_file(b"FFS test data\n", FSString("data.txt"))
    vol.create_dir(FSString("Dir1"))
    vol.write_file(b"A" * 1024, FSString("Dir1/kilobyte.bin"))
    vol.close()
    blkdev.close()
    return path


if __name__ == "__main__":
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Generating OFS ADF: {generate_ofs_adf()}")
    print(f"Generating FFS ADF: {generate_ffs_adf()}")
