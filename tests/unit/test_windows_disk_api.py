"""Check the actual ctypes boundary without accessing a physical disk."""

import ctypes
import struct
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amitools.util import Win32Disk as win


@pytest.fixture
def native(monkeypatch):
    names = ("CreateFileW", "CloseHandle", "DeviceIoControl", "SetFilePointerEx",
             "ReadFile", "WriteFile", "FlushFileBuffers", "FindFirstVolumeW",
             "FindNextVolumeW", "FindVolumeClose")
    dll = SimpleNamespace(**{name: Mock(return_value=1) for name in names})
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=dll), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, "FormatError", lambda code: "access denied", raising=False)
    return win._Win32(), dll


def test_createfile_abi_and_open_existing(native):
    api, dll = native
    handle = 0x123456789
    dll.CreateFileW.return_value = handle
    assert api.open(r"\\.\PhysicalDrive2", api.GENERIC_READ, share=0) == handle
    dll.CreateFileW.assert_called_once_with(
        r"\\.\PhysicalDrive2", 0x80000000, 0, None, 3, 0, None)
    assert dll.CreateFileW.restype is ctypes.c_void_p
    assert dll.CloseHandle.argtypes == [ctypes.c_void_p]
    assert dll.SetFilePointerEx.argtypes[1] is ctypes.c_int64
    dll.CreateFileW.return_value = ctypes.c_void_p(-1).value
    with pytest.raises(OSError, match="Administrator") as exc:
        api.open("missing", api.GENERIC_READ)
    assert exc.value.winerror == 5


def test_read_write_use_64_bit_offsets_and_exact_lengths(native):
    api, dll = native

    def read(handle, buf, size, count, overlapped):
        ctypes.memmove(buf, b"a" * size, size)
        count._obj.value = size
        return 1

    def write(handle, buf, size, count, overlapped):
        assert ctypes.string_at(buf, size) == b"x" * 512
        count._obj.value = size
        return 1

    dll.ReadFile.side_effect = read
    dll.WriteFile.side_effect = write
    offset = 5 * 1024**3
    assert api.read(123, offset, 512) == b"a" * 512
    api.write(123, offset, b"x" * 512)
    dll.SetFilePointerEx.assert_called_with(123, offset, None, 0)
    dll.ReadFile.side_effect = None  # successful API call but zero bytes read
    with pytest.raises(IOError, match="short physical disk read"):
        api.read(123, offset, 512)
    dll.WriteFile.side_effect = None
    with pytest.raises(IOError, match="short physical disk write"):
        api.write(123, offset, b"x" * 512)


def test_ioctl_respects_returned_length(native):
    api, dll = native

    def ioctl(handle, code, inbuf, insize, outbuf, outsize, count, overlapped):
        assert inbuf is None and insize == 0
        ctypes.memmove(outbuf, b"sizehere", 8)
        count._obj.value = 8
        return 1

    dll.DeviceIoControl.side_effect = ioctl
    assert api.ioctl(1, api.GET_LENGTH, 24) == b"sizehere"
    dll.DeviceIoControl.side_effect = None
    dll.DeviceIoControl.return_value = 0
    with pytest.raises(OSError, match="disk control"):
        api.ioctl(1, api.LOCK_VOLUME)


def test_volume_extents_retry_and_native_padding(native):
    api, _ = native
    more = OSError("more data")
    more.winerror = 234
    data = struct.pack("<I4x", 2)
    data += struct.pack("<I4xqq", 0, 1024, 2048)
    data += struct.pack("<I4xqq", 2, 4096, 8192)
    api.ioctl = Mock(side_effect=[more, data])
    assert api.volume_disks(42) == {0, 2}
    assert [call.args[2] for call in api.ioctl.call_args_list] == [32, 64]
    api.ioctl = Mock(return_value=data[:-1])
    with pytest.raises(IOError, match="truncated"):
        api.volume_disks(42)


@pytest.mark.parametrize("early_close", [False, True])
def test_volume_enumeration_closes_search_handle(native, monkeypatch, early_close):
    api, dll = native
    volume = "\\\\?\\Volume{test}\\"

    def first(buf, size):
        buf.value = volume
        return 456

    dll.FindFirstVolumeW.side_effect = first
    dll.FindNextVolumeW.return_value = 0
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 18)
    volumes = api.volumes()
    assert next(volumes) == volume[:-1]
    if early_close:
        volumes.close()
    else:
        assert list(volumes) == []
    dll.FindVolumeClose.assert_called_once_with(456)


def test_flush_and_seek_errors_are_not_ignored(native):
    api, dll = native
    dll.FlushFileBuffers.return_value = 0
    with pytest.raises(OSError, match="flush"):
        api.flush(1)
    dll.SetFilePointerEx.return_value = 0
    with pytest.raises(OSError, match="seek"):
        api.read(1, 0, 512)
    dll.ReadFile.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows APIs")
def test_native_file_handle_io(tmp_path):
    # Exercises the real ctypes signatures in Windows CI, using only a
    # disposable file. Disk geometry/volume controls need a hardware test.
    path = tmp_path / "native-io.bin"
    path.write_bytes(b"a" * 4096)
    api = win._Win32()
    handle = api.open(str(path), api.GENERIC_READ | api.GENERIC_WRITE, share=0)
    try:
        assert api.read(handle, 512, 512) == b"a" * 512
        api.write(handle, 1024, b"b" * 512)
        api.flush(handle)
        assert api.read(handle, 1024, 512) == b"b" * 512
    finally:
        api.close(handle)
    assert path.read_bytes() == b"a" * 1024 + b"b" * 512 + b"a" * 2560
