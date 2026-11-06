"""A missing RDB partition is not a missing filesystem driver (#400)."""

import json

import pytest
from amitools.fs.DosType import DOS3

from amifuse import driver_resolver, fuse_fs, platform
from tests.unit.test_partition_name_before_number import _make_rdb_parts


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "disk.hdf"
    path.write_bytes(_make_rdb_parts(
        tmp_path, 512, [("P0", DOS3), ("P1", DOS3)]))
    return path


def _message(selector):
    return (f"Partition not found: {selector}. "
            "Use amifuse inspect to list the image's partitions.")


@pytest.mark.parametrize("selector", ["2", "Nope", "-1"])
@pytest.mark.parametrize("explicit_driver", [False, True])
@pytest.mark.parametrize("use_json", [False, True])
@pytest.mark.parametrize("command", ["ls", "verify"])
def test_missing_partition_error(image, capsys, selector, explicit_driver,
                                 use_json, command):
    args = [command, str(image), "--partition", selector]
    if explicit_driver:
        args += ["--driver", str(driver_resolver.BUNDLED_DRIVER)]
    if use_json:
        args += ["--json"]
    with pytest.raises(SystemExit) as caught:
        fuse_fs.main(args)
    if use_json:
        assert caught.value.code == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "PARTITION_NOT_FOUND"
        assert error["message"] == _message(selector)
    else:
        assert str(caught.value) == _message(selector)


@pytest.mark.parametrize("command", ["mount", "format"])
@pytest.mark.parametrize("selector", ["2", "Nope"])
@pytest.mark.parametrize("explicit_driver", [False, True])
def test_mount_and_format_reject_missing_partition(
        image, tmp_path, monkeypatch, command, selector, explicit_driver):
    monkeypatch.setattr(fuse_fs, "_require_fuse", lambda: object)
    monkeypatch.setattr(platform, "check_fuse_available", lambda: None)
    before = image.read_bytes()
    args = [command, str(image)]
    if command == "mount":
        args += ["--partition", selector,
                 "--mountpoint", str(tmp_path / "mnt")]
    else:
        args += [selector, "Empty"]
    if explicit_driver:
        args += ["--driver", str(driver_resolver.BUNDLED_DRIVER)]
    with pytest.raises(SystemExit) as caught:
        fuse_fs.main(args)
    assert str(caught.value) == "Error: " + _message(selector)
    assert image.read_bytes() == before


def test_missing_selector_is_escaped(image):
    with pytest.raises(SystemExit) as caught:
        fuse_fs.main(["ls", str(image), "--partition", "bad\n\x1b[31m"])
    assert str(caught.value) == _message(r"bad\x0a\x1b[31m")
