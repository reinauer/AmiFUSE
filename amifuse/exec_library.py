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
- Wait and WaitPort record a parked task on ExecLibrary by name, where the
  bridge reads it, so the subclass holds none of that state.
"""

from amitools.vamos.lib.ExecLibrary import ExecLibrary  # type: ignore


class AmiFuseExecLibrary(ExecLibrary):
    """ExecLibrary whose DoIO and WaitIO have a device complete a held request."""

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
