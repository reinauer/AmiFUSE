"""An explicit selector must never silently select a whole floppy or ISO."""
import json

import pytest
from amifuse import driver_resolver, fuse_fs, platform
from tests.fixtures.images import make_adf
from tests.unit.test_rdb_inspect import _write_iso


@pytest.fixture(params=["floppy", "ISO"])
def image(request, tmp_path):
    path = tmp_path / ("disk.adf" if request.param == "floppy" else "disk.iso")
    if request.param == "floppy":
        make_adf(path)
    else:
        _write_iso(path)
    return path, request.param


@pytest.mark.parametrize("command", ["ls", "read", "hash", "verify", "write"])
@pytest.mark.parametrize("use_json", [False, True])
@pytest.mark.parametrize("explicit_driver", [False, True])
def test_file_command_refuses_selector(image, command, use_json, explicit_driver,
                                      tmp_path, capsys, monkeypatch):
    path, kind = image
    before = path.read_bytes()
    source = tmp_path / "source"
    source.write_bytes(b"must not reach image")
    args = [command, str(path), "--partition", "2"]
    if command in {"read", "hash", "write"}:
        args += ["--file", "file"]
    if command == "read":
        args += ["--out", str(tmp_path / "output")]
    if command == "write":
        args += ["--in", str(source)]
    if explicit_driver:
        args += ["--driver", str(driver_resolver.BUNDLED_DRIVER)]
    if use_json:
        args.append("--json")
    def unexpected(*args, **kwargs):
        pytest.fail("partition refusal must precede driver resolution")
    monkeypatch.setattr(fuse_fs, "resolve_driver", unexpected)
    with pytest.raises(SystemExit) as caught:
        fuse_fs.main(args)
    if use_json:
        assert caught.value.code == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "PARTITION_UNSUPPORTED"
        message = error["message"]
    else:
        message = str(caught.value)
    assert f"{kind} image, which has no partitions" in message
    assert path.read_bytes() == before
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("explicit_driver", [False, True])
def test_mount_refuses_selector(image, explicit_driver, tmp_path, monkeypatch):
    path, kind = image
    before = path.read_bytes()
    monkeypatch.setattr(fuse_fs, "_require_fuse", lambda: object)
    monkeypatch.setattr(platform, "check_fuse_available", lambda: None)
    args = ["mount", str(path), "--partition", "2", "--mountpoint", str(tmp_path / "mnt")]
    if explicit_driver:
        args += ["--driver", str(driver_resolver.BUNDLED_DRIVER)]
    with pytest.raises(SystemExit, match=f"{kind} image, which has no partitions"):
        fuse_fs.main(args)
    assert path.read_bytes() == before
