"""Creating a file at the name of one deleted while still open (#439).

The handler deletes the old file only when its last handle closes, and it
keeps its name on disk until then. On Windows the name is free as soon as
the delete succeeds, so a program that saves by deleting and writing anew
must not get an I/O error while a viewer has the old file open.
"""

import ctypes
import os
import sys

import pytest

from tests.integration.test_windows_renamed_directory import (  # noqa: F401
    _file_api, mounted_handler)

pytestmark = [pytest.mark.fuse,
              pytest.mark.skipif(sys.platform != "win32", reason="WinFsp")]


def test_create_at_the_name_of_a_file_deleted_while_open(mounted_handler):
    handler, _image, _driver, _proc, root = mounted_handler
    path = root / "Pend"
    path.write_bytes(b"old")
    api = _file_api()
    # GENERIC_READ, all sharing, OPEN_EXISTING: a viewer's handle.
    handle = api.CreateFileW(str(path), 0x80000000, 7, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.WinError(ctypes.get_last_error())
    try:
        os.remove(path)
        assert "Pend" not in os.listdir(root)
        if handler == "bundled":
            # It refuses to rename an open file, so the name is free only
            # once the old file is deleted, at its close.
            with pytest.raises(PermissionError):
                path.open("xb")
        else:
            with path.open("xb") as new:  # CREATE_NEW
                new.write(b"new")
            assert path.read_bytes() == b"new"
    finally:
        assert api.CloseHandle(handle), ctypes.WinError(ctypes.get_last_error())
    if handler == "bundled":
        path.write_bytes(b"new")
    assert path.read_bytes() == b"new"
    assert [n for n in os.listdir(root) if n.lower() == "pend"] == ["Pend"]
    assert not [n for n in os.listdir(root) if n.startswith(".amifuse-")]
