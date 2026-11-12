"""File commands must diagnose mount failure before resolving a path."""

import json
import threading
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
    # write's destination name is new
    bridge.locate_path.return_value = (0, fs.ERROR_OBJECT_NOT_FOUND, [])
    bridge.delete_object.return_value = (-1, 0)
    # write opens through open_file_reply(); tests set open_file().
    bridge.open_file_reply.side_effect = lambda *a, **kw: (bridge.open_file(*a, **kw), 0)
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
    bridge.write_handle_reply.assert_not_called()
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
    bridge.write_handle_reply.return_value = (5, 0)
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
    bridge.write_handle_reply.return_value = (-1, 0)
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


def _crash(bridge):
    bridge.state.crashed = True


@pytest.mark.parametrize("command", ["ls", "verify", "hash", "read", "write"])
@pytest.mark.parametrize("use_json", [True, False])
@pytest.mark.parametrize("when", ["startup", "disk-info"])
def test_crashed_handler_rejected_before_file_operations(command_context, capsys, command, use_json, when):
    """A crash is an error, not an empty volume or a missing file."""
    args, bridge, output = command_context
    args.json = use_json
    if command == "verify":
        args.file, args.expect_size = None, None
    bridge.is_mounted.return_value = fs.MountState(
        None, None, "handler did not report disk info")
    if when == "startup":
        _crash(bridge)
    else:
        def is_mounted(**kwargs):
            _crash(bridge)
            return fs.MountState(None, None, "handler did not report disk info")
        bridge.is_mounted.side_effect = is_mounted
    with pytest.raises(SystemExit) as caught:
        getattr(fs, "cmd_" + command)(args)
    captured = capsys.readouterr()
    if use_json:
        assert caught.value.code == 1
        result = json.loads(captured.out)
        assert result["command"] == command
        assert result["error"]["code"] == "HANDLER_ERROR"
        assert result["error"]["message"] == "Filesystem handler crashed"
    else:
        assert str(caught.value) == "Error: filesystem handler crashed"
        assert captured.out == ""
    bridge.list_dir_path.assert_not_called()
    bridge.stat_path.assert_not_called()
    bridge.open_file.assert_not_called()
    bridge.create_dir.assert_not_called()
    bridge.write_handle_reply.assert_not_called()
    bridge.flush_volume.assert_not_called()
    bridge.volume_name.assert_not_called()
    bridge.backend.close.assert_called_once()
    assert output.read_bytes() == b"keep existing output"


@pytest.mark.parametrize("command,file,path,recursive,crashes", [
    ("ls", None, "/", False, "list_dir_path"),
    ("ls", None, "/", True, "list_dir_path"),
    ("ls", None, "/S", False, "stat_path"),
    ("verify", None, "/", False, "list_dir_path"),
    ("verify", "S/file", "/", False, "stat_path"),
    ("hash", "S/file", "/", False, "stat_path"),
    ("read", "S/file", "/", False, "stat_path"),
], ids=["ls", "ls-recursive", "ls-path", "verify", "verify-file", "hash", "read"])
@pytest.mark.parametrize("use_json", [True, False])
def test_crash_during_the_command_is_an_error(command_context, capsys, command, file, path,
                                              recursive, crashes, use_json):
    """A crash after the disk info is no cut-short listing or missing file."""
    args, bridge, output = command_context
    args.json, args.file, args.path, args.recursive = use_json, file, path, recursive
    args.expect_size = None
    bridge.list_dir_path.return_value = []

    def crash(*a, **kwargs):
        # The handler crashes partway through: a cut-short listing, no stat.
        _crash(bridge)
        if crashes == "list_dir_path":
            return [{"name": "Startup-Sequence", "dir_type": -3, "size": 5}]
        return None
    getattr(bridge, crashes).side_effect = crash
    with pytest.raises(SystemExit) as caught:
        getattr(fs, "cmd_" + command)(args)
    captured = capsys.readouterr()
    if use_json:
        assert caught.value.code == 1
        error = json.loads(captured.out)["error"]
        assert error["code"] == "HANDLER_ERROR"
        assert error["message"] == "Filesystem handler crashed"
    else:
        assert str(caught.value) == "Error: filesystem handler crashed"
        assert captured.out == ""
    getattr(bridge, crashes).assert_called()
    bridge.open_file.assert_not_called()
    bridge.backend.close.assert_called_once()
    assert output.read_bytes() == b"keep existing output"


def _backend(drive_names, partition_number=None, partition_name=None):
    parts = [SimpleNamespace(num=num, get_drive_name=lambda name=name: name)
             for num, name in enumerate(drive_names)]
    return SimpleNamespace(
        partition_number=partition_number,
        partition_name=partition_name,
        rdb=SimpleNamespace(
            get_partition=lambda n: parts[n] if n < len(parts) else None))


def _bridge_without_a_root_name(backend):
    """A bridge whose examine of the root fails, on ``backend``."""
    bridge = object.__new__(fs.HandlerBridge)
    bridge._lock = threading.RLock()
    bridge._volname = None
    bridge._alloc_fib = Mock(side_effect=RuntimeError("no reply"))
    # The root's entries are not volume names.
    bridge.list_dir = Mock(return_value=[{"name": "Devs"}])
    bridge.backend = backend
    return bridge


def test_volume_name_falls_back_to_the_opened_partition():
    from amitools.fs.FSString import FSString

    backend = _backend([FSString("DH0"), FSString("DH1")], partition_number=1)
    assert _bridge_without_a_root_name(backend).volume_name() == "DH1"


def test_volume_name_of_a_repeated_drive_name_is_the_one_it_mounts_as():
    """--partition DH0.1 names the second DH0 as the banner does."""
    from amitools.fs.FSString import FSString

    backend = _backend([FSString("DH0"), FSString("DH0")], partition_number=1,
                       partition_name="DH0.1")
    assert _bridge_without_a_root_name(backend).volume_name() == "DH0.1"


def test_backend_keeps_the_name_it_found_the_partition_by(tmp_path):
    from amitools.fs.DosType import DOS3

    from amifuse.driver_runtime import BlockDeviceBackend
    from amifuse.fuse_fs import get_partition_info
    from tests.unit.test_repeated_partition_names import _image

    image = _image(tmp_path, [("DH0", DOS3), ("DH0", DOS3)])
    for selector, number in (("DH0.1", 1), ("1", 1), ("DH0", 0)):
        backend = BlockDeviceBackend(image, partition_scope=True,
                                     partition=selector)
        backend.open()
        try:
            assert (backend.partition_number, backend.partition_name) == (
                number, get_partition_info(image, None, selector)["name"])
        finally:
            backend.close()


@pytest.mark.parametrize("empty", ["fsstring", None])
def test_volume_name_of_a_partition_without_a_drive_name(empty):
    """An empty or damaged name gets the label inspect gives that partition."""
    from amitools.fs.FSString import FSString

    name = FSString("") if empty else None
    backend = _backend([FSString("DH0"), name], partition_number=1)
    assert _bridge_without_a_root_name(backend).volume_name() == "#1"


@pytest.mark.parametrize("backend", [
    SimpleNamespace(partition_number=None, partition_name=None, rdb=None),
    SimpleNamespace(partition_number=None, partition_name=None,
                    rdb=SimpleNamespace(get_partition=lambda n: None)),
], ids=["no-rdb", "no-partition"])
def test_volume_name_never_comes_from_the_root_listing(backend):
    assert _bridge_without_a_root_name(backend).volume_name() == "AmigaFS"


def test_volume_name_from_the_rdb_is_a_str():
    """verify --json serializes the name; an FSString fails json.dumps."""
    from amitools.fs.FSString import FSString

    bridge = object.__new__(fs.HandlerBridge)
    bridge._lock = threading.RLock()
    bridge._volname = None
    bridge._alloc_fib = Mock(side_effect=RuntimeError("no reply"))
    bridge.backend = _backend([FSString("DH0")])
    name = bridge.volume_name()
    assert type(name) is str
    assert json.dumps(name) == '"DH0"'
    assert bridge.volume_name() is name


def _bridge_on(backend):
    bridge = object.__new__(fs.HandlerBridge)
    bridge._lock = threading.RLock()
    bridge._drive_name = None
    bridge._drive_name_known = False
    bridge.backend = backend
    return bridge


def test_drive_name_of_a_repeated_drive_name_is_the_rdb_s_own():
    """A soft link's target says DH0:, not the DH0.1 the mount calls it (#447)."""
    from amitools.fs.FSString import FSString

    backend = _backend([FSString("DH0"), FSString("DH0")], partition_number=1,
                       partition_name="DH0.1")
    name = _bridge_on(backend).drive_name()
    assert (type(name), name) == (str, "DH0")


@pytest.mark.parametrize("case", ["no-rdb", "damaged", "empty"])
def test_no_drive_name(case):
    """A floppy has no drive name; nor has a partition with a damaged one."""
    from amitools.fs.FSString import FSString

    if case == "no-rdb":
        backend = SimpleNamespace(partition_number=None, partition_name=None, rdb=None)
    else:
        backend = _backend([None if case == "damaged" else FSString("")])
    assert _bridge_on(backend).drive_name() is None
