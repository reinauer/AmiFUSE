"""Unit tests for one object under several spellings in a write mount (#119).

Handlers find a name in any letter case, by a rule that depends on the volume
(HandlerBridge.name_fold), while the per-path state is kept under the path it
was first given. Each callback puts a path in the state's spelling first
(AmigaFuseFS._state_path). tests/integration/test_name_case_handlers.py checks
all of it on real handlers; these tests cover what no fixture has: other
disk types, a WinFsp mount, and a read-only one.
"""

import os
import threading
import types

import pytest

from tests.unit.test_rmdir_object_in_use import _InUseVolume


class _FoldingVolume(_InUseVolume):
    """An in-memory volume that folds names as `fold` says, as handlers do."""

    def __init__(self, dirs, files, fold="latin1"):
        super().__init__(dirs, files)
        self.fold = fold

    def name_fold(self):
        return self.fold


@pytest.fixture
def make_fs(fuse_mock):
    from amifuse.fuse_fs import AmigaFuseFS

    def make(dirs=("/",), files=(), fold="latin1", volume_cls=_FoldingVolume):
        volume = volume_cls(dirs, files, fold)
        return AmigaFuseFS(volume, debug=False, icons=False), volume

    return make


def _open_entry(fs, volume, path, **fields):
    handle = fs._next_fh
    fs._next_fh += 1
    fs._fh_cache[handle] = {
        "fh_addr": volume.open(path), "parent_lock": 0, "pos": None,
        "lock": threading.Lock(), "closed": False, "dirty": False, "path": path,
        **fields,
    }
    return handle


# What is one name


@pytest.mark.parametrize("fold, a, b, same", [
    ("ascii", "/abc", "/ABC", True),
    ("ascii", "/\xe9", "/\xc9", False),
    ("latin1", "/abc", "/ABC", True),
    ("latin1", "/\xe9", "/\xc9", True),
    ("latin1", "/\xfe", "/\xde", True),
    ("latin1", "/\xf7", "/\xd7", False),  # the division and multiplication signs
    ("latin1", "/\xff", "/\xdf", False),  # y with diaeresis has no capital here
    ("latin1", "/\xb5", "/Μ", False),  # lower() and upper() relate these
    ("latin1", "/K", "/k", False),  # the Kelvin sign, which lower() folds
    (None, "/abc", "/ABC", False),
])
def test_names_fold_by_the_volumes_rule(make_fs, fold, a, b, same):
    fs, _ = make_fs(fold=fold)
    assert (fs._folded(a) == fs._folded(b)) is same


def test_unknown_answer_from_a_bridge_leaves_names_exact(make_fs):
    fs, _ = make_fs(fold=object())
    assert fs._folded("/abc") == "/abc"


def test_read_only_mount_asks_the_handler_nothing(fuse_mock):
    from amifuse.fuse_fs import AmigaFuseFS

    def no_packet():
        raise AssertionError("a read-only mount keeps no per-path state")

    volume = _FoldingVolume({"/"}, ())
    volume._write_enabled = False
    volume.name_fold = no_packet

    fs = AmigaFuseFS(volume, debug=False, icons=False)

    assert fs._name_fold is None
    fs._pending_deletes.add("/abc")
    assert fs._state_path("/ABC") == "/ABC"


# How a volume folds its names


DOS = 0x444F5300
PFS3 = 0x50465303
PDS3 = 0x50445303
SFS0 = 0x53465300
SFS2 = 0x53465302
NBR7 = 0x4E425207


def _sfs_root(bits=0, block_id=b"SFS\0"):
    return block_id + bytes(16) + bytes([bits]) + bytes(491)


def _bridge(dos_type, disk_type=None, root=None):
    from amifuse.fuse_fs import HandlerBridge

    bridge = HandlerBridge.__new__(HandlerBridge)
    bridge._dos_type = dos_type
    bridge._first_block = None if root is None else 504
    info = None if disk_type is None else {"disk_type": disk_type}
    bridge.get_disk_info = lambda: info
    if isinstance(root, Exception):
        def read_blocks(block, count):
            raise root
    else:
        def read_blocks(block, count):
            assert (block, count) == (504, 1)
            return root
    bridge.backend = types.SimpleNamespace(read_blocks=read_blocks)
    return bridge


@pytest.mark.parametrize("variant, fold", [
    (0, "ascii"), (1, "ascii"), (2, "latin1"), (3, "latin1"), (4, "latin1"),
    (5, "latin1"), (6, "latin1"), (7, "latin1"), (8, None),
])
def test_dos_volume_folds_by_its_type(fuse_mock, variant, fold):
    assert _bridge(DOS + variant, DOS + variant).name_fold() == fold


def test_dos_volume_goes_by_the_disk_not_the_partition(fuse_mock):
    """FastFileSystem goes by the disk, whatever the RDB says of the partition."""
    assert _bridge(DOS + 1, DOS + 3).name_fold() == "latin1"
    assert _bridge(DOS + 3, DOS + 1).name_fold() == "ascii"


@pytest.mark.parametrize("disk_type", [None, 0xFFFFFFFF, 0x42555359],
                         ids=["no-answer", "no-disk", "busy"])
def test_dos_volume_goes_by_the_partition_without_a_disk_type(fuse_mock, disk_type):
    assert _bridge(DOS + 3, disk_type).name_fold() == "latin1"


@pytest.mark.parametrize("dos_type", [PFS3, PDS3])
def test_pfs_volume_folds_latin1_whatever_it_reports(fuse_mock, dos_type):
    assert _bridge(dos_type, DOS + 3).name_fold() == "latin1"
    assert _bridge(dos_type, DOS + 1).name_fold() == "latin1"


@pytest.mark.parametrize("dos_type", [SFS0, SFS2])
def test_sfs_volume_folds_latin1_unless_formatted_case_sensitive(fuse_mock, dos_type):
    assert _bridge(dos_type, SFS0, _sfs_root()).name_fold() == "latin1"
    assert _bridge(dos_type, SFS0, _sfs_root(bits=0x40)).name_fold() == "latin1"
    assert _bridge(dos_type, SFS0, _sfs_root(bits=0x80)).name_fold() is None


@pytest.mark.parametrize("root", [
    _sfs_root(block_id=b"\0\0\0\0"),  # not a root block
    OSError("unreadable"),
], ids=["not-a-root-block", "unreadable"])
def test_sfs_volume_whose_root_block_says_nothing_stays_exact(fuse_mock, root):
    assert _bridge(SFS0, SFS0, root).name_fold() is None


@pytest.mark.parametrize("dos_type, disk_type", [
    (NBR7, DOS + 1),  # BFFS reports DOS\1
    (0x46415401, 0x46415401),  # FAT\1, and CrossDOS reports its own type
    (None, None),  # ISO 9660, or a bridge made without an image
], ids=["bffs", "fat", "no-type"])
def test_any_other_volume_stays_exact(fuse_mock, dos_type, disk_type):
    assert _bridge(dos_type, disk_type).name_fold() is None


# The state's spelling


def test_path_takes_the_spelling_of_the_longest_part_the_state_knows(make_fs):
    fs, volume = make_fs()
    _open_entry(fs, volume, "/Dir/Sub/File")

    assert fs._state_path("/DIR/SUB/FILE") == "/Dir/Sub/File"
    assert fs._state_path("/dir/sub/other") == "/Dir/Sub/other"
    assert fs._state_path("/DIR/Other/File") == "/Dir/Other/File"
    assert fs._state_path("/Elsewhere") == "/Elsewhere"
    assert fs._state_path("/") == "/"


def test_path_follows_every_kind_of_state(make_fs):
    fs, volume = make_fs()
    fs._pinned_stats.add("/Pinned")
    fs._pending_deletes.add("/Pending")
    fs._pending_dir_deletes["/Waiting"] = {"/Waiting/x"}
    fs._held_dates["/Dated"] = 1.0
    fs.opendir("/DirHandle")

    for path in ("/Pinned", "/Pending", "/Waiting", "/Dated", "/DirHandle"):
        assert fs._state_path(path.upper()) == path


def test_exact_volume_keeps_every_spelling_apart(make_fs):
    fs, volume = make_fs(fold=None)
    fs._pinned_stats.add("/abc")

    assert fs._state_path("/ABC") == "/ABC"


def test_accented_name_keeps_its_spelling_on_an_ascii_volume(make_fs):
    fs, volume = make_fs(fold="ascii")
    _open_entry(fs, volume, "/\xe9.bin")

    assert fs._state_path("/\xc9.BIN") == "/\xc9.BIN"
    assert fs._state_path("/\xe9.BIN") == "/\xe9.bin"


def test_pending_filter_goes_by_the_volumes_rule(make_fs):
    fs, _ = make_fs(fold="ascii")
    fs._pending_deletes.add("/\xe9.bin")

    assert fs._filter_pending_deletes("/", ["\xe9.BIN", "\xc9.bin"]) == ["\xc9.bin"]


def test_new_name_keeps_its_spelling_under_the_states_directory(make_fs):
    fs, volume = make_fs(dirs={"/", "/d"}, files={"/d/x"})
    handle = _open_entry(fs, volume, "/d/x")
    sent = []
    fs._rename_object = lambda old, new: sent.append((old, new)) or (-1, 0)

    fs.rename("/D/X", "/D/New")

    assert sent == [("/d/x", "/d/New")]
    assert handle in fs._fh_cache


def test_rename_to_the_states_own_spelling_moves_nothing(make_fs):
    fs, volume = make_fs(dirs={"/"}, files={"/e.bin"})
    volume.rename_object = lambda *args: (-1, 0)
    volume.locate_path = lambda path: (0, 0, [])
    handle = _open_entry(fs, volume, "/e.bin")
    fs._pin_stat("/e.bin", 5)

    assert fs.rename("/E.BIN", "/e.bin") == 0

    assert fs.getattr("/E.BIN")["st_size"] == 5
    assert fs._fh_cache[handle]["path"] == "/e.bin"


# WinFsp goes on passing the name a file was opened under after a rename


def test_winfsp_delete_by_the_opened_name_in_another_spelling(make_fs, monkeypatch):
    from amifuse.fuse_fs import AmigaFuseFS

    monkeypatch.setattr(AmigaFuseFS, "_winfsp", True)
    fs, volume = make_fs(dirs={"/"}, files={"/renamed"})
    _open_entry(fs, volume, "/renamed", opened_as="/Opened")

    assert fs.unlink("/OPENED") == 0

    assert fs._pending_deletes == {"/renamed"}


def test_winfsp_date_for_the_opened_name_in_another_spelling(make_fs, monkeypatch):
    from amifuse.fuse_fs import AmigaFuseFS

    monkeypatch.setattr(AmigaFuseFS, "_winfsp", True)
    fs, volume = make_fs(dirs={"/"}, files={"/renamed"})
    _open_entry(fs, volume, "/renamed", opened_as="/Opened")

    assert fs.utimens("/OPENED", (1e9, 1e9)) == 0

    assert fs._held_dates == {"/renamed": 1e9}


def test_open_stores_the_states_spelling(make_fs):
    fs, volume = make_fs(dirs={"/"}, files={"/e.bin"})
    volume.open_file_reply = lambda path, flags: ((volume.open(path), 0), 0)
    first = _open_entry(fs, volume, "/e.bin")

    second = fs.open("/E.BIN", os.O_RDONLY)

    assert fs._fh_cache[second]["path"] == fs._fh_cache[first]["path"] == "/e.bin"
