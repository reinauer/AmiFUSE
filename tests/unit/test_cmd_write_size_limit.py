"""`amifuse write` must not leave a file larger than 0x7FFFFFFF bytes.

AmigaDOS file positions and sizes are signed LONGs (ACTION_SEEK, fib_Size),
so a longer file cannot be seeked to its end.
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
    bridge.open_file.return_value = (0x1000, 0x2000)
    bridge.write_handle.side_effect = lambda fh, data: len(data)
    created = []

    def fake_create(*a, **kw):
        created.append(a)
        return bridge, None

    monkeypatch.setattr(fs, "_create_bridge_from_args", fake_create)
    monkeypatch.setattr(fs, "_ensure_parent_dirs", lambda *a, **kw: None)

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
def test_source_above_limit_refused_before_bridge(write_context, capsys,
                                                  use_json, size):
    args, bridge, created, source, report_size = write_context
    args.json = use_json
    report_size(size)
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    if use_json:
        assert caught.value.code == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "FILE_TOO_LARGE"
        assert error["details"] == {"size": size, "max_size": 0x7FFFFFFF}
    else:
        assert "larger than 2147483647 bytes" in str(caught.value)
    assert created == []
    bridge.open_file.assert_not_called()
    bridge.write_handle.assert_not_called()


def test_source_at_limit_still_written(write_context, capsys):
    args, bridge, created, source, report_size = write_context
    report_size(0x7FFFFFFF)
    fs.cmd_write(args)
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert result["size"] == 0x7FFFFFFF
    bridge.open_file.assert_called_once_with(
        "/S/file", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    bridge.write_handle.assert_called_once_with(0x1000, b"hello")


def test_source_growing_to_limit_still_written(write_context, monkeypatch,
                                               capsys):
    """A source that grows to exactly the limit is written in full."""
    args, bridge, created, source, report_size = write_context
    source.write_bytes(b"x" * 12)
    report_size(4)
    monkeypatch.setattr(fs, "_MAX_FILE_SIZE", 12)
    monkeypatch.setattr(fs, "IO_CHUNK", 8)
    fs.cmd_write(args)
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert result["bytes_written"] == 12
    assert [c.args[1] for c in bridge.write_handle.call_args_list] == [
        b"x" * 8, b"x" * 4]


@pytest.mark.parametrize("use_json", [True, False])
def test_source_growing_past_limit_stops(write_context, monkeypatch, capsys,
                                         use_json):
    """A source that grows while it is read, or a stream, stops below the limit."""
    args, bridge, created, source, report_size = write_context
    args.json = use_json
    source.write_bytes(b"x" * 20)
    report_size(4)
    monkeypatch.setattr(fs, "_MAX_FILE_SIZE", 12)
    monkeypatch.setattr(fs, "IO_CHUNK", 8)
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    if use_json:
        assert caught.value.code == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "FILE_TOO_LARGE"
        assert error["details"]["bytes_written"] == 8
        assert error["details"]["size"] == 4
        assert "keeps the first 8 bytes" in error["message"]
    else:
        assert "holds more than 12 bytes" in str(caught.value)
        assert "keeps the first 8 bytes" in str(caught.value)
    assert [c.args[1] for c in bridge.write_handle.call_args_list] == [b"x" * 8]
    bridge.close_file.assert_called_once_with(0x1000)
    bridge.flush_volume.assert_called()
