"""A late LOCATE reply that the bridge drops does not leave its lock held.

The test sends an ACTION_LOCATE_OBJECT for a file without waiting for it,
as a packet whose wait ran out, so that its reply comes during the next
call's wait, which drops it. The handler takes the lock and refuses the
delete in the same burst with ERROR_OBJECT_IN_USE. Once the bridge frees
the dropped lock, the next delete of the file succeeds. Runs on a copy of
each fixture image; no host FUSE driver is needed.
"""

import shutil

import pytest

from amifuse.fuse_fs import HandlerBridge
from amifuse.rdb_inspect import detect_adf

pytestmark = pytest.mark.integration

ERROR_OBJECT_IN_USE = 202


@pytest.mark.parametrize("image_name, driver_name, name", [
    ("pfs.hdf", "pfs3aio", "foo.md"),
    ("ofs.adf", "FastFileSystem", "OFS_README.txt"),
])
def test_a_dropped_late_lock_is_freed(fixture_root, tmp_path, image_name,
                                      driver_name, name):
    source = fixture_root / "fixtures" / "readonly" / image_name
    driver = fixture_root / "drivers" / driver_name
    if not source.exists() or not driver.exists():
        pytest.skip(f"Missing {image_name} or {driver_name}")
    image = tmp_path / image_name
    shutil.copy2(source, image)
    bridge = HandlerBridge(image, driver, read_only=False, adf_info=detect_adf(image))
    try:
        root, _ = bridge.locate(0, "")
        _, name_bptr = bridge._alloc_bstr(name)
        bridge.launcher.send_locate(bridge.state, root, name_bptr)

        # The lock is out when the handler reaches the delete.
        assert bridge.delete_object(root, name) == (0, ERROR_OBJECT_IN_USE)
        assert bridge.delete_object(root, name)[0] != 0
        assert bridge.stat_path("/" + name) is None
        bridge.free_lock(root)
    finally:
        bridge.close()
