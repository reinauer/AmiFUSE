"""The runtime's timer.device is AmiFuseTimerDevice from the start.

dos.library opens timer.device itself when it opens, for DOSBase's
dl_TimeReq, and a device stays loaded with the implementation it was opened
with. Registered any later, AmiFuseTimerDevice would never serve a request:
TR_ADDREQUEST would complete at once, and a handler that polls with short
delays would spin instead of waiting.
"""

from amifuse.timer_device import AmiFuseTimerDevice
from amifuse.vamos_runner import VamosHandlerRuntime


def test_timer_device_is_amifuses_once_the_base_libraries_are_open():
    runtime = VamosHandlerRuntime()
    runtime.setup()
    try:
        vlib = runtime.slm.lib_mgr.vlib_mgr.get_vlib_by_name("timer.device")
        assert vlib.get_impl() is runtime._get_timer_device()
        opened = runtime.slm.lib_mgr.open_lib("timer.device", 0)
        try:
            impl = runtime.slm.lib_mgr.vlib_mgr.get_vlib_by_addr(opened).get_impl()
            assert isinstance(impl, AmiFuseTimerDevice)
        finally:
            runtime.slm.lib_mgr.close_lib(opened)
    finally:
        runtime.shutdown()
