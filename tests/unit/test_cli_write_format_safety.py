"""`amifuse write` and `amifuse format` keep the image and the host safe.

- write opens its source before the image is touched: opening the
  destination with O_TRUNC (ACTION_FINDOUTPUT) empties an existing file, so
  a source that cannot be read, or one that is not a regular file, fails
  first with an argument error.
- format fails when the handler refuses ACTION_FLUSH or does not answer it,
  and fsyncs the image before it reports "Format complete". SmartFilesystem
  answers the flush after a format with ERROR_ACTION_NOT_KNOWN; that passes.
- format removes the handler it extracted from the RDB on every exit, and a
  failed removal does not replace the error the format exits with.
"""

import errno
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from amifuse import fuse_fs as fs

SOURCE_DATA = b"new contents"


# ---------------------------------------------------------------------------
# write: the source is opened before the bridge exists
# ---------------------------------------------------------------------------


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
    bridge.write_handle_reply.side_effect = lambda fh, data: (len(data), 0)
    created = []

    def fake_create(*a, **kw):
        created.append(a)
        return bridge, None

    monkeypatch.setattr(fs, "_create_bridge_from_args", fake_create)
    monkeypatch.setattr(fs, "_ensure_parent_dirs", lambda *a, **kw: None)
    return args, bridge, created, source


def _hold_unreadable(path: Path):
    """Make path unreadable to this process; returns an undo function.

    Windows: another handle holds it open without read sharing, as another
    program can. Elsewhere: its mode is 000.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        generic_read, no_sharing, open_existing = 0x80000000, 0, 3
        handle = k32.CreateFileW(str(path), generic_read, no_sharing, None,
                                 open_existing, 0, None)
        assert handle not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
        return lambda: k32.CloseHandle(handle)
    if os.geteuid() == 0:
        pytest.skip("root reads a file of mode 000")
    path.chmod(0)
    return lambda: path.chmod(0o644)


def _assert_refused(caught, capsys, use_json, code, text):
    if use_json:
        assert caught.value.code == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == code
        assert text in error["message"]
    else:
        assert text.lower() in str(caught.value).lower()


@pytest.mark.parametrize("use_json", [True, False], ids=["json", "text"])
def test_unreadable_source_refused_before_bridge(write_context, capsys, use_json):
    args, bridge, created, source = write_context
    args.json = use_json
    undo = _hold_unreadable(source)
    try:
        with pytest.raises(SystemExit) as caught:
            fs.cmd_write(args)
    finally:
        undo()
    _assert_refused(caught, capsys, use_json, "INVALID_ARGUMENT",
                    f"Cannot read source file: {source}")
    assert created == []
    bridge.open_file.assert_not_called()


@pytest.mark.parametrize("use_json", [True, False], ids=["json", "text"])
def test_device_source_refused_before_bridge(write_context, capsys, use_json):
    """A character device reports a size of 0, then may stream forever."""
    args, bridge, created, _ = write_context
    args.json = use_json
    args.input = os.devnull
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    _assert_refused(caught, capsys, use_json, "INVALID_ARGUMENT",
                    "Source is not a regular file")
    assert created == []
    bridge.open_file.assert_not_called()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
@pytest.mark.timeout(10)
def test_fifo_source_refused_without_blocking(write_context, capsys, tmp_path):
    """A FIFO with no writer would block a plain open() for good."""
    args, bridge, created, _ = write_context
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    args.input = str(fifo)
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    _assert_refused(caught, capsys, True, "INVALID_ARGUMENT",
                    "Source is not a regular file")
    assert created == []
    bridge.open_file.assert_not_called()


def test_source_removed_after_checks_refused_before_bridge(
        write_context, capsys, monkeypatch):
    """A source deleted after the existence check is a missing source.

    The source is opened before its size is read, so a source removed in
    between is SOURCE_NOT_FOUND, not an unhandled stat() error. The
    destination loss itself is shown by the integration test with an
    unreadable source.
    """
    args, bridge, created, source = write_context
    real_is_dir = Path.is_dir

    def is_dir_then_delete(self):
        result = real_is_dir(self)
        if self == source:
            source.unlink()
        return result

    monkeypatch.setattr(Path, "is_dir", is_dir_then_delete)
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    _assert_refused(caught, capsys, True, "SOURCE_NOT_FOUND",
                    f"Source file not found: {source}")
    assert created == []
    bridge.open_file.assert_not_called()


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows cannot replace a file that is open")
def test_source_replaced_after_open_is_not_copied(write_context, capsys, tmp_path,
                                                  monkeypatch):
    """The write loop reads the file that was checked, not the path again."""
    args, bridge, created, source = write_context
    other = tmp_path / "other.bin"
    other.write_bytes(b"replacement")
    bridge_for = fs._create_bridge_from_args

    def replace_then_create(*a, **kw):
        # The handler starts in this window, which takes seconds.
        os.replace(other, source)
        return bridge_for(*a, **kw)

    monkeypatch.setattr(fs, "_create_bridge_from_args", replace_then_create)
    fs.cmd_write(args)
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert [c.args[1] for c in bridge.write_handle_reply.call_args_list] == [SOURCE_DATA]


@pytest.mark.skipif(sys.platform != "win32",
                    reason="byte-range locks are advisory elsewhere")
@pytest.mark.parametrize("use_json", [True, False], ids=["json", "text"])
def test_locked_source_refused_before_bridge(write_context, capsys, use_json):
    """Another handle's byte-range lock lets the open succeed, but not a read."""
    import msvcrt

    args, bridge, created, source = write_context
    args.json = use_json
    with open(source, "rb") as other:
        msvcrt.locking(other.fileno(), msvcrt.LK_NBLCK, len(SOURCE_DATA))
        try:
            with pytest.raises(SystemExit) as caught:
                fs.cmd_write(args)
        finally:
            other.seek(0)
            msvcrt.locking(other.fileno(), msvcrt.LK_UNLCK, len(SOURCE_DATA))
    _assert_refused(caught, capsys, use_json, "INVALID_ARGUMENT",
                    f"Cannot read source file: {source}")
    assert created == []
    bridge.open_file.assert_not_called()


@pytest.mark.skipif(not hasattr(os, "O_NONBLOCK"), reason="needs O_NONBLOCK")
def test_source_is_read_in_blocking_mode(write_context):
    """O_NONBLOCK is only for the open: a non-blocking read can return None,
    which the write loop would take for the end of the file."""
    _, _, _, source = write_context
    f, size = fs._open_write_source(source, True)
    with f:
        assert os.get_blocking(f.fileno())
        assert size == len(SOURCE_DATA)
        assert f.read() == SOURCE_DATA


# ---------------------------------------------------------------------------
# format: flush reply, fsync, and the extracted handler
# ---------------------------------------------------------------------------

INHIBIT_PKT, FORMAT_PKT, FLUSH_PKT = 0x100, 0x200, 0x300
ACK = (-1, 0)
NAK = (0, 0)
NOT_KNOWN = (0, fs.ERROR_ACTION_NOT_KNOWN)  # SmartFilesystem, after a format
DOSTYPE = 0x50465303


class FakeBackend:
    def __init__(self, events, close_error=None, fsync_error=None):
        self.events = events
        self.close_error = close_error
        self.fsync_error = fsync_error

    def sync(self):
        self.events.append("sync")

    def fsync(self):
        self.events.append("fsync")
        if self.fsync_error is not None:
            raise self.fsync_error

    def close(self):
        self.events.append("close")
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture
def format_context(monkeypatch, tmp_path):
    """format_volume on a real HandlerBridge object with a scripted handler.

    The bridge's close() is the real one; the packets' replies and the
    backend are fakes. The RDB holds an extracted handler.
    """
    image = tmp_path / "test.hdf"
    image.write_bytes(b"")
    handler = tmp_path / "amifuse_test.handler"
    handler.write_bytes(b"handler")
    events = []
    ctx = SimpleNamespace(image=image, handler=handler, events=events,
                          flush_reply=ACK, close_error=None, fsync_error=None,
                          waits={})

    monkeypatch.setattr(fs, "get_partition_info",
                        lambda image, block_size, partition: {"dostype": DOSTYPE})
    monkeypatch.setattr(fs, "extract_embedded_driver",
                        lambda image, block_size, partition: (handler, "PFS3", DOSTYPE))

    real_print = print

    def logged_print(*a, **kw):
        if kw.get("file") is None:
            events.append(("print", " ".join(str(x) for x in a)))
        real_print(*a, **kw)

    monkeypatch.setattr(fs, "print", logged_print, raising=False)

    real_bridge = fs.HandlerBridge

    def make_bridge(*a, **kw):
        bridge = real_bridge.__new__(real_bridge)
        bridge._debug = False
        bridge._lock = threading.RLock()
        bridge.state = SimpleNamespace(crashed=False)
        bridge.vh = None
        bridge.backend = FakeBackend(events, ctx.close_error, ctx.fsync_error)
        launcher = MagicMock()
        launcher.alloc_bstr.return_value = (None, 0x1234)
        launcher.send_inhibit.return_value = (INHIBIT_PKT, 0)
        launcher.send_format.return_value = (FORMAT_PKT, 0)
        launcher.send_flush.return_value = (FLUSH_PKT, 0)
        bridge.launcher = launcher
        replies = {INHIBIT_PKT: ACK, FORMAT_PKT: ACK}

        def run_until_reply(pkt_addr, label, **wait):
            ctx.waits[pkt_addr] = wait
            if pkt_addr == FLUSH_PKT:
                events.append("flush reply")
                return ctx.flush_reply
            return replies[pkt_addr]

        bridge._run_until_reply = run_until_reply
        return bridge

    monkeypatch.setattr(fs, "HandlerBridge", MagicMock(side_effect=make_bridge))
    ctx.run = lambda: fs.format_volume(image, None, None, "DH0")
    return ctx


def _complete_printed(events):
    return any(e[0] == "print" and e[1].startswith("Format complete")
               for e in events if isinstance(e, tuple))


@pytest.mark.parametrize("reply", [
    ACK, NOT_KNOWN,
    (0, fs.ERROR_DEVICE_NOT_MOUNTED), (0, fs.ERROR_NOT_A_DOS_DISK), (0, fs.ERROR_NO_DISK),
], ids=["DOSTRUE", "ACTION_NOT_KNOWN", "DEVICE_NOT_MOUNTED", "NOT_A_DOS_DISK", "NO_DISK"])
def test_format_fsyncs_after_the_flush_reply_before_complete(format_context, reply):
    """The flush is sent while the volume is still inhibited: a refusal that
    says there is no volume passes, as SmartFilesystem's does."""
    ctx = format_context
    ctx.flush_reply = reply
    ctx.run()
    tail = ctx.events[-5:]
    assert tail[:3] == ["flush reply", "sync", "fsync"]
    assert tail[3][0] == "print" and tail[3][1].startswith("Format complete.")
    assert tail[4] == "close"
    # format's longer wait reaches the flush.
    assert ctx.waits[FLUSH_PKT] == {"max_iters": 500, "cycles": 2_000_000}
    assert not ctx.handler.exists()


@pytest.mark.parametrize("reply, message", [
    (NAK, "Format failed: handler refused ACTION_FLUSH (error code 0)"),
    ((0, 214), "Format failed: handler refused ACTION_FLUSH (error code 214)"),
    (None, "Format failed: no reply to ACTION_FLUSH from handler."),
], ids=["DOSFALSE", "write-protected", "no-reply"])
def test_format_fails_when_the_flush_is_not_acknowledged(format_context, reply,
                                                         message):
    ctx = format_context
    ctx.flush_reply = reply
    with pytest.raises(SystemExit) as caught:
        ctx.run()
    assert str(caught.value) == message
    assert "fsync" not in ctx.events
    assert not _complete_printed(ctx.events)
    assert "close" in ctx.events
    assert not ctx.handler.exists()


def test_format_fails_when_the_image_fsync_fails(format_context):
    ctx = format_context
    ctx.fsync_error = OSError(errno.EIO, "Input/output error")
    with pytest.raises(SystemExit) as caught:
        ctx.run()
    assert str(caught.value) == f"Format failed: {ctx.fsync_error}"
    assert caught.value.__cause__ is ctx.fsync_error
    assert not _complete_printed(ctx.events)


def test_format_removes_extracted_handler_when_close_fails(format_context):
    ctx = format_context
    ctx.close_error = OSError(errno.EIO, "backend close failed")
    with pytest.raises(OSError) as caught:
        ctx.run()
    assert caught.value is ctx.close_error
    assert not ctx.handler.exists()


@pytest.fixture
def unremovable_handler(format_context, monkeypatch):
    """Removing the extracted handler fails, as a locked file does on Windows."""
    handler = format_context.handler
    real_unlink = Path.unlink

    def unlink(self, *a, **kw):
        if self == handler:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)
    return handler


def test_failed_removal_keeps_the_format_error(format_context, unremovable_handler,
                                               capsys):
    ctx = format_context
    ctx.flush_reply = NAK
    with pytest.raises(SystemExit) as caught:
        ctx.run()
    assert str(caught.value).startswith("Format failed:")
    assert f"could not remove {unremovable_handler}" in capsys.readouterr().err


def test_failed_removal_after_a_format_is_reported(format_context, unremovable_handler,
                                                    capsys):
    ctx = format_context
    ctx.run()
    out = capsys.readouterr()
    assert "Format complete." in out.out
    assert f"could not remove {unremovable_handler}" in out.err
