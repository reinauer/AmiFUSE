"""An open that ends with the disk's own RDB names what still opens (#323).

Without --partition, a disk's own RDB that fails ends mount and the file
commands, though the partitions in its 0x76 entries open by name.
"""

import json

import pytest

from amifuse import fuse_fs, platform, rdb_inspect
from amifuse.fuse_fs import main
from tests.unit.test_parceiro_partitions import _disk_rdb
from tests.unit.test_partition_name_before_number import _damage_first_name
from tests.unit.test_selector_failed_rdb import _flip


def _damage_entry(image, tmp_path, lba):
    """Damage the first drive name in the RDB of the entry at lba."""
    data = bytearray(image.read_bytes())
    start, end = lba * 512, (lba + 160) * 512
    data[start:end] = _damage_first_name(tmp_path, bytes(data[start:end]))
    image.write_bytes(bytes(data))


def _hint(names):
    return (f"\nThe partitions in the MBR's 0x76 entries still open, read-only: "
            f"{', '.join(names)}. Open one with --partition {names[0]}.")


def _failed_disk(tmp_path, entries):
    """A Parceiro disk whose own RDSK block (block 2) fails its checksum,
    beside 0x76 entries holding the RDBs named."""
    image = _disk_rdb(tmp_path, entries, 2)
    _flip(image, 2)
    return image


def _error(image):
    with pytest.raises(OSError) as caught:
        rdb_inspect.open_rdisk(image)
    assert isinstance(caught.value, rdb_inspect.DiskRDBError)
    return str(caught.value)


@pytest.mark.parametrize("command", [
    ["ls"], ["verify"], ["hash", "--file", "x"], ["read", "--file", "x"]])
def test_file_command_names_the_entry_partitions(tmp_path, command,
                                                 user_driver_dir):
    image = _failed_disk(tmp_path, ("DH1", "PI1"))
    with pytest.raises(SystemExit) as caught:
        main([command[0], str(image), *command[1:]])
    assert caught.value.code == (
        f"Error: {_error(image)}{_hint(['DH1', 'PI1'])}")


def test_write_says_the_entry_partitions_open_read_only(tmp_path,
                                                        user_driver_dir):
    image = _failed_disk(tmp_path, ("DH1",))
    source = tmp_path / "source"
    source.write_bytes(b"data")
    with pytest.raises(SystemExit) as caught:
        main(["write", str(image), "--file", "x", "--in", str(source)])
    assert caught.value.code == f"Error: {_error(image)}{_hint(['DH1'])}"


def test_mount_names_the_entry_partitions(tmp_path, monkeypatch):
    image = _failed_disk(tmp_path, ("DH1",))
    monkeypatch.setattr(fuse_fs, "_require_fuse", lambda: object)
    monkeypatch.setattr(platform, "check_fuse_available", lambda: None)
    with pytest.raises(SystemExit) as caught:
        main(["mount", str(image), "--mountpoint", str(tmp_path / "mnt")])
    assert caught.value.code == f"Error: {_error(image)}{_hint(['DH1'])}"


def test_json_gives_the_names_in_the_error_envelope(tmp_path, capsys,
                                                    user_driver_dir):
    image = _failed_disk(tmp_path, ("DH1", "PI1"))
    with pytest.raises(SystemExit) as caught:
        main(["ls", str(image), "--json"])
    assert caught.value.code == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "IMAGE_INVALID"
    assert error["message"] == _error(image)
    assert error["details"] == {"partitions": ["DH1", "PI1"]}


def test_driver_option_names_the_entry_partitions(tmp_path, capsys):
    # With --driver no RDB is read for a driver: the bridge opens the image.
    image = _failed_disk(tmp_path, ("DH1",))
    driver = tmp_path / "driver"
    driver.write_bytes(b"driver")

    with pytest.raises(SystemExit) as caught:
        main(["ls", str(image), "--driver", str(driver)])
    assert caught.value.code == (
        f"Error initializing handler: {_error(image)}{_hint(['DH1'])}")

    with pytest.raises(SystemExit):
        main(["ls", str(image), "--driver", str(driver), "--json"])
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "HANDLER_ERROR"
    assert error["details"] == {"partitions": ["DH1"]}


def test_repeated_name_is_given_as_it_selects(tmp_path, user_driver_dir):
    image = _failed_disk(tmp_path, ("DH0", "DH0"))
    with pytest.raises(SystemExit) as caught:
        main(["ls", str(image)])
    assert caught.value.code.endswith(_hint(["DH0", "DH0.1"]))


def test_damaged_name_is_left_out(tmp_path, user_driver_dir):
    image = _failed_disk(tmp_path, ("PI0", "PI1"))
    _damage_entry(image, tmp_path, 200)
    with pytest.raises(SystemExit) as caught:
        main(["ls", str(image)])
    assert caught.value.code.endswith(_hint(["PI1"]))

    # With no name left there is nothing to say.
    _damage_entry(image, tmp_path, 400)
    with pytest.raises(SystemExit) as caught:
        main(["ls", str(image)])
    assert caught.value.code == f"Error: {_error(image)}"


def test_name_that_does_not_select_its_partition_is_left_out(
        tmp_path, monkeypatch, user_driver_dir):
    # As after a device or media error in an entry.
    image = _failed_disk(tmp_path, ("PI0", "PI1"))
    find_partition = rdb_inspect.find_partition

    def failing_entry(img, block_size, name, **kwargs):
        if name == "PI1":
            raise OSError("device error")
        return find_partition(img, block_size, name, **kwargs)

    monkeypatch.setattr(rdb_inspect, "find_partition", failing_entry)
    with pytest.raises(SystemExit) as caught:
        main(["ls", str(image)])
    assert caught.value.code.endswith(_hint(["PI0"]))


def test_named_partition_lists_the_volume_the_error_points_to(
        tmp_path, capsys, user_driver_dir):
    # Intact, the disk's own RDB holds DH1, so the entry's is DH1.1. The
    # bundled FastFileSystem serves its DOS type.
    image = _disk_rdb(tmp_path, ("DH1",), 2)
    main(["format", str(image), "DH1.1", "Entry"])
    source = tmp_path / "source"
    source.write_bytes(b"data")
    main(["write", str(image), "--partition", "DH1.1", "--file", "note",
          "--in", str(source)])
    capsys.readouterr()
    _flip(image, 2)

    with pytest.raises(SystemExit) as caught:
        main(["ls", str(image)])
    assert caught.value.code.endswith(_hint(["DH1"]))

    main(["ls", str(image), "--partition", "DH1"])
    assert "note" in capsys.readouterr().out
