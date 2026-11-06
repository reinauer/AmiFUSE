"""Delete-on-close must follow a directory renamed through its handle."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys

import pytest

pytestmark = [pytest.mark.fuse,
              pytest.mark.skipif(sys.platform != "win32", reason="WinFsp")]


def test_delete_renamed_directory_keeps_replacement(
        mount_image, pfs3_image, pfs3_driver):
    _proc, root = mount_image(pfs3_image, driver=pfs3_driver,
                              extra_args=["--write"])
    old, new = Path(root) / "OldDir", Path(root) / "NewDir"
    old.mkdir()
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD]
    api.SetFileInformationByHandle.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateFileW(str(old), 0x10000 | 0x80, 7, None, 3,
                             0x02000000, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.WinError()
    try:
        name = str(new)

        class Rename(ctypes.Structure):
            _fields_ = [("replace", wintypes.BOOLEAN),
                        ("root", wintypes.HANDLE),
                        ("length", wintypes.DWORD),
                        ("name", wintypes.WCHAR * len(name))]

        rename = Rename(False, None, len(name.encode("utf-16-le")), name)
        assert api.SetFileInformationByHandle(handle, 3, ctypes.byref(rename),
                                              ctypes.sizeof(rename)), ctypes.WinError()
        old.mkdir()
        delete = wintypes.BOOL(True)
        assert api.SetFileInformationByHandle(handle, 4, ctypes.byref(delete),
                                              ctypes.sizeof(delete)), ctypes.WinError()
    finally:
        assert api.CloseHandle(handle), ctypes.WinError()
    assert old.is_dir(), "the new directory at the old name was deleted"
    assert not new.exists(), "the renamed directory was not deleted"
