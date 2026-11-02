"""`amifuse format` on a floppy image: which images, and which refusals.

Only format takes a blank image for a floppy, through
rdb_inspect.floppy_to_format; detect_adf, which every other command uses,
still wants a DOS type in block 0. tests/integration/test_format_floppy.py
runs the bundled FastFileSystem.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from amifuse import fuse_fs, rdb_inspect

DD, HD = 901120, 1802240
DOS0, DOS1, DOS3 = 0x444F5300, 0x444F5301, 0x444F5303
PFS_VER = b"$VER: Professional-File-System-III 19.2 (1.1.2020)\0"


def _image(tmp_path, size=DD, head=b"", name="floppy.adf") -> Path:
    image = tmp_path / name
    image.write_bytes(head + bytes(size - len(head)))
    return image


# --- what counts as a floppy ------------------------------------------------


@pytest.mark.parametrize("size,is_hd,sectors", [(DD, False, 11), (HD, True, 22)])
def test_blank_image_of_a_floppy_size_is_dos0(tmp_path, size, is_hd, sectors):
    info = rdb_inspect.floppy_to_format(_image(tmp_path, size))
    assert (info.dos_type, info.is_hd) == (DOS0, is_hd)
    assert (info.cylinders, info.heads, info.sectors_per_track) == (80, 2, sectors)
    assert (info.block_size, info.total_blocks) == (512, size // 512)


def test_blank_image_takes_the_dos_type_asked_for(tmp_path):
    assert rdb_inspect.floppy_to_format(_image(tmp_path), DOS3).dos_type == DOS3


def test_dos_type_comes_from_block_0(tmp_path):
    info = rdb_inspect.floppy_to_format(_image(tmp_path, HD, b"DOS\x03"))
    assert (info.dos_type, info.is_hd) == (DOS3, True)


def test_asked_for_dos_type_replaces_the_one_in_block_0(tmp_path):
    image = _image(tmp_path, head=b"DOS\x03")
    assert rdb_inspect.floppy_to_format(image, DOS1).dos_type == DOS1


@pytest.mark.parametrize("dos_type", [None, DOS1])
@pytest.mark.parametrize("size", [0, 512, DD - 1, DD + 1, 1474560, HD + 512])
def test_another_size_is_no_floppy(tmp_path, size, dos_type):
    assert rdb_inspect.floppy_to_format(_image(tmp_path, size), dos_type) is None


def test_missing_image_is_no_floppy(tmp_path):
    assert rdb_inspect.floppy_to_format(tmp_path / "missing.adf") is None


@pytest.mark.parametrize("head", [
    b"KICK", b"NDOS", b"DOS\x08", b"PFS\x01", b"\x00\x00\x00\x00\x01"],
    ids=["kick", "ndos", "dos8", "pfs", "not-all-zero"])
def test_other_contents_need_the_option(tmp_path, head):
    """Nothing says such an image may be lost, so asking for it is needed."""
    image = _image(tmp_path, head=head)
    with pytest.raises(ValueError, match="--dos-type"):
        rdb_inspect.floppy_to_format(image)
    assert rdb_inspect.floppy_to_format(image, DOS1).dos_type == DOS1


def test_a_single_byte_at_the_end_is_not_blank(tmp_path):
    image = _image(tmp_path)
    with open(image, "r+b") as f:
        f.seek(DD - 1)
        f.write(b"\x01")
    with pytest.raises(ValueError, match="--dos-type"):
        rdb_inspect.floppy_to_format(image)


@pytest.mark.parametrize("dos_type", [None, DOS1])
@pytest.mark.parametrize("block", [0, 1, 15])
def test_an_rdsk_block_is_left_to_the_rdb_path(tmp_path, block, dos_type):
    """An RDB on a disk of a floppy's size, damaged or not, is no floppy."""
    image = _image(tmp_path, head=bytes(512 * block) + b"RDSK")
    assert rdb_inspect.floppy_to_format(image, dos_type) is None


def test_an_rdsk_block_at_the_requested_block_size_is_left_to_the_rdb_path(tmp_path):
    image = _image(tmp_path, head=bytes(1024 * 15) + b"RDSK")
    assert rdb_inspect.floppy_to_format(image, DOS1, block_size=1024) is None


@pytest.mark.parametrize("dos_type", [None, DOS1])
def test_an_mbr_is_left_to_the_rdb_path(tmp_path, dos_type):
    mbr = bytearray(512)
    mbr[0x1BE + 4] = 0x76
    mbr[0x1BE + 12] = 8
    mbr[0x1FE:0x200] = b"\x55\xAA"
    assert rdb_inspect.floppy_to_format(_image(tmp_path, head=bytes(mbr)), dos_type) is None


def test_mount_decides_an_image_with_both_dos_and_rdsk(tmp_path):
    """mount takes DOS in block 0 for a floppy, so format does too."""
    image = _image(tmp_path, head=b"DOS\x01".ljust(512, b"\0") + b"RDSK")
    assert rdb_inspect.detect_adf(image) is not None
    assert rdb_inspect.floppy_to_format(image).dos_type == DOS1


@pytest.mark.parametrize("size", [DD, HD])
def test_other_commands_do_not_take_a_blank_image_for_a_floppy(tmp_path, size):
    assert rdb_inspect.detect_adf(_image(tmp_path, size)) is None


# --- the command line -------------------------------------------------------


def _parse(monkeypatch, *argv):
    called = []
    monkeypatch.setattr(fuse_fs, "cmd_format", called.append)
    fuse_fs.main(["format", *argv])
    return called[0]


@pytest.mark.parametrize("text,value", [
    ("DOS0", DOS0), ("DOS7", DOS0 + 7), ("dos3", DOS3)])
def test_dos_type_option(monkeypatch, text, value):
    assert _parse(monkeypatch, "x.adf", "Name", "--dos-type", text).dos_type == value


@pytest.mark.parametrize("argv", [
    ("x.adf", "--dos-type", "DOS1", "Name"),
    ("--dos-type", "DOS1", "x.adf", "Name"),
    ("x.adf", "Name", "--dos-type", "DOS1"),
], ids=["between", "first", "last"])
def test_option_may_stand_anywhere(monkeypatch, argv):
    args = _parse(monkeypatch, *argv)
    assert (args.partition, args.volname, args.dos_type) == ("Name", None, DOS1)


@pytest.mark.parametrize("text", ["DOS8", "DOS", "DOS10", "PFS3", "0", ""])
def test_a_dos_type_other_than_dos0_to_dos7_is_refused(monkeypatch, capsys, text):
    with pytest.raises(SystemExit) as caught:
        _parse(monkeypatch, "x.adf", "Name", "--dos-type", text)
    assert caught.value.code == 2
    assert "must be DOS0 to DOS7" in capsys.readouterr().err


def test_format_without_a_name_stays_a_usage_error(monkeypatch, capsys):
    with pytest.raises(SystemExit) as caught:
        _parse(monkeypatch, "x.adf")
    assert caught.value.code == 2


def test_the_rdb_form_keeps_its_default_name(monkeypatch, tmp_path):
    """The default moves into format_volume, once it knows the image."""
    format_volume = MagicMock()
    monkeypatch.setattr(fuse_fs, "format_volume", format_volume)
    fuse_fs.main(["format", str(tmp_path / "disk.hdf"), "DH0"])
    assert format_volume.call_args.kwargs["partition"] == "DH0"
    assert format_volume.call_args.kwargs["volname"] is None
    assert format_volume.call_args.kwargs["dos_type"] is None


# --- format_volume ----------------------------------------------------------


@pytest.fixture
def stub_bridge(monkeypatch):
    """A bridge whose handler answers every packet; returns its constructor."""
    bridge = MagicMock()
    bridge.state.crashed = False
    bridge.launcher.alloc_bstr.return_value = (None, 0x1234)
    bridge.launcher.send_inhibit.return_value = (0x100, 0)
    bridge.launcher.send_format.return_value = (0x200, 0)
    bridge.launcher.send_flush.return_value = (0x300, 0)
    bridge._run_until_reply.return_value = (-1, 0)
    constructor = MagicMock(return_value=bridge)
    constructor.bridge = bridge
    monkeypatch.setattr(fuse_fs, "HandlerBridge", constructor)
    return constructor


@pytest.fixture
def no_rdb(monkeypatch):
    """The floppy path must not ask for an RDB."""
    def refuse(*args, **kwargs):
        raise AssertionError("get_partition_info called for a floppy")

    monkeypatch.setattr(fuse_fs, "get_partition_info", refuse)


def _driver(tmp_path, ver=b""):
    driver = tmp_path / "x.handler"
    driver.write_bytes(b"\x00\x00\x03\xf3" + bytes(64) + ver + bytes(64))
    return driver


def test_a_floppy_formats_with_its_geometry_and_no_partition(
        tmp_path, stub_bridge, no_rdb, capsys):
    image = _image(tmp_path, HD)
    fuse_fs.format_volume(image, _driver(tmp_path), None, "Fresh", dos_type=DOS3)

    kwargs = stub_bridge.call_args.kwargs
    assert kwargs["partition"] is None
    assert kwargs["read_only"] is False
    assert (kwargs["adf_info"].dos_type, kwargs["adf_info"].is_hd) == (DOS3, True)
    launcher = stub_bridge.bridge.launcher
    assert launcher.send_inhibit.call_args.args[1] is True
    assert launcher.alloc_bstr.call_args.args[0] == "Fresh"
    assert launcher.send_format.call_args.args[2] == DOS3
    stub_bridge.bridge.backend.fsync.assert_called_once_with()
    out = capsys.readouterr().out
    assert f"Formatting HD floppy image {image}" in out
    assert "partition" not in out
    assert f"Format complete. Volume 'Fresh' created on {image}." in out


def test_a_floppy_uses_the_driver_mount_would_use(monkeypatch, tmp_path, stub_bridge, no_rdb):
    calls = []
    real = fuse_fs.resolve_driver

    def resolve(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(fuse_fs, "resolve_driver", resolve)
    fuse_fs.format_volume(_image(tmp_path), _driver(tmp_path), None, "Fresh", dos_type=DOS3)
    assert calls[0][0][3] is None
    assert calls[0][1]["adf_info"].dos_type == DOS3


def test_the_driver_is_looked_up_for_the_dos_type_asked_for(
        monkeypatch, tmp_path, stub_bridge, no_rdb):
    """A driver that serves block 0's type need not serve the option's."""
    looked_up = []

    def find(dos_type, subject, quiet=False):
        looked_up.append(dos_type)
        raise fuse_fs.DriverNotFound(f"{subject}\nnone")

    monkeypatch.setattr(fuse_fs.driver_resolver, "find_folder_driver", find)
    image = _image(tmp_path, head=b"DOS\x01")
    with pytest.raises(SystemExit, match=r"DD, DOS7"):
        fuse_fs.format_volume(image, None, None, "Fresh", dos_type=DOS0 + 7)
    assert looked_up == [DOS0 + 7]
    stub_bridge.assert_not_called()


def test_volname_argument_names_a_floppy_too(tmp_path, stub_bridge, no_rdb):
    fuse_fs.format_volume(_image(tmp_path), _driver(tmp_path), None, None, volname="Fresh")
    assert stub_bridge.bridge.launcher.alloc_bstr.call_args.args[0] == "Fresh"


def test_a_partition_and_a_name_are_refused_for_a_floppy(tmp_path, stub_bridge, no_rdb):
    with pytest.raises(SystemExit, match="no partitions"):
        fuse_fs.format_volume(_image(tmp_path), _driver(tmp_path), None, "DF0", volname="Name")
    stub_bridge.assert_not_called()


@pytest.mark.parametrize("length", [256, 300])
def test_a_floppy_name_too_long_for_a_bstr_is_refused(tmp_path, stub_bridge, no_rdb, length):
    """The name is in args.partition, past cmd_format's own check."""
    image = _image(tmp_path)
    args = SimpleNamespace(image=image, partition="A" * length, volname=None,
                           driver=_driver(tmp_path), block_size=None, debug=False,
                           dos_type=None)
    with pytest.raises(SystemExit) as caught:
        fuse_fs.cmd_format(args)
    assert caught.value.code == (
        f"Error: volume name is {length} characters long; at most 255 are allowed.")
    stub_bridge.assert_not_called()


def test_a_floppy_name_of_255_goes_to_the_handler(tmp_path, stub_bridge, no_rdb):
    fuse_fs.format_volume(_image(tmp_path), _driver(tmp_path), None, "A" * 255)
    assert stub_bridge.bridge.launcher.alloc_bstr.call_args.args[0] == "A" * 255


def test_pfs3_name_limit_holds_on_a_floppy(tmp_path, stub_bridge, no_rdb):
    image = _image(tmp_path)
    with pytest.raises(SystemExit) as caught:
        fuse_fs.format_volume(image, _driver(tmp_path, PFS_VER), None, "A" * 32)
    assert caught.value.code == (
        "Error: volume name is 32 characters long; pfs3aio stores at most 31. "
        "Use a shorter name.")
    stub_bridge.assert_not_called()
    assert image.read_bytes() == bytes(DD)


def test_other_contents_are_refused_before_the_handler_starts(tmp_path, stub_bridge):
    image = _image(tmp_path, head=b"KICK")
    with pytest.raises(SystemExit) as caught:
        fuse_fs.format_volume(image, _driver(tmp_path), None, "Name")
    assert "--dos-type" in str(caught.value.code)
    stub_bridge.assert_not_called()


@pytest.mark.parametrize("size,head", [
    (4096, b""), (DD, b"RDSK")], ids=["hard-disk-size", "floppy-size-rdb"])
def test_the_option_is_refused_for_an_rdb_disk(tmp_path, stub_bridge, size, head):
    image = _image(tmp_path, size, head, name="disk.hdf")
    with pytest.raises(SystemExit, match="only for a floppy image"):
        fuse_fs.format_volume(image, _driver(tmp_path), None, "DH0", dos_type=DOS1)
    stub_bridge.assert_not_called()


def test_a_disk_of_a_floppy_size_with_an_rdb_keeps_its_partition_argument(
        monkeypatch, tmp_path, stub_bridge, capsys):
    monkeypatch.setattr(fuse_fs, "get_partition_info", lambda *a, **kw: {
        "name": "DH0", "dostype": DOS3, "block_size": 512})
    image = _image(tmp_path, head=b"RDSK", name="disk.hdf")

    fuse_fs.format_volume(image, _driver(tmp_path), None, "DH0")

    assert stub_bridge.call_args.kwargs["partition"] == "DH0"
    assert stub_bridge.call_args.kwargs["adf_info"] is None
    assert stub_bridge.bridge.launcher.alloc_bstr.call_args.args[0] == "Empty"
    assert "Volume 'Empty' created on partition 'DH0'." in capsys.readouterr().out
