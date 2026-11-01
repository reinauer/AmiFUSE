"""The handler as a task of the vamos scheduler, on real handlers.

The handler and the children it creates run as scheduler tasks on the
bridge's emulation thread; a wait in exec or dos suspends the task, and the
bridge runs the tasks until all of them wait (startup_runner.py,
emu_thread.py). No host FUSE driver is needed.
"""

import gc
import shutil
import time
import weakref

import pytest

from amitools.vamos.schedule import RunResult, TaskState

from amifuse.fuse_fs import HandlerBridge
from amifuse.rdb_inspect import detect_adf

pytestmark = pytest.mark.integration


@pytest.fixture
def pfs3_bridge(pfs3_image, pfs3_driver):
    bridge = HandlerBridge(pfs3_image, pfs3_driver, read_only=True)
    try:
        yield bridge
    finally:
        bridge.close()


def test_handler_waits_for_a_packet_and_is_resumed_with_it(pfs3_bridge):
    """A task in WaitPkt()/WaitPort() on its empty port leaves every task waiting;
    a packet sent to the port wakes it, and it replies."""
    bridge = pfs3_bridge
    task = bridge.state.task
    assert bridge._emu.call(bridge._run) == RunResult.IDLE
    assert task.get_state() == TaskState.TS_WAIT

    def disk_info():
        info = bridge.vh.alloc.alloc_memory(36, label="InfoData")
        try:
            pkt_addr, _ = bridge.launcher.send_disk_info(bridge.state, info.addr)
            replies = bridge._run_until_replies()
        finally:
            bridge.vh.alloc.free_memory(info)
        return pkt_addr, replies

    pkt_addr, replies = bridge._emu.call(disk_info)
    assert [(pkt, res1) for _, pkt, res1, _ in replies] == [(pkt_addr, -1)]
    assert task.get_state() == TaskState.TS_WAIT


@pytest.fixture
def no_log_records():
    """vamos logs the tasks as arguments of its records, and pytest's log
    capture would hold those records, and so the tasks, until the test ends."""
    import logging

    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


def test_closing_the_bridge_lets_the_scheduler_and_its_tasks_go(
    pfs3_image, pfs3_driver, no_log_records
):
    """The tasks are ended at shutdown: a greenlet suspended in a wait would
    otherwise keep the scheduler, the machine and the tasks alive forever."""
    bridge = HandlerBridge(pfs3_image, pfs3_driver, read_only=True)
    scheduler = weakref.ref(bridge.vh.scheduler)
    task = weakref.ref(bridge.state.task)
    machine = weakref.ref(bridge.vh.machine)
    bridge.close()
    del bridge
    gc.collect()
    assert scheduler() is None
    assert task() is None
    assert machine() is None


def test_startup_delays_are_fast_forwarded(fixture_root, tmp_path):
    """SmartFilesystem polls with Delay() for the helper process it starts;
    the delays end at once during startup, as the timer requests do."""
    source = fixture_root / "fixtures" / "readonly" / "sfs.hdf"
    driver = fixture_root / "drivers" / "SmartFilesystem"
    if not source.exists() or not driver.exists():
        pytest.skip("Missing sfs.hdf or SmartFilesystem")
    image = tmp_path / "sfs.hdf"
    shutil.copy2(source, image)
    start = time.monotonic()
    bridge = HandlerBridge(image, driver, read_only=False, adf_info=detect_adf(image))
    try:
        elapsed = time.monotonic() - start
        timer = bridge.vh.timer_device
        assert timer.delays_ended > 0
        assert timer.next_delay() == float("inf")
        # the helper process runs as a task of its own
        assert len(bridge.vh.slm.dos_impl.child_procs) >= 1
        assert elapsed < 5.0, f"startup took {elapsed:.1f} s"
    finally:
        bridge.close()
