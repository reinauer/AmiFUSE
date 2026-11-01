"""What a failed `amifuse write` leaves in the image.

- The source is copied in full before the destination is opened, so a source
  that fails part way through fails the command with the image untouched.
- A write the handler fails removes a file the command created.
- An existing destination cannot be restored; the report names it.
"""

import errno
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amifuse import fuse_fs as fs

PARTIAL_NOTE = "; the existing S/file is left partial, and its old contents are gone"


@pytest.fixture
def write_context(monkeypatch, tmp_path):
    """cmd_write with a mocked bridge; the destination name is new."""
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdefghij")
    args = SimpleNamespace(image=tmp_path / "image.hdf", file="S/file",
                           input=str(source), json=True, debug=False)
    bridge = Mock()
    bridge.is_mounted.return_value = fs.MountState(True, None, None)
    bridge.locate_path.return_value = (0, fs.ERROR_OBJECT_NOT_FOUND, [])
    bridge.open_file.return_value = (0x1000, 0x2000)
    bridge.write_handle_reply.side_effect = lambda fh, data: (len(data), 0)
    bridge.delete_object.return_value = (-1, 0)
    created = []

    def fake_create(*a, **kw):
        created.append(a)
        return bridge, None

    monkeypatch.setattr(fs, "_create_bridge_from_args", fake_create)
    monkeypatch.setattr(fs, "_ensure_parent_dirs", lambda *a, **kw: None)
    monkeypatch.setattr(fs, "IO_CHUNK", 4)
    return args, bridge, created, source


class _FailingReader:
    """A source whose reads fail once `good` bytes have been read."""

    def __init__(self, f, good):
        self._f, self._good = f, good

    def read(self, size=-1):
        if self._f.tell() >= self._good:
            raise PermissionError(errno.EACCES, "Permission denied")
        return self._f.read(size)

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.mark.parametrize("use_json", [True, False], ids=["json", "text"])
@pytest.mark.parametrize("existing", [True, False], ids=["existing", "new"])
def test_source_failing_part_way_leaves_image_untouched(
        write_context, monkeypatch, capsys, use_json, existing):
    args, bridge, created, source = write_context
    args.json = use_json
    if existing:
        bridge.locate_path.return_value = (0x3000, 0, [0x3000])
    real_open = fs._open_write_source

    def open_failing(path, use_json):
        f, size = real_open(path, use_json)
        return _FailingReader(f, 4), size

    monkeypatch.setattr(fs, "_open_write_source", open_failing)
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    message = f"Cannot read source file: {source}: Permission denied"
    if use_json:
        assert caught.value.code == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "INVALID_ARGUMENT"
        assert error["message"] == message
    else:
        assert str(caught.value) == f"Error: c{message[1:]}"
    assert created == []
    bridge.open_file.assert_not_called()


def test_spool_beyond_memory_writes_every_byte(write_context, monkeypatch, capsys):
    """A source larger than the in-memory limit goes to a temporary file."""
    args, bridge, _, source = write_context
    monkeypatch.setattr(fs, "_WRITE_SPOOL_MAX", 3)
    fs.cmd_write(args)
    assert json.loads(capsys.readouterr().out)["bytes_written"] == 10
    assert [c.args[1] for c in bridge.write_handle_reply.call_args_list] == [
        b"abcd", b"efgh", b"ij"]


@pytest.mark.parametrize("use_json", [True, False], ids=["json", "text"])
@pytest.mark.parametrize("failing", ["write", "seek"])
def test_temporary_copy_failing_leaves_image_untouched(write_context, monkeypatch,
                                                       capsys, use_json, failing):
    # The seek back to the start flushes a spool on disk, and can fail too.
    args, bridge, created, _ = write_context
    args.json = use_json

    class FullSpool(tempfile.SpooledTemporaryFile):
        pass

    def full(self, *args):
        raise OSError(errno.ENOSPC, "No space left on device")

    setattr(FullSpool, failing, full)
    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", FullSpool)
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    message = ("Write operation failed: cannot copy the source to a temporary "
               "file: No space left on device")
    if use_json:
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "HANDLER_ERROR"
        assert error["message"] == message
    else:
        assert str(caught.value) == f"Error: w{message[1:]}"
    assert created == []


@pytest.mark.parametrize("use_json", [True, False], ids=["json", "text"])
@pytest.mark.parametrize("reply, report, text", [
    ((-1, fs.ERROR_DISK_FULL), "Write failed at offset 4: disk full",
     "Error: write failed at offset 4: disk full"),
    ((2, fs.ERROR_DISK_FULL), "Partial write: wrote 6 of 10 bytes (disk full)",
     "Error: partial write (6/10 bytes, disk full)"),
], ids=["failed", "short"])
def test_failed_write_removes_a_new_file(write_context, capsys, use_json, reply,
                                         report, text):
    args, bridge, _, _ = write_context
    args.json = use_json
    bridge.write_handle_reply.side_effect = [(4, 0), reply]
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    captured = capsys.readouterr()
    if use_json:
        assert json.loads(captured.out)["error"]["message"] == report
    else:
        assert str(caught.value) == text
    bridge.close_file.assert_called_once_with(0x1000)
    bridge.delete_object.assert_called_once_with(0x2000, "file")
    assert "Warning" not in captured.err


def test_failed_removal_is_reported(write_context, capsys):
    args, bridge, _, _ = write_context
    bridge.write_handle_reply.side_effect = None
    bridge.write_handle_reply.return_value = (-1, fs.ERROR_DISK_FULL)
    bridge.delete_object.return_value = (0, fs.ERROR_OBJECT_IN_USE)
    with pytest.raises(SystemExit):
        fs.cmd_write(args)
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"]["message"] == (
        "Write failed at offset 0: disk full")
    assert "Warning: cannot remove the partial file S/file" in captured.err


@pytest.mark.parametrize("use_json", [True, False], ids=["json", "text"])
def test_failed_write_names_an_existing_destination(write_context, capsys,
                                                     use_json):
    args, bridge, _, _ = write_context
    args.json = use_json
    bridge.locate_path.return_value = (0x3000, 0, [0x3000])
    bridge.write_handle_reply.side_effect = [(4, 0), (-1, fs.ERROR_DISK_FULL)]
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    report = f"Write failed at offset 4: disk full{PARTIAL_NOTE}"
    if use_json:
        assert json.loads(capsys.readouterr().out)["error"]["message"] == report
    else:
        assert str(caught.value) == f"Error: w{report[1:]}"
    bridge.free_lock.assert_called_once_with(0x3000)
    bridge.delete_object.assert_not_called()


def test_exception_names_an_existing_destination(write_context, capsys):
    args, bridge, _, _ = write_context
    bridge.locate_path.return_value = (0x3000, 0, [0x3000])
    bridge.write_handle_reply.side_effect = RuntimeError("handler crashed")
    with pytest.raises(SystemExit):
        fs.cmd_write(args)
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "HANDLER_ERROR"
    assert error["message"] == f"Write operation failed: handler crashed{PARTIAL_NOTE}"


def test_unclear_lookup_neither_removes_nor_names(write_context, capsys):
    """A lookup that got no reply says nothing about the name."""
    args, bridge, _, _ = write_context
    bridge.locate_path.return_value = (0, -1, [])
    bridge.write_handle_reply.side_effect = None
    bridge.write_handle_reply.return_value = (-1, fs.ERROR_DISK_FULL)
    with pytest.raises(SystemExit):
        fs.cmd_write(args)
    assert json.loads(capsys.readouterr().out)["error"]["message"] == (
        "Write failed at offset 0: disk full")
    bridge.delete_object.assert_not_called()


@pytest.mark.parametrize("existing", [True, False], ids=["existing", "new"])
def test_successful_write_removes_nothing(write_context, capsys, existing):
    args, bridge, _, _ = write_context
    if existing:
        bridge.locate_path.return_value = (0x3000, 0, [0x3000])
    fs.cmd_write(args)
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    bridge.delete_object.assert_not_called()


def test_failed_open_removes_nothing(write_context, capsys):
    args, bridge, _, _ = write_context
    bridge.open_file.return_value = None
    with pytest.raises(SystemExit):
        fs.cmd_write(args)
    assert json.loads(capsys.readouterr().out)["error"]["message"] == (
        "Failed to open file for writing: S/file")
    bridge.delete_object.assert_not_called()
