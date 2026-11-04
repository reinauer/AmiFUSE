"""One object under several spellings, on real handlers (#119).

AmigaDOS handlers find a name in any letter case, so a host program can reach
one object under several spellings, and the per-path state of a write mount
has to treat them as one. How a volume folds names depends on its handler and
its type, and tests/unit/test_name_fold.py covers the rule. The tests call the
AmigaFuseFS callbacks directly, as a mount would, on copies of the fixture
images and on ADFs the bundled FastFileSystem serves. No host FUSE driver is
needed.
"""

import errno
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from amifuse.driver_resolver import BUNDLED_DRIVER
from amifuse.fuse_fs import AmigaFuseFS, FuseOSError, HandlerBridge
from amifuse.rdb_inspect import detect_adf

AMITOOLS_ROOT = Path(__file__).resolve().parents[2] / "amitools"
BUNDLED = "bundled FastFileSystem"

HANDLERS = [
    pytest.param(("pfs.hdf", "pfs3aio"), marks=pytest.mark.integration, id="pfs3aio"),
    pytest.param(("sfs.hdf", "SmartFilesystem"), marks=pytest.mark.integration,
                 id="SmartFilesystem"),
    pytest.param(("ofs.adf", "FastFileSystem"), marks=pytest.mark.integration,
                 id="FastFileSystem"),
    # Not integration-marked, as in test_bundled_ffs: the image is built here.
    pytest.param((None, BUNDLED), id="bundled"),
]
# The bundled FastFileSystem refuses to move an open file or its directory.
MOVE_WHAT_IS_OPEN = HANDLERS[:3]


def _adf(tmp_path: Path, dos_type: str = "DOS3") -> Path:
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")
    image = tmp_path / f"{dos_type}.adf"
    proc = subprocess.run(
        [sys.executable, "-m", "amitools.tools.xdftool", str(image), "format", "Gen", dos_type],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return image


def _fixture_copy(request, tmp_path, image_name, driver_name):
    fixture_root = request.getfixturevalue("fixture_root")
    source = fixture_root / "fixtures" / "readonly" / image_name
    driver = fixture_root / "drivers" / driver_name
    if not source.exists() or not driver.exists():
        pytest.skip(f"Missing {image_name} or {driver_name}")
    image = tmp_path / image_name
    shutil.copy2(source, image)
    return image, driver


@pytest.fixture
def mount():
    """Open a writable AmigaFuseFS on an image; closed at the end of the test."""
    bridges = []

    def open_ops(image, driver):
        bridge = HandlerBridge(image, driver, read_only=False, adf_info=detect_adf(image))
        bridges.append(bridge)
        return AmigaFuseFS(bridge)

    yield open_ops
    for bridge in bridges:
        bridge.close()


@pytest.fixture(params=HANDLERS)
def ops(request, tmp_path, mount):
    image_name, driver_name = request.param
    if driver_name == BUNDLED:
        return mount(_adf(tmp_path), BUNDLED_DRIVER)
    return mount(*_fixture_copy(request, tmp_path, image_name, driver_name))


def _write(ops, path, data):
    fh = ops.create(path, 0o644)
    ops.write(path, data, 0, fh)
    ops.release(path, fh)


def _errno_of(call, *args):
    with pytest.raises(FuseOSError) as exc_info:
        call(*args)
    return exc_info.value.args[0]


def _renames(ops, old, new):
    """Whether the handler takes the rename. FastFileSystem can refuse to move
    what is open, and the state then stays where it is."""
    try:
        return ops.rename(old, new) == 0
    except FuseOSError as exc:
        assert exc.args[0] == errno.EBUSY
        return False


def test_file_open_from_create_answers_in_another_case(ops):
    fh = ops.create("/e.bin", 0o644)
    ops.write("/e.bin", b"x" * 300, 0, fh)

    assert ops.getattr("/E.BIN")["st_size"] == 300

    assert ops.unlink("/E.BIN") == 0  # deferred, as for /e.bin
    assert _errno_of(ops.getattr, "/E.bin") == errno.ENOENT
    assert ops.getattr("/e.bin", fh)["st_size"] == 300
    ops.release("/e.bin", fh)
    assert "e.bin" not in [n.lower() for n in ops.readdir("/", None)]


def test_delete_deferred_in_another_case_hides_the_stored_name(ops):
    _write(ops, "/Mixed.bin", b"m" * 40)
    fh = ops.open("/mixed.bin", os.O_RDONLY)

    assert ops.unlink("/mixed.bin") == 0

    assert "Mixed.bin" not in ops.readdir("/", None)
    assert _errno_of(ops.getattr, "/Mixed.bin") == errno.ENOENT
    ops.release("/mixed.bin", fh)
    assert "Mixed.bin" not in ops.readdir("/", None)


@pytest.mark.parametrize("ops", MOVE_WHAT_IS_OPEN, indirect=True)
def test_rename_in_another_case_carries_a_pending_delete(ops):
    """The handler renames d, as /D names it, so the delete has to follow.

    Left behind, the pending delete of /d/x fails at its release, and hides
    and deletes a file created at /d/x later, while the unlinked one survives.
    """
    ops.mkdir("/d", 0o755)
    _write(ops, "/d/x", b"old" * 10)
    fh = ops.open("/d/x", os.O_RDWR)
    assert ops.unlink("/d/x") == 0

    assert ops.rename("/D", "/e") == 0

    assert ops._pending_deletes == {"/e/x"}
    ops.release("/e/x", fh)
    assert ops.readdir("/e", None) == [".", ".."]

    ops.mkdir("/d", 0o755)
    fh = ops.create("/d/x", 0o644)
    ops.write("/d/x", b"new" * 10, 0, fh)
    assert ops.getattr("/d/x", fh)["st_size"] == 30
    ops.release("/d/x", fh)
    assert ops.bridge.read_file("/d/x", 100, 0) == b"new" * 10


def test_rmdir_in_another_case_waits_for_a_pending_child(ops):
    ops.mkdir("/gone", 0o755)
    _write(ops, "/gone/x", b"x")
    fh = ops.open("/gone/x", os.O_RDONLY)
    assert ops.unlink("/GONE/X") == 0

    assert ops.rmdir("/GONE") == 0

    assert ops._pending_dir_deletes == {"/gone": {"/gone/x"}}
    assert "gone" not in [n.lower() for n in ops.readdir("/", None)]
    ops.release("/gone/x", fh)
    assert ops._pending_deletes == set()
    assert ops.bridge.stat_path("/gone") is None


def test_file_stays_while_a_handle_under_another_spelling_is_open(ops):
    _write(ops, "/both.bin", b"b" * 20)
    first = ops.open("/both.bin", os.O_RDONLY)
    second = ops.open("/BOTH.BIN", os.O_RDONLY)
    assert ops.unlink("/both.bin") == 0

    ops.release("/both.bin", first)

    assert ops._pending_deletes == {"/both.bin"}
    assert ops.read("/BOTH.BIN", 20, 0, second) == b"b" * 20
    ops.release("/BOTH.BIN", second)
    assert ops._pending_deletes == set()
    assert "both.bin" not in [n.lower() for n in ops.readdir("/", None)]


def test_directory_spelled_two_ways_is_one_directory(ops):
    ops.mkdir("/dir", 0o755)
    _write(ops, "/dir/a", b"a")
    _write(ops, "/dir/b", b"b")
    first = ops.open("/dir/a", os.O_RDONLY)
    second = ops.open("/DIR/b", os.O_RDONLY)

    # Both handles have the directory under one spelling.
    assert {e["path"] for e in ops._fh_cache.values()} == {"/dir/a", "/dir/b"}
    if _renames(ops, "/Dir", "/moved"):
        assert {e["path"] for e in ops._fh_cache.values()} == {"/moved/a", "/moved/b"}
    assert ops.read("/dir/a", 1, 0, first) == b"a"
    assert ops.read("/dir/b", 1, 0, second) == b"b"
    ops.release("/dir/a", first)
    ops.release("/dir/b", second)


def test_date_held_for_an_open_file_in_another_case(ops):
    fh = ops.create("/dated.bin", 0o644)
    ops.write("/dated.bin", b"d", 0, fh)

    assert ops.utimens("/DATED.BIN", (981173106.0, 981173106.0)) == 0

    assert ops._held_dates == {"/dated.bin": 981173106.0}
    ops.release("/dated.bin", fh)
    assert ops.getattr("/dated.bin")["st_mtime"] == pytest.approx(981173106.0, abs=0.01)


def test_case_of_an_open_file_changes_by_rename(ops):
    fh = ops.create("/case.bin", 0o644)
    ops.write("/case.bin", b"c" * 7, 0, fh)

    renamed = _renames(ops, "/case.bin", "/CASE.bin")

    assert ops.getattr("/case.bin")["st_size"] == 7
    assert ops.getattr("/CASE.bin")["st_size"] == 7
    assert ops._fh_cache[fh]["path"] == ("/CASE.bin" if renamed else "/case.bin")
    ops.release("/CASE.bin", fh)
    assert ("CASE.bin" if renamed else "case.bin") in ops.readdir("/", None)


def test_rename_to_the_spelling_the_state_has_keeps_the_pin(ops):
    fh = ops.create("/kept.bin", 0o644)
    ops.write("/kept.bin", b"k" * 9, 0, fh)

    _renames(ops, "/KEPT.BIN", "/kept.bin")

    assert ops.getattr("/KEPT.bin")["st_size"] == 9
    ops.release("/kept.bin", fh)


@pytest.mark.parametrize("ops", MOVE_WHAT_IS_OPEN, indirect=True)
def test_rename_over_an_open_file_in_another_case(ops):
    _write(ops, "/src.bin", b"s" * 5)
    _write(ops, "/dst.bin", b"d" * 50)
    fh = ops.open("/dst.bin", os.O_RDONLY)

    assert ops.rename("/src.bin", "/DST.BIN") == 0

    assert ops.getattr("/dst.bin")["st_size"] == 5
    temp = ops._fh_cache[fh]["path"]  # the replaced file, deleted at its release
    assert temp.startswith("/.amifuse-")
    assert ops.read(temp, 50, 0, fh) == b"d" * 50
    ops.release(temp, fh)
    assert ops._pending_deletes == set()
    assert not [n for n in ops.readdir("/", None) if n.startswith(".amifuse-")]


@pytest.mark.parametrize("dos_type, fold, same", [
    ("DOS1", "ascii", False),
    ("DOS3", "latin1", True),
])
def test_accented_letter_folds_on_international_volumes_only(
        tmp_path, mount, dos_type, fold, same):
    ops = mount(_adf(tmp_path, dos_type), BUNDLED_DRIVER)
    assert ops.bridge.name_fold() == fold
    fh = ops.create("/\xe9.bin", 0o644)
    ops.write("/\xe9.bin", b"x" * 3, 0, fh)

    assert ops.getattr("/\xe9.BIN")["st_size"] == 3
    if same:
        assert ops.getattr("/\xc9.bin")["st_size"] == 3
    else:
        assert _errno_of(ops.getattr, "/\xc9.bin") == errno.ENOENT
    ops.release("/\xe9.bin", fh)


def _assert_rule_matches_the_handler(ops):
    pairs = (("a", "A"), ("\xe9", "\xc9"), ("\xfe", "\xde"), ("\xf7", "\xd7"),
             ("\xff", "\xdf"))
    for stored, other in pairs:
        name, spelled = f"/rule{stored}", f"/rule{other}"
        _write(ops, name, b"x")
        assert (ops.bridge.stat_path(spelled) is not None) == (
            ops._folded(name) == ops._folded(spelled)), (stored, other)
        assert ops.unlink(name) == 0


def test_fold_rule_matches_the_handler(ops):
    _assert_rule_matches_the_handler(ops)


@pytest.mark.parametrize("dos_type", [f"DOS{n}" for n in range(8)])
def test_fold_rule_matches_fastfilesystem_on_each_dos_type(tmp_path, mount, dos_type):
    _assert_rule_matches_the_handler(mount(_adf(tmp_path, dos_type), BUNDLED_DRIVER))


def _make_sfs_case_sensitive(image: Path):
    """Set ROOTBITS_CASESENSITIVE in both of the partition's root blocks."""
    data = bytearray(image.read_bytes())
    found = 0
    for offset in range(0, len(data), 512):
        if data[offset:offset + 4] != b"SFS\0":
            continue
        before = struct.unpack_from(">I", data, offset + 20)[0]
        data[offset + 20] |= 0x80
        after = struct.unpack_from(">I", data, offset + 20)[0]
        checksum = struct.unpack_from(">I", data, offset + 4)[0]
        struct.pack_into(">I", data, offset + 4, (checksum - after + before) & 0xFFFFFFFF)
        found += 1
    assert found == 2
    image.write_bytes(data)


@pytest.mark.integration
def test_case_sensitive_smartfilesystem_keeps_each_spelling_apart(
        request, tmp_path, mount):
    image, driver = _fixture_copy(request, tmp_path, "sfs.hdf", "SmartFilesystem")
    _make_sfs_case_sensitive(image)
    ops = mount(image, driver)
    assert ops.bridge.name_fold() is None
    _write(ops, "/abc.bin", b"lower")
    _write(ops, "/ABC.bin", b"UPPER")
    fh = ops.open("/abc.bin", os.O_RDONLY)

    assert ops.unlink("/abc.bin") == 0  # deferred or at once, as the handler answers
    assert "ABC.bin" in ops.readdir("/", None)
    assert ops.getattr("/ABC.bin")["st_size"] == 5
    ops.release("/abc.bin", fh)
    assert ops.bridge.read_file("/ABC.bin", 5, 0) == b"UPPER"
    assert ops.bridge.stat_path("/abc.bin") is None


@pytest.mark.integration
def test_bffs_is_left_exact(fixture_root):
    image = fixture_root / "fixtures" / "downloaded" / "netbsdamiga92.hdf"
    driver = fixture_root / "drivers" / "BFFSFilesystem"
    if not image.exists() or not driver.exists():
        pytest.skip("Missing netbsdamiga92.hdf or BFFSFilesystem")
    bridge = HandlerBridge(image, driver, read_only=True, partition="netbsd-root")
    try:
        assert bridge.name_fold() is None
    finally:
        bridge.close()


@pytest.mark.integration
def test_nfc_same_name_and_case_only_rename_keep_open_state(request, tmp_path, mount):
    image, driver = _fixture_copy(request, tmp_path, "pfs.hdf", "pfs3aio")
    ops = mount(image, driver)
    composed = "/caf\xe9"
    decomposed = "/cafe\u0301"
    fh = ops("create", composed, 0o644)
    ops("write", composed, b"kept", 0, fh)
    try:
        assert ops("rename", decomposed, composed) == 0
        assert ops("getattr", decomposed)["st_size"] == 4
        assert ops("rename", decomposed, "/CAF\xc9") == 0
        assert "CAF\xc9" in ops("readdir", "/", None)
        assert ops("read", decomposed, 4, 0, fh) == b"kept"
        assert ops._fh_cache[fh]["path"] == "/CAF\xc9"
    finally:
        ops("release", decomposed, fh)
    assert ops.bridge.read_file("/CAF\xc9", 4, 0) == b"kept"
