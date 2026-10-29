"""AmiFuseExecLibrary and timer.device's Unit in a real runtime.

Exec's stub binds its methods when exec.library opens, so the runtime
registers AmiFuseExecLibrary before then; and amitools' OpenDevice must
keep the Unit that timer.device puts into io_Unit.
"""

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration

UNIT_VBLANK = 1
TR_ADDREQUEST = 9
_IO_UNIT = 24
_REQ_SIZE = 40  # struct timerequest


@pytest.fixture
def runtime():
    from amifuse.vamos_runner import VamosHandlerRuntime

    rt = VamosHandlerRuntime()
    rt.setup()
    try:
        yield rt
    finally:
        rt.shutdown()


def test_exec_is_amifuses_and_a_copy_of_an_opened_request_is_held(runtime):
    """The runtime's Exec is AmiFuseExecLibrary; a copied request waits; WaitIO ends it."""
    from amifuse.exec_library import AmiFuseExecLibrary

    exec_impl = runtime.slm.exec_impl
    assert type(exec_impl) is AmiFuseExecLibrary
    ctx = runtime.slm.exec_ctx
    mem = ctx.mem
    io = runtime.alloc.alloc_memory(_REQ_SIZE, label="timerequest").addr
    copy = runtime.alloc.alloc_memory(_REQ_SIZE, label="copy").addr
    for off in range(0, _REQ_SIZE, 4):  # no reply port: nothing is queued
        mem.w32(io + off, 0)
    name = SimpleNamespace(str="timer.device")
    assert exec_impl.OpenDevice(ctx, name, UNIT_VBLANK, io, 0) == 0
    unit = mem.r32(io + _IO_UNIT)
    assert unit != 0
    mem.r16(unit + 36)  # unit_OpenCnt: a Unit a handler can read
    for off in range(0, _REQ_SIZE, 4):
        mem.w32(copy + off, mem.r32(io + off))
    mem.w16(copy + 28, TR_ADDREQUEST)
    mem.w32(copy + 32, 1)
    mem.w32(copy + 36, 0)
    exec_impl.SendIO(ctx, copy)
    timer = runtime.timer_device
    assert timer.next_deadline() != float("inf")
    assert exec_impl.WaitIO(ctx, copy) == 0
    assert timer.next_deadline() == float("inf")
    assert timer.completed_early == {"WaitIO": 1}
    exec_impl.CloseDevice(ctx, io)
    assert mem.r32(io + _IO_UNIT) == 0
