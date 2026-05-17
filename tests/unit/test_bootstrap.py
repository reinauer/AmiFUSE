"""Unit tests for amifuse.bootstrap module."""

import inspect
import pytest


class TestRemapPdsToPfs:
    """Verify _remap_pds_to_pfs covers exactly PDS\\1 / PDS\\3 → PFS\\1 / PFS\\3."""

    @pytest.mark.parametrize("dt,expected", [
        (0x50445301, 0x50465301),  # PDS\1 -> PFS\1
        (0x50445303, 0x50465303),  # PDS\3 -> PFS\3
    ])
    def test_known_pds_versions_remap_to_pfs(self, dt, expected):
        from amifuse.bootstrap import _remap_pds_to_pfs, is_remapped_dostype
        assert _remap_pds_to_pfs(dt) == expected
        assert is_remapped_dostype(dt) is True

    @pytest.mark.parametrize("dt", [
        0x50465301,  # PFS\1 — already PFS, untouched
        0x50465303,  # PFS\3
        0x50445300,  # PDS\0 — unknown version, untouched
        0x50445302,  # PDS\2 — unknown version, untouched
        0x504453FF,  # PDS\xff — unknown version, untouched
        0x41465301,  # AFS\1 — different family
        0x444F5301,  # DOS\1 — different family
        0x53465300,  # SFS\0
        0x00000000,
        0xFFFFFFFF,
    ])
    def test_passthrough_for_non_matching_dostypes(self, dt):
        from amifuse.bootstrap import _remap_pds_to_pfs, is_remapped_dostype
        assert _remap_pds_to_pfs(dt) == dt
        assert is_remapped_dostype(dt) is False


class TestBootstrapAllocatorDosTypeWrite:
    """Verify alloc_all writes the (possibly remapped) dostype into de_DosType
    and that strict_dostype=True disables the remap.

    We mock the DosEnvecStruct field write to capture the value the allocator
    writes, without spinning up the real vamos runtime.
    """

    def _make_allocator(self, dos_type, strict_dostype, monkeypatch):
        """Build a BootstrapAllocator + run alloc_all far enough to capture
        the de_DosType write. Returns (allocator, captured_dostype)."""
        from amifuse import bootstrap

        captured = {}

        class _FakeField:
            def __init__(self, name):
                self._name = name
                self.val = None  # last write wins

            def __setattr__(self, attr, value):
                if attr == "val" and self._name == "de_DosType":
                    captured["de_DosType"] = value
                object.__setattr__(self, attr, value)

        class _SFields:
            def get_field_by_name(self, name):
                return _FakeField(name)

        class _FakeStruct:
            def __init__(self, *a, **kw):
                self.sfields = _SFields()

            @staticmethod
            def get_size():
                return 64

        class _FakeAllocMem:
            def __init__(self, addr):
                self.addr = addr

        class _FakeMem:
            def w_block(self, *a, **kw):
                pass

        class _FakeAlloc:
            def __init__(self):
                self._mem = _FakeMem()
                self._next = 0x1000

            def alloc_memory(self, size, label=None):
                addr = self._next
                self._next += 0x100
                return _FakeAllocMem(addr)

            def get_mem(self):
                return self._mem

        class _FakeVh:
            def __init__(self):
                self.alloc = _FakeAlloc()

        # Replace the three real structs with _FakeStruct.
        monkeypatch.setattr(bootstrap, "DosEnvecStruct", _FakeStruct)
        monkeypatch.setattr(bootstrap, "FileSysStartupMsgStruct", _FakeStruct)
        monkeypatch.setattr(bootstrap, "DeviceNodeStruct", _FakeStruct)

        # Synthesize a `de` with the requested dos_type.
        class _De:
            pass

        de = _De()
        de.size = 16
        de.block_size = 128
        de.sec_org = 0
        de.surfaces = 1
        de.sec_per_blk = 1
        de.blk_per_trk = 32
        de.reserved = 2
        de.pre_alloc = 0
        de.interleave = 0
        de.low_cyl = 0
        de.high_cyl = 79
        de.num_buffer = 5
        de.buf_mem_type = 0
        de.max_transfer = 0xFFFFFFFF
        de.mask = 0xFFFFFFFF
        de.boot_pri = 0
        de.dos_type = dos_type
        de.baud = 0
        de.control = 0
        de.boot_blocks = 2

        class _Part:
            num = 0

            def get_num_blocks(self):
                return 1000

        ba = bootstrap.BootstrapAllocator(
            _FakeVh(), image_path=None,
            strict_dostype=strict_dostype,
        )
        # Bypass the disk read; feed the synthesized envelope directly.
        ba._read_partition_env = lambda: (de, None, None, _Part())
        ba.alloc_all(handler_seglist_baddr=0, handler_seglist_bptr=0)
        return ba, captured["de_DosType"]

    def test_pds3_is_remapped_to_pfs3_by_default(self, monkeypatch):
        ba, written = self._make_allocator(0x50445303, strict_dostype=False, monkeypatch=monkeypatch)
        assert written == 0x50465303
        assert ba.remapped_dostype == (0x50445303, 0x50465303)

    def test_pds1_is_remapped_to_pfs1_by_default(self, monkeypatch):
        ba, written = self._make_allocator(0x50445301, strict_dostype=False, monkeypatch=monkeypatch)
        assert written == 0x50465301
        assert ba.remapped_dostype == (0x50445301, 0x50465301)

    def test_pfs3_is_unchanged(self, monkeypatch):
        ba, written = self._make_allocator(0x50465303, strict_dostype=False, monkeypatch=monkeypatch)
        assert written == 0x50465303
        assert ba.remapped_dostype is None

    def test_sfs_is_unchanged(self, monkeypatch):
        ba, written = self._make_allocator(0x53465300, strict_dostype=False, monkeypatch=monkeypatch)
        assert written == 0x53465300
        assert ba.remapped_dostype is None

    def test_strict_dostype_preserves_pds3(self, monkeypatch):
        ba, written = self._make_allocator(0x50445303, strict_dostype=True, monkeypatch=monkeypatch)
        assert written == 0x50445303
        assert ba.remapped_dostype is None


class TestAllocMsgport:
    """Verify alloc_msgport() initializes mp_MsgList correctly.

    Source inspection tests: fragile by design, to be replaced with
    functional tests when integration test infrastructure is available (Phase 5).
    """

    def test_alloc_msgport_empty_list_uses_sentinel_pointers(self):
        """Verify mp_MsgList is initialized as a proper empty Exec list."""
        from amifuse.bootstrap import BootstrapAllocator
        source = inspect.getsource(BootstrapAllocator.alloc_msgport)
        assert "lst.head.aptr = lh_tail_addr" in source
        assert "lst.tail_pred.aptr = lh_head_addr" in source
        assert "lst.head.aptr = 0" not in source

    def test_alloc_msgport_list_init_matches_init_msgport(self):
        """Verify alloc_msgport list init matches _init_msgport pattern."""
        from amifuse.startup_runner import HandlerLauncher
        from amifuse.bootstrap import BootstrapAllocator
        bootstrap_src = inspect.getsource(BootstrapAllocator.alloc_msgport)
        launcher_src = inspect.getsource(HandlerLauncher._init_msgport)
        assert "lst.tail.aptr = 0" in bootstrap_src
        assert "lst.tail.aptr = 0" in launcher_src
        assert "lst.head.aptr = 0" not in bootstrap_src
        assert "lst.head.aptr = 0" not in launcher_src
