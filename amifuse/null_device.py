from amitools.vamos.libcore import LibImpl  # type: ignore
from amitools.vamos.machine.regs import REG_A1  # type: ignore
from amitools.vamos.astructs.access import AccessStruct  # type: ignore
from amitools.vamos.libstructs.exec_ import IORequestStruct, NodeType  # type: ignore


class NullDevice(LibImpl):
    """Minimal stub device: always succeeds, no I/O."""

    def get_version(self):
        return 40

    def open_lib(self, ctx, open_cnt):
        return 0

    def close_lib(self, ctx, open_cnt):
        return 0

    def BeginIO(self, ctx):
        io_ptr = ctx.cpu.r_reg(REG_A1)
        io = AccessStruct(ctx.mem, IORequestStruct, io_ptr)
        io.w_s("io_Error", 0)
        io.w_s("io_Flags", io.r_s("io_Flags") | 1)  # IOF_QUICK
        io.w_s("io_Actual", 0)
        io.w_s("io_Message.mn_Node.ln_Type", NodeType.NT_REPLYMSG)
        return 0

    def AbortIO(self, ctx):
        return 0
