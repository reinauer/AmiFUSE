"""Shared test fixtures for AmiFUSE test suite."""
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def fixtures_path():
    """Path to the test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def fuse_mock(monkeypatch):
    """Inject a fake fuse module to allow importing amifuse.fuse_fs without FUSE.

    Adapted from tools/pfs_benchmark.py. Required because fuse_fs.py imports
    fusepy at module level. Tests that don't touch fuse_fs don't need this.

    Usage:
        def test_something(fuse_mock):
            from amifuse.fuse_fs import HandlerBridge
            ...
    """
    fake_fuse = types.ModuleType("fuse")

    class _DummyFuseError(RuntimeError):
        pass

    fake_fuse.FUSE = object
    fake_fuse.FuseOSError = _DummyFuseError
    fake_fuse.LoggingMixIn = type("LoggingMixIn", (), {})
    fake_fuse.Operations = type("Operations", (), {})
    monkeypatch.setitem(sys.modules, "fuse", fake_fuse)


@pytest.fixture
def amitools_mock(monkeypatch):
    """Inject stub amitools modules so rdb_inspect.py can be imported.

    rdb_inspect.py has top-level imports:
        from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
        from amitools.fs.rdb.RDisk import RDisk
        import amitools.fs.DosType as DosType

    These will fail with ModuleNotFoundError if amitools is not installed.
    The tested functions (detect_adf, detect_iso, detect_mbr, OffsetBlockDevice)
    do NOT use these imports -- they only need to be present to satisfy the
    module-level import.

    Must be requested BEFORE importing anything from amifuse.rdb_inspect.

    Usage:
        def test_something(amitools_mock):
            from amifuse.rdb_inspect import detect_adf
            ...
    """
    stubs = {}
    # Build the module hierarchy: amitools, amitools.fs, etc.
    for mod_path in [
        "amitools",
        "amitools.fs",
        "amitools.fs.blkdev",
        "amitools.fs.blkdev.RawBlockDevice",
        "amitools.fs.rdb",
        "amitools.fs.rdb.RDisk",
        "amitools.fs.DosType",
    ]:
        mod = types.ModuleType(mod_path)
        stubs[mod_path] = mod
        monkeypatch.setitem(sys.modules, mod_path, mod)

    # Add the attributes that rdb_inspect expects to import
    stubs["amitools.fs.blkdev.RawBlockDevice"].RawBlockDevice = type(
        "RawBlockDevice", (), {}
    )
    stubs["amitools.fs.rdb.RDisk"].RDisk = type("RDisk", (), {})

    # Wire up submodule attributes on parent packages
    stubs["amitools"].fs = stubs["amitools.fs"]
    stubs["amitools.fs"].blkdev = stubs["amitools.fs.blkdev"]
    stubs["amitools.fs.blkdev"].RawBlockDevice = stubs[
        "amitools.fs.blkdev.RawBlockDevice"
    ]
    stubs["amitools.fs"].rdb = stubs["amitools.fs.rdb"]
    stubs["amitools.fs.rdb"].RDisk = stubs["amitools.fs.rdb.RDisk"]
    stubs["amitools.fs"].DosType = stubs["amitools.fs.DosType"]
