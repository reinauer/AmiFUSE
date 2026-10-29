"""Two packets queued together on SmartFilesystem, on a copy of sfs.hdf.

SmartFilesystem's front process, whose port the bridge sends packets to
after startup, answers ACTION_DISK_INFO itself and forwards
ACTION_LOCATE_OBJECT to the main process's pr_MsgPort. The bridge drains
stray replies after each packet wait, and must leave a forwarded packet
there for the main process (#188). No host FUSE driver is needed.
"""

import shutil

import pytest

from amifuse.fuse_fs import HandlerBridge
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


def test_both_queued_packets_are_answered(sfs_bridge):
    bridge = sfs_bridge
    info = bridge.vh.alloc.alloc_memory(36, label="InfoData")
    disk_info, _ = bridge.launcher.send_disk_info(bridge.state, info.addr)
    _, name_bptr = bridge._alloc_bstr("")
    locate, _ = bridge.launcher.send_locate(bridge.state, 0, name_bptr)

    answered = {}
    for _ in range(6):
        for _, pkt, res1, _ in bridge._run_until_replies():
            answered[pkt] = res1
        if len(answered) == 2:
            break

    assert answered.get(disk_info) == -1
    # A lock on the root.
    assert answered.get(locate, 0) != 0
