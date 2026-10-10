"""Incomplete extraction must not publish a digest or replace an output."""

import io
import json
import os
import re
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amifuse import fuse_fs as fs


@pytest.fixture
def read_context(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.write_bytes(b"keep me")
    args = SimpleNamespace(image=tmp_path / "image", file="file", json=True,
                           out=str(output))
    bridge = Mock()
    bridge.is_mounted.return_value = fs.MountState(True, None, None)
    bridge.stat_path.return_value = {"dir_type": -3, "size": 5}
    bridge.open_file.return_value = (123, 0)
    monkeypatch.setattr(fs, "_create_bridge_from_args", lambda *a: (bridge, None))
    return args, bridge, output


@pytest.mark.parametrize("command", ["hash", "read"])
@pytest.mark.parametrize("chunks", [[b""], [b"ab", b""]])
@pytest.mark.parametrize("use_json", [False, True])
def test_short_read_fails(read_context, capsys, command, chunks, use_json):
    args, bridge, output = read_context
    args.json = use_json
    bridge.read_handle.side_effect = chunks
    with pytest.raises(SystemExit) as caught:
        getattr(fs, "cmd_" + command)(args)
    captured = capsys.readouterr()
    if use_json:
        assert caught.value.code == 1
        result = json.loads(captured.out)
        assert result["status"] == "error"
        assert result["error"]["code"] == "HANDLER_ERROR"
        message = result["error"]["message"]
        assert "hash" not in result
    else:
        assert captured.out == ""
        message = str(caught.value)
    assert f"expected 5 bytes, got {len(chunks[0])}" in message
    assert output.read_bytes() == b"keep me"
    assert list(output.parent.iterdir()) == [output]
    bridge.close_file.assert_called_once_with(123)
    bridge.backend.close.assert_called_once()


@pytest.mark.parametrize("failure", ["read", "close", "replace"])
@pytest.mark.parametrize("existing", [True, False])
def test_extraction_failure_preserves_destination(read_context, monkeypatch, capsys,
                                                 failure, existing):
    args, bridge, output = read_context
    if not existing:
        output.unlink()
    bridge.read_handle.return_value = b"hello"
    error = OSError("injected failure")
    if failure == "read":
        bridge.read_handle.side_effect = [b"ab", error]
    elif failure == "close":
        bridge.close_file.side_effect = error
    else:
        monkeypatch.setattr(fs.os, "replace", Mock(side_effect=error))
    with pytest.raises(SystemExit):
        fs.cmd_read(args)
    assert "injected failure" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert output.exists() is existing
    if existing:
        assert output.read_bytes() == b"keep me"
    assert not list(output.parent.glob(".amifuse-*"))
    bridge.backend.close.assert_called_once()


@pytest.mark.parametrize("data", [b"", b"hello"])
def test_complete_read_replaces_output(read_context, capsys, data):
    args, bridge, output = read_context
    bridge.stat_path.return_value["size"] = len(data)
    bridge.read_handle.side_effect = [data]
    old_mode = stat.S_IMODE(output.stat().st_mode)
    fs.cmd_read(args)
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert output.read_bytes() == data
    assert stat.S_IMODE(output.stat().st_mode) == old_mode
    assert not list(output.parent.glob(".amifuse-*"))


def test_stdout_short_read_fails_without_closing_stream(read_context, monkeypatch):
    args, bridge, _ = read_context
    args.out, args.json = "-", False
    bridge.read_handle.side_effect = [b"ab", b""]
    stream = io.BytesIO()
    monkeypatch.setattr(fs, "sys", SimpleNamespace(stdout=SimpleNamespace(buffer=stream)))
    with pytest.raises(SystemExit, match="expected 5 bytes, got 2"):
        fs.cmd_read(args)
    assert stream.getvalue() == b"ab"
    assert not stream.closed


def test_symlink_output_keeps_link(read_context, capsys):
    args, bridge, output = read_context
    link = output.with_name("link")
    try:
        link.symlink_to(output.name)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    args.out = str(link)
    bridge.read_handle.return_value = b"hello"
    fs.cmd_read(args)
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert link.is_symlink()
    assert output.read_bytes() == b"hello"


def test_null_device_remains_a_stream(read_context, capsys, monkeypatch):
    args, bridge, _ = read_context
    args.out = os.devnull
    bridge.read_handle.return_value = b"hello"
    # Windows access() can deny NUL although opening the stream succeeds.
    monkeypatch.setattr(fs.os, "access", lambda *args: False)
    fs.cmd_read(args)
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


@pytest.mark.parametrize("use_json", [False, True])
def test_read_only_destination_is_preserved(read_context, capsys, use_json):
    args, bridge, output = read_context
    args.json = use_json
    bridge.read_handle.return_value = b"hello"
    old_mode = output.stat().st_mode
    output.chmod(stat.S_IREAD)
    try:
        if os.access(output, os.W_OK):
            pytest.skip("Current user can write read-only files")
        with pytest.raises(SystemExit) as caught:
            fs.cmd_read(args)
        captured = capsys.readouterr()
        if use_json:
            assert caught.value.code == 1
            error = json.loads(captured.out)["error"]
            assert error["code"] == "HANDLER_ERROR"
            assert "Cannot create output file" in error["message"]
        else:
            assert "cannot create output file" in str(caught.value)
        assert output.read_bytes() == b"keep me"
        assert not list(output.parent.glob(".amifuse-*"))
        bridge.read_handle.assert_not_called()
        bridge.backend.close.assert_called_once()
    finally:
        output.chmod(old_mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask permissions")
@pytest.mark.parametrize("mask", [0o022, 0o002, 0o077])
def test_new_output_uses_umask(read_context, capsys, mask):
    args, bridge, output = read_context
    output.unlink()
    bridge.read_handle.return_value = b"hello"
    old_mask = os.umask(mask)
    try:
        fs.cmd_read(args)
    finally:
        os.umask(old_mask)
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert output.read_bytes() == b"hello"
    assert stat.S_IMODE(output.stat().st_mode) == 0o666 & ~mask
    assert not list(output.parent.glob(".amifuse-*"))


def test_read_only_staged_file_is_removed_after_replace_failure(
        read_context, monkeypatch, capsys):
    args, bridge, output = read_context
    old_mode = output.stat().st_mode

    def read_and_protect(*args):
        # The target can become read-only after the initial access check.
        output.chmod(stat.S_IREAD)
        return b"hello"

    def fail_replace(source, destination):
        assert not source.stat().st_mode & stat.S_IWRITE
        raise PermissionError("replacement denied")

    bridge.read_handle.side_effect = read_and_protect
    monkeypatch.setattr(fs.os, "replace", fail_replace)
    try:
        with pytest.raises(SystemExit) as caught:
            fs.cmd_read(args)
        assert caught.value.code == 1
        assert "replacement denied" in json.loads(capsys.readouterr().out)["error"]["message"]
        assert output.read_bytes() == b"keep me"
        assert not list(output.parent.glob(".amifuse-*"))
        bridge.backend.close.assert_called_once()
    finally:
        output.chmod(old_mode)


def test_output_is_staged_beside_destination(read_context, capsys):
    args, bridge, output = read_context
    staged = []

    def read(*args):
        # A private staging directory would give the output its own ACL.
        staged.extend(output.parent.glob(".amifuse-*"))
        return b"hello"

    bridge.read_handle.side_effect = read
    fs.cmd_read(args)
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert len(staged) == 1
    assert staged[0].parent == output.parent
    assert staged[0].name.endswith(".tmp")
    assert not staged[0].exists()


def _sddl(path, tmp_path):
    # /save writes the DACL as SDDL, which names accounts by SID and so does
    # not depend on the Windows display language.
    saved = tmp_path / f"{path.name}.acl"
    subprocess.run(["icacls", str(path), "/save", str(saved)],
                   capture_output=True, check=True)
    raw = saved.read_bytes()
    text = raw.decode("utf-16" if raw[:2] == b"\xff\xfe" else "utf-16-le")
    return text.strip("\ufeff\x00\r\n").splitlines()[-1].strip("\x00")


# Inherited BUILTIN\Users access, the ACE granted to the output directory.
INHERITED_USERS_ACE = re.compile(r"\(A;[^;]*ID[^;]*;[^;]*;;;BU\)")


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL inheritance")
@pytest.mark.parametrize("existing", [True, False])
def test_output_inherits_directory_acl(read_context, capsys, tmp_path, existing):
    args, bridge, output = read_context
    # tmp_path is owner-only, like a private staging directory, so give the
    # output directory an inheritable ACE that only it can pass on.
    shared = tmp_path / "shared"
    shared.mkdir()
    subprocess.run(["icacls", str(shared), "/grant", "*S-1-5-32-545:(OI)(CI)(RX)"],
                   capture_output=True, check=True)
    output = shared / "output"
    if existing:
        output.write_bytes(b"keep me")
    args.out = str(output)
    reference = shared / "reference"
    with open(reference, "wb") as stream:
        stream.write(b"x")
    bridge.read_handle.return_value = b"hello"
    fs.cmd_read(args)
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    acl = _sddl(output, tmp_path)
    assert acl == _sddl(reference, tmp_path)
    assert INHERITED_USERS_ACE.search(acl), acl


@pytest.mark.parametrize("use_json", [False, True])
@pytest.mark.parametrize("existing", [True, False])
def test_unwritable_directory_names_the_cause(read_context, capsys, monkeypatch,
                                              use_json, existing):
    args, bridge, output = read_context
    args.json = use_json
    if not existing:
        output.unlink()
    bridge.read_handle.return_value = b"hello"
    real_open = open

    def deny_staging(path, mode="r", *a, **kw):
        if mode == "xb":
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", deny_staging)
    with pytest.raises(SystemExit) as caught:
        fs.cmd_read(args)
    if use_json:
        message = json.loads(capsys.readouterr().out)["error"]["message"]
    else:
        message = str(caught.value)
    assert (f"cannot stage output in {output.parent.resolve()} "
            "(directory not writable): Permission denied") in message
    assert ".amifuse-" not in message
    assert output.exists() is existing
    bridge.read_handle.assert_not_called()
    bridge.backend.close.assert_called_once()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="POSIX directory permissions")
def test_writable_output_in_read_only_directory(tmp_path, read_context, capsys):
    args, bridge, output = read_context
    locked = tmp_path / "locked"
    locked.mkdir()
    target = locked / "out"
    target.write_bytes(b"keep me")
    args.out = str(target)
    bridge.read_handle.return_value = b"hello"
    locked.chmod(0o555)
    try:
        with pytest.raises(SystemExit):
            fs.cmd_read(args)
    finally:
        locked.chmod(0o755)
    message = json.loads(capsys.readouterr().out)["error"]["message"]
    assert "directory not writable" in message
    assert target.read_bytes() == b"keep me"
    assert list(locked.iterdir()) == [target]


@pytest.mark.parametrize("use_json", [False, True])
def test_missing_output_directory_names_the_directory(read_context, capsys, use_json):
    args, bridge, output = read_context
    args.json = use_json
    missing = output.parent / "nodir"
    args.out = str(missing / "out.txt")
    bridge.read_handle.return_value = b"hello"
    with pytest.raises(SystemExit) as caught:
        fs.cmd_read(args)
    if use_json:
        message = json.loads(capsys.readouterr().out)["error"]["message"]
    else:
        message = str(caught.value)
    assert f"cannot stage output in {missing.resolve()}: " in message
    assert ".amifuse-" not in message
    assert not missing.exists()
    bridge.read_handle.assert_not_called()
    bridge.backend.close.assert_called_once()
