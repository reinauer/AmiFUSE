"""Text an image chooses reaches the terminal escaped (#291, #211, #210).

File and volume names, drive names and DOS types come from the image, so an
escape sequence in one must not reach the terminal of whoever lists, inspects
or mounts it. Names stay raw where they are not printed: in the JSON output,
and for --partition.
"""

import argparse
import json
from contextlib import ExitStack
from pathlib import Path

import pytest

from amitools.fs.DosType import DOS3
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk

from amifuse import fuse_fs, rdb_inspect
from amifuse.driver_resolver import DriverNotFound, ResolvedDriver
from amifuse.driver_runtime import BlockDeviceBackend
from amifuse.rdb_inspect import dos_type_tag, escape_text, find_partition
from tests.unit.test_driver_resolver import _flip_byte, _rdb
from tests.unit.test_fuse_fs import _make_mock_bridge
from tests.unit.test_mbr_rdb_block_size import _make_mbr
from tests.unit.test_partition_name_before_number import (_damage_first_name,
                                                          _make_rdb_parts)
from tests.unit.test_repeated_partition_names import _image


ESC_TYPE = 0x1B5B324A  # ESC [ 2 J
ESC_TAG = "\\x1b[2J"
NAME = "D\x1b[2J"
SHOWN = "D\\x1b[2J"


def _hostile(tmp_path, name="esc.hdf", fs="0x1B5B324A"):
    """An rdbtool image: drive name NAME, DOS type ESC [ 2 J, and the bundled
    FastFileSystem in its RDB under that DOS type."""
    return _rdb(tmp_path, "0x1B5B324A", fs, name=name, drive=NAME)


@pytest.mark.parametrize("text, shown", [
    ("DH0", "DH0"),
    ("a b~", "a b~"),
    (NAME, SHOWN),
    ("a\x00b\nc\x7fd", "a\\x00b\\x0ac\\x7fd"),
    ("\x9b2J", "\\x9b2J"),  # C1 CSI
    ("B\xfccher", "B\\xfccher"),
    # A backslash and "x1b", which must not pass for an escaped ESC.
    ("\\x1b", "\\x5cx1b"),
    (None, "None"),
])
def test_escape_text(text, shown):
    assert escape_text(text) == shown


def test_dos_type_tag_goes_through_escape_text():
    assert dos_type_tag(ESC_TYPE) == ESC_TAG


class TestReport:
    """The text report of inspect and rdb-inspect (#210, #211)."""

    @pytest.mark.parametrize("full", [False, True], ids=["summary", "full"])
    def test_rdb_inspect(self, tmp_path, capsys, full):
        image = _hostile(tmp_path)

        rdb_inspect.main([str(image)] + (["--full"] if full else []))

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert f"Partition: #0 '{SHOWN}'" in out
        assert f"{ESC_TAG}/0x1b5b324a" in out
        assert f"FileSystem #0 {ESC_TAG}/0x1b5b324a" in out
        assert f"FS #0: {ESC_TAG}/0x1b5b324a" in out

    def test_inspect_with_its_driver_list(self, tmp_path, capsys, temp_dir):
        image = _hostile(tmp_path)

        fuse_fs.main(["inspect", str(image)])

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert f"Partition: #0 '{SHOWN}'" in out
        assert f"  {SHOWN}: {ESC_TAG}/0x1b5b324a (from RDB)" in out

    def test_lseg_warning_and_damaged_driver_message(
            self, tmp_path, capsys, temp_dir):
        image = _hostile(tmp_path)
        _flip_byte(image, b"LSEG")

        rdb_inspect.main([str(image)])
        out = capsys.readouterr().out
        with pytest.raises(DriverNotFound) as caught:
            fuse_fs.resolve_driver(image, None, None, None)

        assert "\x1b" not in out
        assert f"Filesystem #0 ({ESC_TAG}/0x1b5b324a): " in out
        message = str(caught.value)
        assert "\x1b" not in message
        assert message.startswith(f"Partition {SHOWN} ({ESC_TAG}) has a damaged ")

    def test_drive_name_a_damaged_partition_is_selected_instead_of(
            self, tmp_path, capsys, user_driver_dir):
        """inspect names the partition a number selects instead."""
        first = _image(tmp_path, [(NAME, DOS3)], name="a.hdf").read_bytes()
        second = _damage_first_name(
            tmp_path, _make_rdb_parts(tmp_path, 512, [("DH1", DOS3)]))
        image = _make_mbr(tmp_path, [(64, first), (448, second)])

        fuse_fs.main(["inspect", str(image)])

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert f"--partition 0 selects '{SHOWN}' instead" in out

    def test_json_keeps_the_raw_bytes(self, tmp_path, capsys):
        """json.dumps escapes them; an escape here would hide the real name."""
        image = _hostile(tmp_path)

        rdb_inspect.main([str(image), "--json"])

        desc = json.loads(capsys.readouterr().out)
        assert desc["partitions"][0]["name"] == NAME
        assert desc["filesystems"][0]["dostype_str"] == "\x1b[2J"


@pytest.mark.parametrize("dos_type", [
    "0x1B5B324A",  # ESC [ 2 J
    "0x413A4201",  # A:B1, an NTFS alternate data stream
    "0x2F2E2E01",  # /..1, a path below a directory
    "0x5C2E2E01",  # \..1, the same on Windows
])
def test_extract_fs_names_its_file_by_number(tmp_path, monkeypatch, capsys,
                                             dos_type):
    image = _rdb(tmp_path, dos_type, dos_type)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.chdir(out_dir)

    rdb_inspect.main([str(image), "--extract-fs", "0"])

    extracted = f"fs0_{int(dos_type, 16):08x}.bin"
    assert [path.name for path in out_dir.iterdir()] == [extracted]
    assert (out_dir / extracted).stat().st_size > 0
    assert f"to {extracted}\n" in capsys.readouterr().out


class TestPartitionMessages:
    """The driver and geometry messages (#211)."""

    def test_no_driver_in_the_rdb(self, tmp_path, monkeypatch):
        image = _image(tmp_path, [(NAME, DOS3)])
        found = []

        def find_folder_driver(dos_type, subject):
            found.append(subject)
            return ResolvedDriver(Path("FastFileSystem"), "bundled", "bundled")

        monkeypatch.setattr(fuse_fs.driver_resolver, "find_folder_driver",
                            find_folder_driver)

        fuse_fs.resolve_driver(image, None, None, None)

        assert found == [f"Partition {SHOWN} has no filesystem driver for "
                         "DOS3 in its RDB."]

    @pytest.mark.parametrize("env, message", [
        ({"low_cyl": 2, "high_cyl": 1}, "has invalid geometry"),
        ({"low_cyl": 0}, "overlaps RDB metadata"),
    ], ids=["geometry", "overlap"])
    def test_refused_partition(self, tmp_path, env, message):
        image = _image(tmp_path, [(NAME, DOS3)])
        with ExitStack() as cleanup:
            raw = RawBlockDevice(str(image), read_only=False)
            raw.open()
            cleanup.callback(raw.close)
            disk = RDisk(raw)
            assert disk.open()
            cleanup.callback(disk.close)
            part = disk.get_partition(0).part_blk
            for key, value in env.items():
                setattr(part.dos_env, key, value)
            part.write()

        backend = BlockDeviceBackend(image, partition_scope=True, read_only=False)
        with pytest.raises(ValueError) as caught:
            backend.open()

        assert str(caught.value) == f"Partition {SHOWN} {message}"

    @pytest.mark.parametrize("second, selector, message", [
        ([NAME], NAME, "is in an RDB that does not open with 1024-byte blocks"),
        ([NAME], f"{NAME}.1", "is not found with 1024-byte blocks, as an RDB "
                              "on this disk does not open with them"),
        ([NAME, NAME], f"{NAME}.1",
         "names another partition with 1024-byte blocks, as an RDB on this "
         "disk does not open with them"),
    ], ids=["other-rdb", "not-found", "another"])
    def test_forced_block_size(self, tmp_path, second, selector, message):
        """inspect looks up the image's names, so these messages can hold one."""
        image = self._two_sizes(tmp_path, second)

        with pytest.raises(OSError) as caught:
            find_partition(image, 1024, selector)

        assert str(caught.value) == (
            f"Partition {escape_text(selector)} {message}")

    def test_forced_block_size_in_inspect(self, tmp_path, capsys,
                                          user_driver_dir):
        image = self._two_sizes(tmp_path, [NAME])

        fuse_fs.main(["inspect", "--block-size", "1024", str(image)])

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert f"Partition {SHOWN} is in an RDB that does not open" in out

    @staticmethod
    def _two_sizes(tmp_path, second):
        first = _image(tmp_path, [(NAME, DOS3)], name="a.hdf")
        later = _image(tmp_path, [(drive, DOS3) for drive in second],
                       block_bytes=1024, name="b.hdf")
        return _make_mbr(tmp_path, [(64, first.read_bytes()),
                                    (256, later.read_bytes())])

    def test_partition_still_matches_the_raw_name(self, tmp_path):
        image = _image(tmp_path, [(NAME, DOS3)])

        assert find_partition(image, None, NAME).name == NAME
        assert fuse_fs.get_partition_info(image, None, NAME)["name"] == NAME


@pytest.fixture
def temp_dir(monkeypatch, tmp_path):
    """Where RDB drivers are extracted."""
    import tempfile

    folder = tmp_path / "temp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


@pytest.fixture
def bridge(fuse_mock, monkeypatch):
    """The bridge of ls and verify, holding a file and a directory with NAME
    in their names on a volume named NAME."""
    bridge = _make_mock_bridge()
    bridge.list_dir_path.side_effect = lambda path: [] if path != "/" else [
        {"name": f"a{NAME}.txt", "dir_type": -3, "size": 2, "protection": 0},
        {"name": f"d{NAME}", "dir_type": 2, "size": 0, "protection": 0},
    ]
    bridge.volume_name.return_value = f"V{NAME}"
    monkeypatch.setattr(fuse_fs, "_create_bridge_from_args",
                        lambda args, cmd, read_only=True: (bridge, None))
    return bridge


def _args(**overrides):
    return argparse.Namespace(**{
        "image": Path("test.hdf"), "json": False, "path": "/", "recursive": False,
        "file": None, "expect_size": None, "partition": None, "driver": None,
        "block_size": None, "debug": False, **overrides})


class TestFileCommands:
    """File and volume names in ls and verify (#291)."""

    @pytest.mark.parametrize("recursive", [False, True], ids=["flat", "recursive"])
    def test_ls(self, bridge, capsys, recursive):
        fuse_fs.cmd_ls(_args(recursive=recursive))

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert f"  a{SHOWN}.txt" in out
        assert f"  d{SHOWN}" in out

    def test_ls_keeps_the_column_after_the_escaped_name(self, bridge, capsys):
        fuse_fs.cmd_ls(_args())

        first = capsys.readouterr().out.splitlines()[0]
        assert first.startswith(f"  {f'a{SHOWN}.txt':30s}  {2:>10d}  ")

    def test_ls_json_keeps_the_raw_bytes(self, bridge, capsys):
        fuse_fs.cmd_ls(_args(json=True))

        entries = json.loads(capsys.readouterr().out)["entries"]
        assert entries[0]["name"] == f"a{NAME}.txt"
        assert entries[0]["path"] == f"/a{NAME}.txt"

    def test_verify(self, bridge, capsys):
        fuse_fs.cmd_verify(_args())

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert f"Volume: V{SHOWN}\n" in out

    def test_verify_json_keeps_the_raw_bytes(self, bridge, capsys):
        fuse_fs.cmd_verify(_args(json=True))

        assert json.loads(capsys.readouterr().out)["volume"] == f"V{NAME}"


class TestDiskRDBMessages:
    """Messages added beside #346 that name the image's partitions."""

    def test_overlap_refusal_names_the_own_partition_escaped(self, tmp_path):
        from tests.unit.test_parceiro_mbr_overlap import (
            ENTRY_AT_256, OVERLAP, _open_as, _parceiro_76)

        image = _parceiro_76(tmp_path, (1, 39), own_name=NAME)
        with pytest.raises(ValueError) as caught:
            _open_as(image, partition="DH1")
        assert str(caught.value) == (
            f"{OVERLAP}: {ENTRY_AT_256} overlaps partition {SHOWN} "
            "of the disk's own RDB")

    def test_entry_partitions_hint_escapes_and_json_keeps_raw(
            self, tmp_path, capsys, user_driver_dir):
        from tests.unit.test_entry_partitions_hint import _failed_disk

        image = _failed_disk(tmp_path, (NAME,))
        with pytest.raises(SystemExit) as caught:
            fuse_fs.main(["ls", str(image)])
        message = str(caught.value)
        assert message.endswith(
            f"still open, read-only: {SHOWN}. Open one with --partition '{SHOWN}'.")
        assert "\x1b" not in message

        with pytest.raises(SystemExit):
            fuse_fs.main(["ls", str(image), "--json"])
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["details"] == {"partitions": [NAME]}


ISO_ESC = "CD\x1b[2J\x9b2J\xe9X"
ISO_SHOWN = "CD\\x1b[2J\\x9b2J\\xe9X"


def _iso(tmp_path):
    """An ISO 9660 image whose volume ID holds ESC [ 2 J."""
    pvd = bytearray(b" " * 2048)
    pvd[:6] = b"\x01CD001"
    pvd[40:40 + len(ISO_ESC)] = ISO_ESC.encode("latin-1")
    image = tmp_path / "esc.iso"
    image.write_bytes(bytes(16 * 2048) + bytes(pvd) + bytes(2 * 2048))
    return image


class TestIsoVolumeId:
    """The volume ID of an ISO 9660 image (#361)."""

    def _inspect(self, image, use_json=False):
        fuse_fs.cmd_inspect(argparse.Namespace(
            image=image, json=use_json, block_size=None, full=False,
            debug=False, partition=None))

    def test_inspect(self, tmp_path, capsys):
        self._inspect(_iso(tmp_path))

        out = capsys.readouterr().out
        assert f"Volume ID: {ISO_SHOWN}" in out
        assert "\x1b" not in out

    def test_inspect_json_keeps_the_raw_bytes(self, tmp_path, capsys):
        self._inspect(_iso(tmp_path), use_json=True)

        assert json.loads(capsys.readouterr().out)["volume_id"] == ISO_ESC

    def test_driver_message(self, tmp_path):
        image = _iso(tmp_path)

        with pytest.raises(DriverNotFound) as excinfo:
            fuse_fs.resolve_driver(image, None, None, None,
                                   iso_info=rdb_inspect.detect_iso(image))

        assert f"(volume: {ISO_SHOWN})" in str(excinfo.value)
        assert "\x1b" not in str(excinfo.value)

    def test_backend_description(self, tmp_path):
        image = _iso(tmp_path)
        backend = BlockDeviceBackend(
            image, read_only=True, iso_info=rdb_inspect.detect_iso(image))
        with ExitStack() as stack:
            stack.callback(backend.close)
            backend.open()
            text = backend.describe()

        assert f"ISO 9660 ({ISO_SHOWN})" in text
        assert "\x1b" not in text

    def test_detection_keeps_the_raw_bytes(self, tmp_path):
        assert rdb_inspect.detect_iso(_iso(tmp_path)).volume_id == ISO_ESC
