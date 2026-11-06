"""Windows volume flush must not leave completed metadata in handler RAM."""

import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.fuse,
              pytest.mark.skipif(sys.platform != "win32", reason="WinFsp")]


def test_directory_creation_survives_volume_flush_and_kill(
        mount_image, pfs3_image, pfs3_driver, tmp_path):
    image = tmp_path / "disk.hdf"
    shutil.copy2(pfs3_image, image)
    proc, root = mount_image(image, driver=pfs3_driver, extra_args=["--write"])
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    api.FlushFileBuffers.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateFileW("\\\\.\\" + str(root)[:2], 0x40000000,
                             7, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.WinError()
    try:
        (Path(root) / "PersistedDir").mkdir()
        assert api.FlushFileBuffers(handle), ctypes.WinError()
        proc.kill()
        proc.wait(timeout=10)
    finally:
        api.CloseHandle(handle)
    result = subprocess.run(
        [sys.executable, "-m", "amifuse", "ls", str(image),
         "--driver", str(pfs3_driver), "--json"], capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PersistedDir" in [item["name"] for item in json.loads(result.stdout)["entries"]]
