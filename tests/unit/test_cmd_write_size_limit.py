"""`amifuse write` must not leave a file larger than the volume takes.

AmigaDOS file positions and sizes are signed LONGs (ACTION_SEEK, fib_Size),
so without the 64 bit packets a file longer than 0x7FFFFFFF bytes cannot be
seeked to its end (HandlerBridge.max_file_size).
"""

import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amifuse import fuse_fs as fs

# The named fields among the ten that list(stat_result) returns; items 7-9
# are unnamed integer times.
_STAT_SEQ = ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid",
             "st_size")


@pytest.fixture
def write_context(monkeypatch, tmp_path):
    """A small source file whose reported size a test can override."""
    source = tmp_path / "source"
    source.write_bytes(b"hello")
    args = SimpleNamespace(image=tmp_path / "image.hdf", file="S/file",
                           input=str(source), json=True, debug=False)
    bridge = Mock()
    bridge.is_mounted.return_value = fs.MountState(True, None, None)
    bridge.max_file_size.return_value = fs._MAX_FILE_SIZE
    bridge.locate_path.return_value = (0, fs.ERROR_OBJECT_NOT_FOUND, [])  # a new name
    bridge.delete_object.return_value = (-1, 0)
    bridge.open_file.return_value = (0x1000, 0x2000)
    # write opens through open_file_reply(); tests set open_file().
    bridge.open_file_reply.side_effect = lambda *a, **kw: (bridge.open_file(*a, **kw), 0)
    bridge.write_handle_reply.side_effect = lambda fh, data: (len(data), 0)
    created = []

    def fake_create(*a, **kw):
        created.append(kw.get("read_only", True))
        return bridge, None

    monkeypatch.setattr(fs, "_create_bridge_from_args", fake_create)
    monkeypatch.setattr(fs, "_ensure_parent_dirs", Mock())

    def report_size(size):
        # cmd_write takes the size from fstat() on the source it opened.
        real_fstat = os.fstat
        source_stat = os.stat(source)

        def fake_fstat(fd, *a, **kw):
            st = real_fstat(fd, *a, **kw)
            if not os.path.samestat(st, source_stat):
                return st
            fields = list(st)
            fields[6] = size  # st_size
            # Carry over the fields list(st) leaves out (float and ns times,
            # st_file_attributes, ...).
            extra = {name: getattr(st, name) for name in dir(st)
                     if name.startswith("st_") and name not in _STAT_SEQ}
            return os.stat_result(fields, extra)

        monkeypatch.setattr(os, "fstat", fake_fstat)

    return args, bridge, created, source, report_size


@pytest.mark.parametrize("use_json", [True, False])
@pytest.mark.parametrize("size", [0x80000000, 0x80010000, 1 << 32])
def test_source_above_limit_refused_before_open(write_context, capsys, monkeypatch,
                                                use_json, size):
    """A source above 2 GiB is checked against the handler through a bridge
    that only reads, and refused before it is copied."""
    args, bridge, created, source, report_size = write_context
    args.json = use_json
    report_size(size)
    monkeypatch.setattr(fs, "_spool_write_source",
                        Mock(side_effect=AssertionError("copied")))
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    assert created == [True]
    if use_json:
        assert caught.value.code == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "FILE_TOO_LARGE"
        assert error["details"] == {"size": size, "max_size": 0x7FFFFFFF}
    else:
        assert "larger than 2147483647 bytes, the most the handler takes" in str(caught.value)
    fs._ensure_parent_dirs.assert_not_called()
    bridge.open_file.assert_not_called()
    bridge.write_handle_reply.assert_not_called()


def test_source_at_limit_still_written(write_context, capsys):
    args, bridge, created, source, report_size = write_context
    report_size(0x7FFFFFFF)
    fs.cmd_write(args)
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert result["size"] == 0x7FFFFFFF
    bridge.open_file.assert_called_once_with(
        "/S/file", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    bridge.write_handle_reply.assert_called_once_with(0x1000, b"hello")


def test_source_growing_to_limit_still_written(write_context, monkeypatch,
                                               capsys):
    """A source that grows to exactly the limit is written in full."""
    args, bridge, created, source, report_size = write_context
    source.write_bytes(b"x" * 12)
    report_size(4)
    bridge.max_file_size.return_value = 12
    monkeypatch.setattr(fs, "IO_CHUNK", 8)
    fs.cmd_write(args)
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert result["bytes_written"] == 12
    assert [c.args[1] for c in bridge.write_handle_reply.call_args_list] == [
        b"x" * 8, b"x" * 4]


@pytest.mark.parametrize("use_json", [True, False])
def test_source_growing_past_limit_stops(write_context, monkeypatch, capsys,
                                         use_json):
    """A source that grows past the limit while it is read, or a stream, is
    refused by the size of its copy, before the destination is touched."""
    args, bridge, created, source, report_size = write_context
    args.json = use_json
    source.write_bytes(b"x" * 20)
    report_size(4)
    bridge.max_file_size.return_value = 12
    monkeypatch.setattr(fs, "IO_CHUNK", 8)
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    message = (f"Source file is larger than 12 bytes, the most the handler "
               f"takes: {source} (20 bytes)")
    if use_json:
        assert caught.value.code == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "FILE_TOO_LARGE"
        assert error["message"] == message
        assert error["details"] == {"size": 20, "max_size": 12}
    else:
        assert str(caught.value) == f"Error: s{message[1:]}"
    fs._ensure_parent_dirs.assert_not_called()
    bridge.open_file.assert_not_called()


def test_source_above_2_gib_written_with_the_64_bit_packets(write_context, capsys):
    args, bridge, created, source, report_size = write_context
    bridge.max_file_size.return_value = fs._MAX_FILE_SIZE64
    report_size(1 << 32)
    fs.cmd_write(args)
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert created == [True, False]
    bridge.write_handle_reply.assert_called_once_with(0x1000, b"hello")


def test_source_up_to_2_gib_asks_no_bridge_before_the_copy(write_context, capsys):
    args, bridge, created, source, report_size = write_context
    report_size(0x7FFFFFFF)
    fs.cmd_write(args)
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert created == [False]


@pytest.mark.parametrize("use_json", [True, False])
def test_source_growing_past_the_limit_stops_the_copy(write_context, monkeypatch,
                                                      capsys, use_json):
    """A copy that grows past the classic limit asks the handler, and stops
    at the limit it names, before the destination is touched."""
    args, bridge, created, source, report_size = write_context
    args.json = use_json
    source.write_bytes(b"x" * 20)
    report_size(4)
    monkeypatch.setattr(fs, "_MAX_FILE_SIZE", 8)
    monkeypatch.setattr(fs, "IO_CHUNK", 8)
    bridge.max_file_size.return_value = 12
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    message = (f"Source file grew past 12 bytes, the most the handler takes, "
               f"while it was copied: {source}")
    if use_json:
        assert caught.value.code == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "FILE_TOO_LARGE"
        assert error["message"] == message
        assert error["details"] == {"max_size": 12}
    else:
        assert str(caught.value) == f"Error: s{message[1:]}"
    assert created == [True]
    bridge.open_file.assert_not_called()


def test_source_growing_past_2_gib_copied_on_the_64_bit_packets(write_context,
                                                                monkeypatch, capsys):
    """A source that grows past the classic limit while it is copied is
    written when the handler takes more."""
    args, bridge, created, source, report_size = write_context
    source.write_bytes(b"x" * 20)
    report_size(4)
    monkeypatch.setattr(fs, "_MAX_FILE_SIZE", 8)
    monkeypatch.setattr(fs, "IO_CHUNK", 8)
    bridge.max_file_size.return_value = fs._MAX_FILE_SIZE64
    fs.cmd_write(args)
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert result["bytes_written"] == 20
    assert created == [True, False]


def test_an_unusable_volume_is_not_too_large(write_context, capsys):
    """The bridge that asks the limit refuses a volume it cannot use."""
    args, bridge, created, source, report_size = write_context
    report_size(1 << 32)
    bridge.is_mounted.return_value = fs.MountState(False, "not a DOS disk", None)
    with pytest.raises(SystemExit):
        fs.cmd_write(args)
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "NOT_MOUNTED"
    assert created == [True]
    bridge.max_file_size.assert_not_called()
