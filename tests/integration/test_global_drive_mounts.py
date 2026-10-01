"""Exercise real Windows mount-manager allocation, aliases and cleanup."""

import ctypes
import os
import shutil
import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.fuse, pytest.mark.slow,
              pytest.mark.skipif(sys.platform != "win32", reason="Windows mount manager")]


def volume_name(root):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel.GetVolumeNameForVolumeMountPointW
    query.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    query.restype = ctypes.c_int
    result = ctypes.create_unicode_buffer(128)
    if not query(str(root), result, len(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    return result.value


@pytest.mark.parametrize("explicit", [False, True], ids=["automatic", "explicit"])
def test_global_drive_write_unmount_remount(pfs3_image, pfs3_driver, mount_image, tmp_path, explicit):
    from amifuse.platform import is_windows_admin

    if not is_windows_admin():
        pytest.skip("global drive allocation requires Administrator privileges")
    image = tmp_path / "global-drive.hdf"
    shutil.copy2(pfs3_image, image)
    proc, root = mount_image(image, driver=pfs3_driver,
                             extra_args=["--write"], mountmgr=explicit)
    # A volume GUID registered with Mount Manager distinguishes this from
    # the per-logon DefineDosDevice drive that used to be created here.
    guid_path = volume_name(root)
    assert guid_path.startswith("\\\\?\\Volume{"), guid_path
    payload = b"global drive data must survive clean unmount"
    with open(os.path.join(root, "global.txt"), "wb") as stream:
        stream.write(payload)
    with open(guid_path + "global.txt", "rb") as stream:
        assert stream.read() == payload

    # Use the opposite spelling to prove discovery matches both aliases.
    unmount_path = root if explicit else "\\\\.\\" + root.rstrip("\\")
    result = subprocess.run([sys.executable, "-m", "amifuse", "unmount", unmount_path],
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    proc.wait(timeout=10)
    deadline = time.monotonic() + 5
    while os.path.exists(root) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not os.path.exists(root), "global drive survived unmount"

    _, root = mount_image(image, driver=pfs3_driver)
    with open(os.path.join(root, "global.txt"), "rb") as stream:
        assert stream.read() == payload
