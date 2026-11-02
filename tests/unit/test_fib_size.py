"""An examine stays inside the bridge's FileInfoBlock whatever the name's length.

fib_FileName holds 108 bytes, but SmartFilesystem copies a volume name of up
to 255 characters into it whole, as a BSTR at offset 8 (#287). The allocation
has to reach the end of that BSTR.
"""

from types import SimpleNamespace

from amitools.vamos.machine.mock.mem import MockMemory
from amitools.vamos.mem.alloc import MemoryAlloc

from amifuse import fuse_fs as fs


def _bridge():
    mem = MockMemory(size_kib=64)
    b = object.__new__(fs.HandlerBridge)
    b._debug = False
    b.vh = SimpleNamespace(alloc=MemoryAlloc(mem, addr=0x1000))
    b.mem = mem
    b._fib_mem = None
    b._held_args = set
    return b


def test_a_full_bstr_stays_inside_the_fib():
    b = _bridge()
    fib = b._alloc_fib()
    guard = b.vh.alloc.alloc_memory(16, label="guard")
    assert guard.addr == fib.addr + fib.size
    b.mem.w_block(guard.addr, b"\xa5" * 16)

    b.mem.w_block(fib.addr + 8, bytes([255]) + b"N" * 255)

    assert bytes(b.mem.r_block(guard.addr, 16)) == b"\xa5" * 16


def test_a_new_examine_clears_the_whole_name():
    b = _bridge()
    fib = b._alloc_fib()
    b.mem.w_block(fib.addr + 8, bytes([255]) + b"N" * 255)

    assert b._alloc_fib() is fib
    assert bytes(b.mem.r_block(fib.addr + 8, 256)) == bytes(256)
