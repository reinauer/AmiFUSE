"""File commands must diagnose mount failure before resolving a path."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amifuse import fuse_fs as fs


@pytest.fixture
def command_context(monkeypatch, tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.write_bytes(b"hello")
    output.write_bytes(b"keep existing output")
    args = SimpleNamespace(image=tmp_path / "image.adf", file="S/file",
                           json=True, input=str(source), out=str(output))
    bridge = Mock()
    bridge.is_mounted.return_value = fs.MountState(True, None, None)
    monkeypatch.setattr(fs, "_create_bridge_from_args", lambda *a, **kw: (bridge, None))
    return args, bridge, output


@pytest.mark.parametrize("command", ["hash", "read", "write"])
@pytest.mark.parametrize("path", ["/", "S/file"])
@pytest.mark.parametrize("use_json", [True, False])
@pytest.mark.parametrize("state,details", [
    (fs.MountState(False, {"disk_type": 0x4E444F53, "volume_node": 0}, "not a DOS disk"),
     {"disk_type": 0x4E444F53, "volume_node": 0}),
    (fs.MountState(False, None, "device not mounted", 218), {"dos_error": 218}),
])
def test_unmounted_rejected_before_file_operations(command_context, capsys, command, path, use_json, state, details):
    args, bridge, output = command_context
    args.file, args.json = path, use_json
    bridge.is_mounted.return_value = state
    with pytest.raises(SystemExit) as caught:
        getattr(fs, "cmd_" + command)(args)
    captured = capsys.readouterr()
    if use_json:
        assert caught.value.code == 1
        result = json.loads(captured.out)
        assert result["command"] == command
        assert result["error"]["code"] == "NOT_MOUNTED"
        assert result["error"]["details"] == details
    else:
        assert state.reason in str(caught.value)
        assert "no usable volume mounted" in str(caught.value)
    bridge.stat_path.assert_not_called()
    bridge.open_file.assert_not_called()
    bridge.create_dir.assert_not_called()
    bridge.write_handle.assert_not_called()
    bridge.flush_volume.assert_not_called()
    bridge.backend.close.assert_called_once()
    assert output.read_bytes() == b"keep existing output"


@pytest.mark.parametrize("command", ["hash", "read", "write"])
def test_unexpected_failure_keeps_mount_context(command_context, capsys, command):
    args, bridge, output = command_context
    args.file = "file"
    bridge.is_mounted.return_value = fs.MountState(None, None, "no disk info")
    bridge.stat_path.return_value = {"dir_type": -3, "size": 5}
    bridge.open_file.side_effect = RuntimeError("handler crashed")
    with pytest.raises(SystemExit) as caught:
        getattr(fs, "cmd_" + command)(args)
    assert caught.value.code == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "HANDLER_ERROR"
    assert "handler crashed" in error["message"]
    assert error["details"] == {"filesystem_responsive": None,
                                "mount_state_reason": "no disk info"}
    bridge.backend.close.assert_called_once()
    assert output.read_bytes() == b"keep existing output"


@pytest.mark.parametrize("command", ["hash", "read"])
@pytest.mark.parametrize("mounted", [True, None])
@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("use_json", [True, False])
def test_path_failures_keep_mount_context(command_context, capsys, command, mounted, directory, use_json):
    args, bridge, _ = command_context
    args.json = use_json
    reason = "handler did not report disk info" if mounted is None else None
    bridge.is_mounted.return_value = fs.MountState(mounted, None, reason)
    bridge.stat_path.return_value = {"dir_type": 2} if directory else None
    with pytest.raises(SystemExit):
        getattr(fs, "cmd_" + command)(args)
    captured = capsys.readouterr()
    if use_json:
        error = json.loads(captured.out)["error"]
        expected_code = ("IS_DIRECTORY" if command == "read" else "INVALID_ARGUMENT") if directory else "FILE_NOT_FOUND"
        assert error["code"] == expected_code
        assert error["details"]["filesystem_responsive"] is mounted
        assert error["details"].get("mount_state_reason") == reason
    else:
        assert ("mount state unknown" in captured.err) == (mounted is None)


@pytest.mark.parametrize("command", ["hash", "read", "write"])
def test_unknown_mount_state_does_not_block_success(command_context, capsys, command):
    args, bridge, output = command_context
    args.file = "file"
    bridge.is_mounted.return_value = fs.MountState(None, None, "no disk info")
    bridge.stat_path.return_value = {"dir_type": -3, "size": 5}
    bridge.open_file.return_value = (123, 0)
    bridge.read_handle.return_value = b"hello"
    bridge.write_handle.return_value = 5
    getattr(fs, "cmd_" + command)(args)
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert result["filesystem_responsive"] is None
    bridge.close_file.assert_called_once_with(123)
    if command == "read":
        assert output.read_bytes() == b"hello"
    if command == "write":
        bridge.flush_volume.assert_called_once()


@pytest.mark.parametrize("failure", ["parent", "open", "write"])
def test_unknown_write_failure_keeps_context_and_cleanup(command_context, capsys, failure):
    args, bridge, _ = command_context
    bridge.is_mounted.return_value = fs.MountState(None, None, "no disk info")
    bridge.stat_path.return_value = None
    bridge.locate_path.return_value = (0, None, [])
    bridge.locate.return_value = (0, 0)
    bridge.create_dir.return_value = (0, 218)
    if failure != "parent":
        args.file = "file"
    bridge.open_file.return_value = None if failure == "open" else (123, 0)
    bridge.write_handle.return_value = -1
    with pytest.raises(SystemExit):
        fs.cmd_write(args)
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == ("WRITE_ERROR" if failure == "write" else "HANDLER_ERROR")
    assert error["details"]["filesystem_responsive"] is None
    assert error["details"]["mount_state_reason"] == "no disk info"
    if failure == "write":
        assert error["details"]["bytes_written"] == 0
        assert error["details"]["expected"] == 5
    bridge.flush_volume.assert_called_once()
    bridge.backend.close.assert_called_once()
