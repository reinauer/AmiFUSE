from amitools.vamos.libcore import LibImpl  # type: ignore
from amitools.vamos.libstructs.exec_ import IORequestStruct, NodeType  # type: ignore


class NullDevice(LibImpl):
    """Minimal stub device: always succeeds, no I/O."""

    def get_version(self):
        return 40

    def open_lib(self, ctx, open_cnt):
        return 0

    def close_lib(self, ctx, open_cnt):
        return 0

    def BeginIO(self, ctx, io_request):
        io = IORequestStruct(ctx.mem, io_request)
        io.error.val = 0
        io.flags.val |= 1  # IOF_QUICK
        io.actual.val = 0
        io.message.node.type.val = NodeType.NT_REPLYMSG
        return 0

    def AbortIO(self, ctx):
        return 0
