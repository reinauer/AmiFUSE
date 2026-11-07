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


def test_incomplete_list_does_not_claim_partition_is_absent(tmp_path):
    from tests.unit.test_rdb_partition_list import _two_parts, PAST_END

    image = _two_parts(tmp_path, PAST_END)
    with pytest.raises(fuse_fs.PartitionNotFound, match="partition list is incomplete"):
        fuse_fs.get_partition_info(image, None, "2")
    assert fuse_fs.get_partition_info(image, None, "DH1")["name"] == "DH1"


@pytest.mark.parametrize("use_json", [False, True])
def test_write_retains_incomplete_list_refusal(tmp_path, capsys, use_json):
    from tests.unit.test_rdb_partition_list import _two_parts, PAST_END

    image = _two_parts(tmp_path, PAST_END)
    before = image.read_bytes()
    source = tmp_path / "input"
    source.write_bytes(b"must not be written")
    args = ["write", str(image), "--partition", "2", "--file", "new",
            "--in", str(source), "--driver", str(driver_resolver.BUNDLED_DRIVER)]
    if use_json:
        args.append("--json")
    with pytest.raises(SystemExit) as caught:
        fuse_fs.main(args)
    if use_json:
        assert caught.value.code == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "IMAGE_INVALID"
        message = error["message"]
    else:
        message = str(caught.value)
    assert "Partition 2 is not opened for writing" in message
    assert "the rest of the partition list" in message
    assert image.read_bytes() == before


@pytest.mark.parametrize("command", ["mount", "format"])
def test_mount_and_format_retain_incomplete_list_refusal(
        tmp_path, monkeypatch, command):
    from tests.unit.test_rdb_partition_list import _two_parts, PAST_END

    image = _two_parts(tmp_path, PAST_END)
    before = image.read_bytes()
    monkeypatch.setattr(fuse_fs, "_require_fuse", lambda: object)
    monkeypatch.setattr(platform, "check_fuse_available", lambda: None)
    args = [command, str(image), "--driver", str(driver_resolver.BUNDLED_DRIVER)]
    if command == "mount":
        args += ["--partition", "2", "--write", "--mountpoint", str(tmp_path / "mnt")]
    else:
        args += ["2", "New"]
    with pytest.raises(SystemExit, match="Partition 2 is not opened for writing"):
        fuse_fs.main(args)
    assert image.read_bytes() == before
