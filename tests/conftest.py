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

    def _stub_module(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    dummy_cls = type("Dummy", (), {})

    _stub_module("amifuse.driver_runtime", BlockDeviceBackend=dummy_cls)
    _stub_module("amifuse.vamos_runner", VamosHandlerRuntime=dummy_cls)
    _stub_module("amifuse.bootstrap", BootstrapAllocator=dummy_cls)
    _stub_module("amifuse.process_mgr", ProcessManager=dummy_cls)
    _stub_module(
        "amifuse.startup_runner",
        HandlerLauncher=dummy_cls,
        OFFSET_BEGINNING=0,
        _get_block_state=lambda *args, **kwargs: None,
        _clear_all_block_state=lambda *args, **kwargs: None,
        _snapshot_block_state=lambda *args, **kwargs: None,
        _restore_block_state=lambda *args, **kwargs: None,
    )

    _stub_module("amitools", vamos=types.SimpleNamespace())
    _stub_module("amitools.vamos")
    _stub_module("amitools.vamos.astructs")
    _stub_module("amitools.vamos.astructs.access", AccessStruct=dummy_cls)
    _stub_module("amitools.vamos.libstructs")
    _stub_module(
        "amitools.vamos.libstructs.dos",
        FileInfoBlockStruct=dummy_cls,
        FileHandleStruct=dummy_cls,
        DosPacketStruct=dummy_cls,
    )
    # DosProtection needs to be callable with an int arg and str-able
    # so _format_protection(prot_bits) works in tests.
    class _FakeDosProtection:
        def __init__(self, bits=0):
            self._bits = bits

        def __str__(self):
            return f"----rwed" if self._bits == 0 else f"p={self._bits}"

    _stub_module("amitools.vamos.lib")
    _stub_module("amitools.vamos.lib.dos")
    _stub_module("amitools.vamos.lib.dos.DosProtection", DosProtection=_FakeDosProtection)


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
