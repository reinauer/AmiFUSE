"""Pin AmiFUSE policies through the device factory installed by its runtime."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from amitools.vamos.libstructs.exec_ import IORequestStruct
from amitools.vamos.libstructs.scsidisk import SCSICmdStruct
from amitools.vamos.machine.mock import MockMemory
from amitools.vamos.mem import MemoryAlloc

from amifuse import vamos_runner


@pytest.fixture
def disk(monkeypatch):
    # Keep the runtime's setup and DiskSession real. Only replace the CPU and
    # library infrastructure; capture the factory setup actually registers.
    for name in ("Machine", "MemoryMap", "TraceManager", "VamosPathManager",
                 "Scheduler", "Runtime", "SetupLibManager"):
        monkeypatch.setattr(vamos_runner, name, MagicMock())
    monkeypatch.setattr(vamos_runner.VamosHandlerRuntime,
                        "_add_machine_run_method", lambda self: None)
    runtime = vamos_runner.VamosHandlerRuntime()
    backend = MagicMock()
    backend.block_size = 512
    backend.total_blocks = 1000
    backend.cyls, backend.heads, backend.secs = 10, 10, 10
    backend.read_only = False
    backend.read_blocks.return_value = b"r" * 512
    mem = MockMemory(size_kib=64)
    ctx = SimpleNamespace(mem=mem, alloc=MemoryAlloc(mem))
    device = None
    try:
        runtime.setup()
        runtime.set_scsi_backend(backend)
        factories = {call.args[0]: call.args[1]
                     for call in runtime.slm.lib_mgr.add_impl_cls.call_args_list}
        device = factories["scsi.device"]()
        request = ctx.alloc.alloc_astruct(IORequestStruct)
        assert device.open_dev(ctx, request.addr, 0, 0) == 0
        yield SimpleNamespace(runtime=runtime, backend=backend, device=device,
                              ctx=ctx, request=request)
    finally:
        if device is not None:
            device.finish_lib(ctx)
        runtime.shutdown()


def _io(disk, command, *, length=512, offset=0, data=0x4000):
    ior = IORequestStruct(disk.ctx.mem, disk.request.addr)
    ior.command.val = command
    ior.length.val = length
    ior.offset.val = offset
    ior.data.val = data
    ior.actual.val = 0
    disk.device.BeginIO(disk.ctx, disk.request.addr)
    return ior


@pytest.mark.parametrize("command", [3, 25, 0xC001, 11, 27, 0xC003])
def test_read_only_write_and_format_acknowledge_without_io(disk, command):
    disk.backend.read_only = True
    disk.ctx.mem.w_block(0x4000, b"w" * 512)
    ior = _io(disk, command)
    assert (ior.error.val, ior.actual.val) == (0, 512)
    disk.backend.write_blocks.assert_not_called()


def test_unknown_command_acknowledges_without_io(disk):
    disk.backend.read_only = True
    ior = _io(disk, 0x7FFF)
    assert (ior.error.val, ior.actual.val) == (0, 0)
    disk.backend.read_blocks.assert_not_called()
    disk.backend.write_blocks.assert_not_called()


@pytest.mark.parametrize("command", [11, 27, 0xC003])
def test_writable_format_writes_requested_data(disk, command):
    disk.ctx.mem.w_block(0x4000, b"f" * 512)
    ior = _io(disk, command, offset=512)
    assert (ior.error.val, ior.actual.val) == (0, 512)
    disk.backend.write_blocks.assert_called_once_with(1, b"f" * 512, 1)


@pytest.mark.parametrize("command", [2, 3, 24, 25, 0xC000, 0xC001])
@pytest.mark.parametrize("offset,length", [(1, 512), (0, 513)])
def test_unaligned_transfers_fail_without_io(disk, command, offset, length):
    ior = _io(disk, command, offset=offset, length=length)
    assert (ior.error.val, ior.actual.val) == (-4, 0)
    disk.backend.read_blocks.assert_not_called()
    disk.backend.write_blocks.assert_not_called()


@pytest.mark.parametrize("length", [0, 31, 32])
def test_geometry_requires_complete_output_buffer(disk, length):
    disk.ctx.mem.w_block(0x4000, b"g" * 32)
    ior = _io(disk, 22, length=length)
    if length < 32:
        assert ior.error.val == -4
        assert disk.ctx.mem.r_block(0x4000, 32) == b"g" * 32
    else:
        assert ior.error.val == 0
        assert disk.ctx.mem.r32(0x4000) == 512
        assert disk.ctx.mem.r32(0x4004) == 1000


def _scsi(disk, opcode, blocks=1):
    mem = disk.ctx.mem
    scsi = SCSICmdStruct(mem, 0x2000)
    scsi.scsi_Data.val = 0x4000
    scsi.scsi_Length.val = 512
    scsi.scsi_Command.val = 0x3000
    scsi.scsi_CmdLength.val = 10
    scsi.scsi_SenseData.val = 0x5000
    scsi.scsi_SenseLength.val = 18
    mem.w8(0x3000, opcode)
    mem.w16(0x3007, blocks)
    return scsi


@pytest.mark.parametrize("length", [0, 29, 30])
def test_direct_scsi_requires_complete_command_struct(disk, length):
    scsi = _scsi(disk, 0x28)
    ior = _io(disk, 28, data=0x2000, length=length)
    if length < 30:
        assert ior.error.val == -4
        disk.backend.read_blocks.assert_not_called()
    else:
        assert ior.error.val == 0
        assert (scsi.scsi_Status.val, scsi.scsi_Actual.val) == (0, 512)
        assert disk.ctx.mem.r_block(0x4000, 512) == b"r" * 512


def test_direct_scsi_short_read_reports_check_condition(disk):
    scsi = _scsi(disk, 0x28, blocks=2)
    _io(disk, 28, data=0x2000, length=30)
    assert (scsi.scsi_Status.val, scsi.scsi_Actual.val) == (2, 0)
    disk.backend.read_blocks.assert_not_called()


def test_direct_scsi_read_only_write_acknowledges_without_io(disk):
    disk.backend.read_only = True
    scsi = _scsi(disk, 0x2A)
    ior = _io(disk, 28, data=0x2000, length=30)
    assert ior.error.val == 0
    assert (scsi.scsi_Status.val, scsi.scsi_Actual.val) == (0, 512)
    disk.backend.write_blocks.assert_not_called()


def test_copied_request_works_only_while_device_is_open(disk):
    mem = disk.ctx.mem
    _io(disk, 2)
    copy = disk.ctx.alloc.alloc_astruct(IORequestStruct)
    mem.w_block(copy.addr, mem.r_block(disk.request.addr,
                                     IORequestStruct.get_byte_size()))
    disk.backend.read_blocks.reset_mock()
    disk.device.BeginIO(disk.ctx, copy.addr)
    assert IORequestStruct(mem, copy.addr).error.val == 0
    disk.backend.read_blocks.assert_called_once_with(0, 1)
    disk.device.close_dev(disk.ctx, disk.request.addr)
    disk.backend.read_blocks.reset_mock()
    disk.device.BeginIO(disk.ctx, copy.addr)
    assert IORequestStruct(mem, copy.addr).error.val == 32  # TDERR_BAD_UNIT_NUM
    disk.backend.read_blocks.assert_not_called()
