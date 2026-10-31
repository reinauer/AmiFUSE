"""`amifuse write`'s error reports.

- A write the handler fails names its AmigaDOS error code (dp_Res2); only a
  write that got no reply says "no reply from handler".
- A source whose existence or type cannot be checked is an argument error,
  not a traceback.
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amifuse import fuse_fs as fs

SOURCE_DATA = b"hello"


@pytest.fixture
def write_context(monkeypatch, tmp_path):
    """cmd_write with a mocked bridge; records whether a bridge was made."""
    source = tmp_path / "source.bin"
    source.write_bytes(SOURCE_DATA)
    args = SimpleNamespace(image=tmp_path / "image.hdf", file="S/file",
                           input=str(source), json=True, debug=False)
    bridge = Mock()
    bridge.is_mounted.return_value = fs.MountState(True, None, None)
    bridge.open_file.return_value = (0x1000, 0x2000)
    created = []

    def fake_create(*a, **kw):
        created.append(a)
        return bridge, None

    monkeypatch.setattr(fs, "_create_bridge_from_args", fake_create)
    monkeypatch.setattr(fs, "_ensure_parent_dirs", lambda *a, **kw: None)
    return args, bridge, created


@pytest.mark.parametrize("use_json", [True, False], ids=["json", "text"])
@pytest.mark.parametrize("res2, reason", [
    (221, "disk full"),
    (214, "disk is write-protected"),
    (223, "file is write-protected"),
    (225, "error code 225"),
    (0, "no reply from handler"),
])
def test_failed_write_names_the_handler_error(write_context, capsys, use_json,
                                              res2, reason):
    args, bridge, _ = write_context
    args.json = use_json
    bridge.write_handle_reply.return_value = (-1, res2)
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    if use_json:
        assert caught.value.code == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "WRITE_ERROR"
        assert error["message"] == f"Write failed at offset 0: {reason}"
        expected = {"filesystem_responsive": True, "bytes_written": 0,
                    "expected": len(SOURCE_DATA)}
        if res2:
            expected["dos_error"] = res2
        assert error["details"] == expected
    else:
        assert str(caught.value) == f"Error: write failed at offset 0: {reason}"
    bridge.close_file.assert_called_once_with(0x1000)
    bridge.flush_volume.assert_called_once()


@pytest.mark.parametrize("use_json", [True, False], ids=["json", "text"])
@pytest.mark.parametrize("written, res2, reason", [
    (0, 214, "disk is write-protected"),  # a refusal with DOSFALSE
    (2, 221, "disk full"),
    (2, 225, "error code 225"),
    (2, 0, "disk full?"),
])
def test_short_write_names_the_handler_error(write_context, capsys, use_json,
                                             written, res2, reason):
    args, bridge, _ = write_context
    args.json = use_json
    bridge.write_handle_reply.return_value = (written, res2)
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    total = len(SOURCE_DATA)
    if use_json:
        assert caught.value.code == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "WRITE_ERROR"
        assert error["message"] == f"Partial write: wrote {written} of {total} bytes ({reason})"
        expected = {"filesystem_responsive": True, "bytes_written": written,
                    "expected": total}
        if res2:
            expected["dos_error"] = res2
        assert error["details"] == expected
    else:
        assert str(caught.value) == f"Error: partial write ({written}/{total} bytes, {reason})"


def _unreadable(*a, **kw):
    raise OSError(22, "Invalid argument")


@pytest.mark.parametrize("use_json", [True, False], ids=["json", "text"])
@pytest.mark.parametrize("check", ["exists", "is_dir"])
def test_source_check_error_is_an_argument_error(write_context, monkeypatch,
                                                 capsys, use_json, check):
    args, bridge, created = write_context
    args.json = use_json
    monkeypatch.setattr(Path, check, _unreadable)
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    message = f"Cannot read source file: {Path(args.input)}: Invalid argument"
    if use_json:
        assert caught.value.code == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "INVALID_ARGUMENT"
        assert error["message"] == message
    else:
        assert str(caught.value) == "Error: c" + message[1:]
    assert created == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipes")
@pytest.mark.skipif(sys.version_info >= (3, 14),
                    reason="Path.exists() swallows the error from Python 3.14")
def test_busy_named_pipe_source_is_an_argument_error(write_context, capsys):
    """The issue's repro: a one-instance pipe whose instance the existence
    check takes, so the type check finds every instance busy."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateNamedPipeW.restype = wintypes.HANDLE
    k32.CreateNamedPipeW.argtypes = (
        [wintypes.LPCWSTR] + [wintypes.DWORD] * 6 + [wintypes.LPVOID])
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    name = rf"\\.\pipe\amifuse-test-write-source-{os.getpid()}"
    pipe_access_duplex, one_instance = 3, 1
    handle = k32.CreateNamedPipeW(name, pipe_access_duplex, 0, one_instance,
                                  4096, 4096, 0, None)
    assert handle not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
    args, bridge, created = write_context
    args.input = name
    try:
        with pytest.raises(SystemExit) as caught:
            fs.cmd_write(args)
    finally:
        k32.CloseHandle(handle)
    assert caught.value.code == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    assert error["message"].startswith(f"Cannot read source file: {Path(name)}: ")
    assert created == []
