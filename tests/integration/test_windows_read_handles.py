"""Windows attribute opens and rename probes must not lock Amiga files."""

import os
from pathlib import Path
import shutil
import sys

import pytest

from tests.integration.test_bundled_ffs import ROOT_DATA, _seed_adf, _unmount, _xdf_read

pytestmark = [pytest.mark.fuse,
              pytest.mark.skipif(sys.platform != "win32", reason="WinFsp")]


@pytest.mark.parametrize("handler", ["bundled", "pfs3aio"])
def test_stat_and_read_while_writing(mount_image, tmp_path, fixture_root, handler):
    if handler == "bundled":
        image, driver = _seed_adf(tmp_path, "DOS3"), None
    else:
        image = tmp_path / "pfs.hdf"
        shutil.copy2(fixture_root / "fixtures/readonly/pfs.hdf", image)
        driver = fixture_root / "drivers/pfs3aio"
    _proc, root = mount_image(image, driver=driver, extra_args=["--write"])
    path = Path(root) / "busy.bin"
    payload = b"first chunk" * 8192
    with open(path, "wb", buffering=0) as writer:
        writer.write(payload)
        assert os.stat(path).st_size == len(payload)
        with open(path, "rb", buffering=0) as reader:
            assert reader.read(17) == payload[:17]
            writer.write(b"tail")
            assert os.stat(path).st_size == len(payload) + 4
            reader.seek(len(payload))
            assert reader.read() == b"tail"
    assert path.read_bytes() == payload + b"tail"


def test_bundled_file_rename(mount_image, tmp_path):
    image = _seed_adf(tmp_path, "DOS3")
    proc, root = mount_image(image, extra_args=["--write"])
    root = Path(root)
    payload = (root / "Root.txt").read_bytes()
    os.rename(root / "Root.txt", root / "Renamed.txt")
    (root / "Target.txt").write_bytes(b"old")
    os.replace(root / "Renamed.txt", root / "Target.txt")
    assert (root / "Target.txt").read_bytes() == payload
    _unmount(root, proc)
    assert _xdf_read(image, "Target.txt", tmp_path) == payload


@pytest.mark.parametrize("replace", [False, True])
def test_open_reader_survives_delete_or_refused_replace(mount_image, tmp_path, replace):
    import ctypes
    from ctypes import wintypes

    image = _seed_adf(tmp_path, "DOS3")
    _proc, root = mount_image(image, extra_args=["--write"])
    root = Path(root)
    old = root / "Root.txt"
    payload = ROOT_DATA
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    api.ReadFile.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    # Share deletion explicitly; Python's open() does not request this.
    handle = api.CreateFileW(str(old), 0x80000000, 7, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.WinError()
    try:
        if replace:
            source = root / "New.txt"
            source.write_bytes(b"replacement")
            # WinFsp follows the Windows non-POSIX rename rule: an open
            # destination cannot be replaced, even with delete sharing.
            with pytest.raises(PermissionError) as caught:
                os.replace(source, old)
            assert caught.value.winerror == 5
            assert source.read_bytes() == b"replacement"
        else:
            os.remove(old)
        buffer = ctypes.create_string_buffer(len(payload) + 1)
        count = wintypes.DWORD()
        assert api.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None), ctypes.WinError()
        assert buffer.raw[:count.value] == payload
    finally:
        assert api.CloseHandle(handle), ctypes.WinError()
    if replace:
        os.replace(source, old)
        assert old.read_bytes() == b"replacement"
    else:
        assert not old.exists()
