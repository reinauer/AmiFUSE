"""Delete-on-close must follow a directory renamed through its handle."""

import ctypes
from ctypes import wintypes
import os
import shutil
from pathlib import Path
import sys

import pytest

pytestmark = [pytest.mark.fuse,
              pytest.mark.skipif(sys.platform != "win32", reason="WinFsp")]


def test_delete_renamed_directory_keeps_replacement(
        mount_image, pfs3_image, pfs3_driver, tmp_path):
    image = tmp_path / "disk.hdf"
    shutil.copy2(pfs3_image, image)
    _proc, root = mount_image(image, driver=pfs3_driver,
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


@pytest.fixture(params=["bundled", "FastFileSystem", "pfs3aio", "SmartFilesystem"])
def mounted_handler(request, mount_image, tmp_path):
    from amifuse.driver_resolver import BUNDLED_DRIVER
    from tests.integration.test_bundled_ffs import _seed_adf

    handler = request.param
    if handler == "bundled":
        image, driver = _seed_adf(tmp_path, "DOS3"), BUNDLED_DRIVER
    else:
        fixtures = request.getfixturevalue("fixture_root")
        filename = {"FastFileSystem": "ofs.adf", "pfs3aio": "pfs.hdf",
                    "SmartFilesystem": "sfs.hdf"}[handler]
        image, driver = tmp_path / filename, fixtures / "drivers" / handler
        shutil.copy2(fixtures / "fixtures/readonly" / filename, image)
    proc, root = mount_image(image, driver=driver, extra_args=["--write"])
    return handler, image, driver, proc, Path(root)


def _file_api():
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
    return api


def _open_for_rename(api, path, directory):
    handle = api.CreateFileW(str(path), 0x10000 | 0x80, 7, None, 3,
                             0x02000000 if directory else 0, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.WinError(ctypes.get_last_error())
    return handle


def _rename_handle(api, handle, target):
    name = str(target)
    class Rename(ctypes.Structure):
        _fields_ = [("replace", wintypes.BOOLEAN), ("root", wintypes.HANDLE),
                    ("length", wintypes.DWORD), ("name", wintypes.WCHAR * len(name))]
    rename = Rename(False, None, len(name.encode("utf-16-le")), name)
    assert api.SetFileInformationByHandle(handle, 3, ctypes.byref(rename),
                                          ctypes.sizeof(rename)), ctypes.WinError(ctypes.get_last_error())


def test_repeated_native_handle_rename(mounted_handler):
    from amifuse.fuse_fs import HandlerBridge
    from amifuse.rdb_inspect import detect_adf
    from tests.integration.test_bundled_ffs import _unmount

    handler, image, driver, proc, root = mounted_handler
    api = _file_api()
    kinds = [True] if handler == "bundled" else [True, False]
    for directory in kinds:
        name = "Dir" if directory else "File"
        original, middle, last = [root / (name + suffix) for suffix in ("A", "B", "C")]
        if directory:
            original.mkdir()
        else:
            original.write_bytes(b"rename data")
        handle = _open_for_rename(api, original, directory)
        try:
            for target in (middle, last, original):
                _rename_handle(api, handle, target)
            assert original.exists()
            assert not middle.exists() and not last.exists()
        finally:
            assert api.CloseHandle(handle), ctypes.WinError(ctypes.get_last_error())
    _unmount(root, proc)
    bridge = HandlerBridge(image, driver, read_only=True, adf_info=detect_adf(image))
    try:
        names = {entry["name"] for entry in bridge.list_dir_path("/")}
        assert "DirA" in names and "DirB" not in names and "DirC" not in names
        if handler != "bundled":
            assert bridge.read_file("/FileA", 32, 0) == b"rename data"
            assert "FileB" not in names and "FileC" not in names
    finally:
        bridge.close()


@pytest.mark.parametrize("replacement", [False, True])
def test_nonempty_renamed_directory_refuses_delete(mounted_handler, replacement):
    from amifuse.fuse_fs import HandlerBridge
    from amifuse.rdb_inspect import detect_adf
    from tests.integration.test_bundled_ffs import _unmount

    _handler, image, driver, proc, root = mounted_handler
    old, new = root / "Before", root / "After"
    old.mkdir()
    (old / "child").write_bytes(b"keep child")
    api = _file_api()
    handle = _open_for_rename(api, old, True)
    try:
        _rename_handle(api, handle, new)
        if replacement:
            old.mkdir()
        delete = wintypes.BOOL(True)
        assert not api.SetFileInformationByHandle(handle, 4, ctypes.byref(delete),
                                                  ctypes.sizeof(delete))
        assert ctypes.get_last_error() == 145  # ERROR_DIR_NOT_EMPTY
    finally:
        assert api.CloseHandle(handle), ctypes.WinError(ctypes.get_last_error())
    _unmount(root, proc)
    bridge = HandlerBridge(image, driver, read_only=True, adf_info=detect_adf(image))
    try:
        assert bridge.read_file("/After/child", 32, 0) == b"keep child"
        names = {entry["name"] for entry in bridge.list_dir_path("/")}
        assert ("Before" in names) == replacement
    finally:
        bridge.close()


def _list_handle(api, handle):
    """Names GetFileInformationByHandleEx lists through a directory handle."""
    api.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, wintypes.DWORD]
    api.GetFileInformationByHandleEx.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(64 * 1024)
    names, info_class = [], 15  # FileFullDirectoryRestartInfo
    while True:
        if not api.GetFileInformationByHandleEx(handle, info_class, buffer,
                                                ctypes.sizeof(buffer)):
            error = ctypes.get_last_error()
            assert error == 18, ctypes.WinError(error)  # ERROR_NO_MORE_FILES
            return names
        offset = 0
        while True:
            # FILE_FULL_DIR_INFO: NextEntryOffset at 0, FileNameLength at 60,
            # FileName at 68.
            next_offset = int.from_bytes(buffer.raw[offset:offset + 4], "little")
            length = int.from_bytes(buffer.raw[offset + 60:offset + 64], "little")
            names.append(buffer.raw[offset + 68:offset + 68 + length].decode("utf-16-le"))
            if not next_offset:
                break
            offset += next_offset
        info_class = 14  # FileFullDirectoryInfo


@pytest.mark.parametrize("renames", [1, 2])
def test_directory_renamed_through_its_handle_lists_its_files(mounted_handler, renames):
    # WinFsp lists a handle's directory, and looks up each entry, under the
    # path the handle was opened with; after a rename through it, every
    # entry dropped out (#427). READDIR_PLUS gives the attributes with the
    # listing, so WinFsp no longer looks them up.
    _handler, _image, _driver, _proc, root = mounted_handler
    old = root / "Listed"
    old.mkdir()
    (old / "inside.txt").write_bytes(b"listed")
    api = _file_api()
    # FILE_LIST_DIRECTORY | DELETE | FILE_READ_ATTRIBUTES, all sharing,
    # OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS
    handle = api.CreateFileW(str(old), 0x1 | 0x10000 | 0x80, 7, None, 3,
                             0x02000000, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.WinError(ctypes.get_last_error())
    try:
        current = old
        for step in range(renames):
            current = root / f"Renamed{step}"
            _rename_handle(api, handle, current)
        assert "inside.txt" in _list_handle(api, handle)
        assert os.listdir(current) == ["inside.txt"]
    finally:
        assert api.CloseHandle(handle), ctypes.WinError(ctypes.get_last_error())
