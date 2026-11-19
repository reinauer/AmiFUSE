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
from amifuse.startup_runner import ACTION_DISK_INFO
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


def _abandon_disk_info(bridge):
    """Leave an ACTION_DISK_INFO unanswered: its DOSTRUE comes late."""
    info = bridge.vh.alloc.alloc_memory(36, label="InfoData")
    assert bridge._packet(
        ACTION_DISK_INFO, [info.addr >> 2], "disk_info", max_iters=0) is None


def test_unlink_of_an_open_file_is_deferred_past_a_late_reply(sfs_ops):
    """The late DOSTRUE is not taken as the root lock.

    Taken as the lock, -1 goes out with the delete; SmartFilesystem
    refuses it with ERROR_DEVICE_NOT_MOUNTED and the unlink fails with
    EIO instead of being deferred to the release.
    """
    bridge = sfs_ops.bridge
    fh = sfs_ops.open("/Prefs.info", os.O_RDONLY)
    # Its reply comes first in the unlink's wait for the root lock.
    _abandon_disk_info(bridge)

    assert sfs_ops.unlink("/Prefs.info") == 0
    assert sfs_ops._pending_deletes == {"/Prefs.info"}
    sfs_ops.release("/Prefs.info", fh)
    assert bridge.stat_path("/Prefs.info") is None
