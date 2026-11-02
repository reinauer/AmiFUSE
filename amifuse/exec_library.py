"""exec.library whose DoIO and WaitIO return with no request pending.

Exec's DoIO and WaitIO wait until the device replies; amitools' return at
once. While every request completed in BeginIO that did not matter. A
request AmiFuseTimerDevice holds is replied later, to a handler that takes
it for finished: after DoIO the reply is a stray message on its port, and
after WaitIO the handler may reuse or free the request while it is still
queued. vamos cannot block in a library call, so AmiFuseExecLibrary has
the device complete such a request at once, and WaitIO then takes the
reply back off the port, as after a wait.

A device that holds requests defines complete_now(ctx, io_request, call),
with call "DoIO" or "WaitIO": if it holds io_request, it replies it to its
port without a signal and returns True; otherwise it returns False.

What it relies on in amitools:
- A library's stub binds its methods when the library opens, so
  VamosHandlerRuntime registers this class before exec.library opens.
- setup_lib sets lib_mgr and _io_device_off, and lib_mgr.get_vlib_by_addr
  returns None for an address that is no library.
- DoIO queues no reply itself; WaitIO takes a reply off a port the port
  manager knows, from its queue and, by ln_Succ and ln_Pred, its list.
- WaitPort on an empty port the port manager knows raises
  UnsupportedFeatureError instead of waiting; on a port it does not know
  it waits for the port's signal. AmiFuseExecLibrary waits on both.
- The stub binds a call's arguments and its result by the method's
  annotations, so WaitPort keeps amitools' (msg_port: MsgPort) -> Message.
- signal_func.wait() suspends the current task until one of the signals
  is set, and takes the signals it returns.
"""

from amitools.vamos.lib.ExecLibrary import ExecLibrary  # type: ignore
from amitools.vamos.libstructs.exec_ import MsgPortFlags  # type: ignore
from amitools.vamos.libtypes import Message, MsgPort  # type: ignore

from .rdb_inspect import escape_text


class AmiFuseExecLibrary(ExecLibrary):
    """ExecLibrary whose DoIO and WaitIO have a device complete a held request,
    whose WaitPort waits on a port the port manager knows, and whose console
    shows a handler's text escaped."""

    def _raw_console_write(self, value):
        """Send one byte of a handler's debug text to stderr.

        Handlers print names from the image here: ODFileSystem logs the
        volume and system IDs of an ISO. So a byte goes through escape_text,
        as such a name would, but for the line ends and tabs of the text.
        """
        if value in (9, 10, 13):
            super()._raw_console_write(value)
            return
        if value:
            for ch in escape_text(chr(value)):
                super()._raw_console_write(ord(ch))

    def WaitPort(self, ctx, msg_port: MsgPort) -> Message:
        """Wait for a message on the port, then return the first one.

        A packet to the handler goes to a port the port manager knows, and
        amitools' WaitPort fails on such a port when it is empty. Wait for
        the port's signal instead, as Exec does, until a message is there;
        then amitools' WaitPort returns it without taking it off the port.
        """
        port_mgr = self.port_mgr
        addr = msg_port.addr
        if port_mgr.has_port(addr):
            while not port_mgr.has_msg(addr):
                sig_bit = msg_port.sig_bit.val
                if msg_port.flags.val != MsgPortFlags.PA_SIGNAL or not 0 <= sig_bit < 32:
                    # nothing would ever wake us: let amitools report it
                    break
                self.signal_func.wait(1 << sig_bit)
        return super().WaitPort(ctx, msg_port)

    def DoIO(self, ctx, io_request):
        result = super().DoIO(ctx, io_request)
        if self._complete_now(ctx, io_request, "DoIO"):
            result = super().WaitIO(ctx, io_request)
        return result

    def WaitIO(self, ctx, io_request):
        self._complete_now(ctx, io_request, "WaitIO")
        return super().WaitIO(ctx, io_request)

    def _complete_now(self, ctx, io_request, call) -> bool:
        vlib = self.lib_mgr.get_vlib_by_addr(ctx.mem.r32(io_request + self._io_device_off))
        impl = vlib.get_impl() if vlib is not None else None
        complete = getattr(impl, "complete_now", None)
        return complete is not None and complete(ctx, io_request, call)
