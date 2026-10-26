"""A late reply to another packet, on a real handler in write mode.

The bridge stops waiting for a packet that runs out of time, and the
handler answers it later, while the next call waits. The test takes a real
ACTION_DISK_INFO reply and hands it back, alone, in the next wait, as such
a late reply would arrive. It then unlinks an open file through the FUSE
callback, on a copy of the fixture image. No host FUSE driver is needed.
"""

import os
import shutil

import pytest

from amifuse.fuse_fs import AmigaFuseFS, HandlerBridge
from amifuse.rdb_inspect import detect_adf

pytestmark = pytest.mark.integration


@pytest.fixture
def sfs_ops(fixture_root, tmp_path):
    source = fixture_root / "fixtures" / "readonly" / "sfs.hdf"
    driver = fixture_root / "drivers" / "SmartFilesystem"
    if not source.exists() or not driver.exists():
        pytest.skip("Missing sfs.hdf or SmartFilesystem")
    image = tmp_path / "sfs.hdf"
    shutil.copy2(source, image)
    bridge = HandlerBridge(image, driver, read_only=False, adf_info=detect_adf(image))
    try:
        yield AmigaFuseFS(bridge)
    finally:
        bridge.close()


def _late_disk_info_reply(bridge):
    """Run an ACTION_DISK_INFO to its reply and return that reply burst."""
    info = bridge.vh.alloc.alloc_memory(36, label="InfoData")
    pkt_addr, _ = bridge.launcher.send_disk_info(bridge.state, info.addr)
    replies = bridge._run_until_replies()
    # Precondition, not the check: SmartFilesystem answers DISK_INFO alone.
    assert [(pkt, res1) for _, pkt, res1, _ in replies] == [(pkt_addr, -1)]
    return replies


def test_unlink_of_an_open_file_is_deferred_past_a_late_reply(sfs_ops, monkeypatch):
    """The late DOSTRUE is not taken as the root lock.

    Taken as the lock, -1 goes out with the delete; SmartFilesystem
    refuses it with ERROR_DEVICE_NOT_MOUNTED and the unlink fails with
    EIO instead of being deferred to the release.
    """
    bridge = sfs_ops.bridge
    fh = sfs_ops.open("/Prefs.info", os.O_RDONLY)
    late = _late_disk_info_reply(bridge)

    run = bridge._run_until_replies
    pending = [late]

    def wait_with_late_reply_first(*args, **kwargs):
        return pending.pop() if pending else run(*args, **kwargs)

    monkeypatch.setattr(bridge, "_run_until_replies", wait_with_late_reply_first)

    assert sfs_ops.unlink("/Prefs.info") == 0
    assert sfs_ops._pending_deletes == {"/Prefs.info"}
    sfs_ops.release("/Prefs.info", fh)
    assert bridge.stat_path("/Prefs.info") is None
