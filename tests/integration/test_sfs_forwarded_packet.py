"""Two packets queued together on SmartFilesystem, on a copy of sfs.hdf.

SmartFilesystem's front process, whose port the bridge sends packets to
after startup, answers ACTION_DISK_INFO itself and forwards
ACTION_LOCATE_OBJECT to the main process's pr_MsgPort. The bridge drains
stray replies after each packet wait, and must leave a forwarded packet
there for the main process (#188). No host FUSE driver is needed.
"""

import shutil

import pytest

from amitools.vamos.libstructs.dos import InfoDataStruct  # type: ignore

from amifuse.fuse_fs import HandlerBridge
from amifuse.startup_runner import (
    ACTION_DISK_INFO, ACTION_FREE_LOCK, ACTION_LOCATE_OBJECT, SHARED_LOCK,
)
from amifuse.rdb_inspect import detect_adf

pytestmark = pytest.mark.integration


@pytest.fixture
def sfs_bridge(fixture_root, tmp_path):
    source = fixture_root / "fixtures" / "readonly" / "sfs.hdf"
    driver = fixture_root / "drivers" / "SmartFilesystem"
    if not source.exists() or not driver.exists():
        pytest.skip("Missing sfs.hdf or SmartFilesystem")
    image = tmp_path / "sfs.hdf"
    shutil.copy2(source, image)
    bridge = HandlerBridge(image, driver, read_only=False, adf_info=detect_adf(image))
    try:
        yield bridge
    finally:
        bridge.close()


def test_both_queued_packets_are_answered(sfs_bridge, monkeypatch):
    bridge = sfs_bridge
    sent = []
    send = bridge.dospkt._send_unwaited

    def record(action, arg1):
        sent.append((int(action), arg1))
        return send(action, arg1)

    monkeypatch.setattr(bridge.dospkt, "_send_unwaited", record)
    info = bridge.vh.alloc.alloc_memory(36, label="InfoData")
    bridge.mem.w_block(info.addr, bytes(36))
    # Both waits end before the handler runs, so both packets are queued.
    assert bridge._packet(
        ACTION_DISK_INFO, [info.addr >> 2], "disk_info", max_iters=0) is None
    _, name_bptr = bridge._alloc_bstr("")
    assert bridge._packet(
        ACTION_LOCATE_OBJECT, [0, name_bptr, SHARED_LOCK], "locate", max_iters=0) is None
    assert bridge._packets_out() == [ACTION_DISK_INFO, ACTION_LOCATE_OBJECT]

    # Both are answered; the late lock is given back with ACTION_FREE_LOCK.
    assert bridge.wait_for_pending() == []
    assert [action for action, _ in sent] == [ACTION_FREE_LOCK]
    assert sent[0][1] != 0
    assert InfoDataStruct(bridge.mem, info.addr).id_NumBlocks.val > 0
