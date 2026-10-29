"""timer.device under real handlers: replies at the deadline, while idle.

Each fixture handler writes a file through the bridge while the mount's
timer thread runs, as it does in a mount, and must read it back from the
image afterwards. The handlers arm timers after a write (PFS3 and SFS to
write back, FFS too), so each must see deliveries, and the image must
change while the mount idles.

The probe counts the uses of timer.device that a delayed reply would
break. Exec in vamos cannot wait in DoIO, or in WaitIO on a pending
request, so AmiFUSE's completes such a request before its delay, and the
device counts it. WaitPort on the reply port of a pending request spins
if the port manager does not know the port. And a packet wait that ends
without a reply while a timer is pending may have left the handler
waiting for its own timer. All four must stay at zero.
"""

import os
import shutil
import time
from collections import Counter

import pytest

pytestmark = pytest.mark.integration

# (handler, image): every fixture handler with a writable fixture image.
CASES = [
    ("pfs3aio", "pfs.hdf"),
    ("SmartFilesystem", "sfs.hdf"),
    ("FastFileSystem", "ofs.adf"),
]
DATA = bytes(range(256)) * 300  # 76,800 bytes: several blocks, two packets


@pytest.fixture
def timer_probe(monkeypatch):
    """Count the uses of timer.device that a delayed reply would break.

    WaitPort on an unregistered reply port of a pending request, and
    packet waits that ended without a reply while a request was pending;
    DoIO and WaitIO on a pending request are the device's own count.
    Library stubs bind ExecLibrary's methods when a runtime is set up, so
    the class is patched before the bridge is built.
    """
    from amifuse.fuse_fs import HandlerBridge
    from amitools.vamos.lib.ExecLibrary import ExecLibrary
    from amitools.vamos.libtypes import Message, MsgPort

    counts = Counter()
    devices = []
    wait_port = ExecLibrary.WaitPort
    run_until_replies = HandlerBridge._run_until_replies

    # Annotated as ExecLibrary's own: the stub converts arguments by them.
    def WaitPort(self, ctx, msg_port: MsgPort) -> Message:
        if not self.port_mgr.has_port(msg_port.addr):
            counts["WaitPort"] += any(
                ctx.mem.r32(io + 14) == msg_port.addr  # mn_ReplyPort
                for dev in devices for io in dev._pending)
        return wait_port(self, ctx, msg_port)

    def _run_until_replies(self, *args, **kwargs):
        replies = run_until_replies(self, *args, **kwargs)
        counts["NoReply"] += not replies and any(dev._pending for dev in devices)
        return replies

    monkeypatch.setattr(ExecLibrary, "WaitPort", WaitPort)
    monkeypatch.setattr(HandlerBridge, "_run_until_replies", _run_until_replies)
    return counts, devices


def _setup(fixture_root, tmp_path, driver_name, image_name):
    from amifuse.rdb_inspect import detect_adf

    driver = fixture_root / "drivers" / driver_name
    source = fixture_root / "fixtures" / "readonly" / image_name
    if not driver.exists() or not source.exists():
        pytest.skip(f"{driver_name} handler or {image_name} not found")
    image = tmp_path / image_name
    shutil.copy2(source, image)
    return image, driver, detect_adf(image)


def _write(bridge, path, data):
    fh, lock = bridge.open_file(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        assert bridge.write_handle(fh, data) == len(data)
    finally:
        bridge.close_file(fh)
        if lock:
            bridge.free_lock(lock)


def _image_bytes(bridge, image):
    """The image as the handler has written it so far.

    Read to its size only: the session's lock holds the byte past the end.
    """
    with bridge._lock:
        bridge.backend.sync()
        with open(image, "rb") as f:
            return f.read(image.stat().st_size)


def _read_back(image, driver, adf_info, path):
    from amifuse.fuse_fs import HandlerBridge

    bridge = HandlerBridge(image, driver, read_only=True, adf_info=adf_info)
    try:
        return bridge.read_file(path, len(DATA) + 1, 0)
    finally:
        bridge.close()


@pytest.mark.parametrize("driver_name, image_name", CASES)
def test_idle_mount_delivers_timers(fixture_root, tmp_path, timer_probe,
                                    driver_name, image_name):
    """While the mount idles after a write, the handler's timers fire."""
    from amifuse.fuse_fs import AmigaFuseFS, HandlerBridge

    image, driver, adf_info = _setup(fixture_root, tmp_path, driver_name, image_name)
    counts, devices = timer_probe
    bridge = HandlerBridge(image, driver, read_only=False, adf_info=adf_info)
    timer = bridge.vh.timer_device
    devices.append(timer)
    fs = AmigaFuseFS(bridge)
    fs.init("/")
    try:
        assert fs._timer_thread.is_alive()
        _write(bridge, "/timer_live.bin", DATA)
        delivered = timer.deliveries
        written = _image_bytes(bridge, image)
        time.sleep(3)
        assert timer.deliveries > delivered
        # The handler acted on its timers: it wrote back while idle.
        assert _image_bytes(bridge, image) != written
        assert bridge.read_file("/timer_live.bin", len(DATA) + 1, 0) == DATA
        assert not bridge.state.crashed
    finally:
        fs.destroy("/")
    assert timer.completed_early == {}
    assert [counts[k] for k in ("WaitPort", "NoReply")] == [0, 0]
    assert _read_back(image, driver, adf_info, "/timer_live.bin") == DATA


@pytest.mark.parametrize("driver_name, image_name", CASES)
def test_flush_with_a_timer_pending(fixture_root, tmp_path, driver_name, image_name):
    """An unmount right after a write flushes while a timer is pending.

    SFS then aborts its write-back timer with AbortIO and WaitIO before
    it writes back.
    """
    from amifuse.fuse_fs import AmigaFuseFS, HandlerBridge

    image, driver, adf_info = _setup(fixture_root, tmp_path, driver_name, image_name)
    bridge = HandlerBridge(image, driver, read_only=False, adf_info=adf_info)
    fs = AmigaFuseFS(bridge)
    fs.init("/")
    try:
        _write(bridge, "/timer_flush.bin", DATA)
        bridge.flush_volume()
        assert not bridge.state.crashed
    finally:
        fs.destroy("/")
    assert _read_back(image, driver, adf_info, "/timer_flush.bin") == DATA
