"""The bundled FastFileSystem on FFS/OFS volumes, with no --driver.

The images are built here with amitools' xdftool and rdbtool, so these
tests need no AmiFUSE-testing checkout. They are not integration-marked:
they run in the integration job on every OS and skip only when machine68k
does not work. The mount tests are fuse-marked and run in the Mount Tests
job, which installs WinFsp on Windows.

Each test points the user's drivers folder (LOCALAPPDATA on Windows, HOME
elsewhere) at an empty folder under tmp_path, so the bundled handler is the
one under test; subprocesses inherit that environment.

Writes stay within what works with every handler on main: whole files
under 64 KiB, no truncate, no seek past the end, no os.replace, and no
file rename, which the bundled v0.2 handler refuses for an open or
locked object (ERROR_OBJECT_IN_USE).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from amifuse import driver_resolver
from amifuse.fuse_fs import HandlerBridge, format_volume, resolve_driver
from amifuse.rdb_inspect import detect_adf

REPO_ROOT = Path(__file__).resolve().parents[2]
AMITOOLS_ROOT = REPO_ROOT / "amitools"

ROOT_DATA = b"Bundled FastFileSystem seed file\n"
MULTI_DATA = bytes((i * 7) % 251 for i in range(3000))  # several blocks, OFS and FFS
NEW_DATA = bytes((i * 13 + 5) % 251 for i in range(20000))

# A partition that straddles 4 GiB: cylinders of 16 heads x 63 sectors x
# 512 bytes, partition DH0 on cylinders 8300-8363 (4.28-4.32 GB).
BIG_CHS = (10400, 16, 63)
BIG_START_CYL, BIG_CYLS = 8300, 64


@pytest.fixture(scope="module", autouse=True)
def _require_machine68k():
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")


@pytest.fixture
def no_user_driver(monkeypatch, tmp_path):
    """An empty user drivers folder and no dev fallback.

    The dev fallback is CI's AmiFUSE-testing clone. A subprocess still sees
    it, but it comes after the bundled handler, which serves DOS0-DOS7.
    """
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setattr(
        driver_resolver, "DEV_FALLBACK_DRIVER", tmp_path / "no-dev" / "FastFileSystem")
    assert not (driver_resolver.user_driver_dir() / "FastFileSystem").exists()


def _amitool(module: str, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"{module} {args} failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def _xdftool(image: Path, *args: str) -> str:
    return _amitool("amitools.tools.xdftool", str(image), *args)


def _xdf_read(image: Path, amiga_path: str, tmp_path: Path, *open_args: str) -> bytes:
    out = tmp_path / f"xdf-{amiga_path.replace('/', '_')}"
    _xdftool(image, *open_args, "read", amiga_path, str(out))
    return out.read_bytes()


def _seed_adf(tmp_path: Path, dos: str, hd: bool = False) -> Path:
    """An ADF with Root.txt and Sub/Multi.bin, made by xdftool."""
    src = tmp_path / "seed"
    src.mkdir(exist_ok=True)
    (src / "Root.txt").write_bytes(ROOT_DATA)
    (src / "Multi.bin").write_bytes(MULTI_DATA)
    image = tmp_path / f"{dos}{'-hd' if hd else ''}.adf"
    create = ["create", "type=adf_hd", "+"] if hd else []
    _xdftool(
        image, *create, "format", "Seed", dos,
        "+", "makedir", "Sub",
        "+", "write", str(src / "Root.txt"), "Root.txt",
        "+", "write", str(src / "Multi.bin"), "Sub/Multi.bin",
    )
    return image


def _write_file(bridge, path: str, data: bytes):
    opened = bridge.open_file(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    assert opened is not None, f"open_file failed for {path}"
    fh_addr, parent_lock = opened
    try:
        assert bridge.write_handle(fh_addr, data) == len(data)
    finally:
        bridge.close_file(fh_addr)
        if parent_lock:
            bridge.free_lock(parent_lock)


def _amifuse(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "amifuse", *args],
        capture_output=True, text=True, check=False,
    )


def _big_partition_image(tmp_path: Path) -> Path:
    """A sparse RDB image whose DOS3 partition DH0 ends past 4 GiB."""
    cyls, heads, secs = BIG_CHS
    image = tmp_path / "big.hdf"
    image.write_bytes(b"")
    if sys.platform.startswith("win"):
        # Extending a file on NTFS writes zeros unless it is marked sparse.
        subprocess.run(["fsutil", "sparse", "setflag", str(image)],
                       check=True, capture_output=True)
    with open(image, "r+b") as f:
        f.seek(cyls * heads * secs * 512 - 1)
        f.write(b"\0")
    _amitool(
        "amitools.tools.rdbtool", str(image),
        "open", f"chs={cyls},{heads},{secs}", "+", "init", "rdb_cyls=2",
        "+", "add", f"start={BIG_START_CYL}", f"size={BIG_CYLS}", "name=DH0", "fs=DOS3",
    )
    return image


# ---------------------------------------------------------------------------
# A. HandlerBridge per DOS type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dos, hd", [
    ("DOS0", False), ("DOS1", False), ("DOS2", False),
    ("DOS3", False), ("DOS4", False), ("DOS5", False),
    ("DOS6", False), ("DOS7", False), ("DOS3", True),
], ids=["DOS0", "DOS1", "DOS2", "DOS3", "DOS4", "DOS5", "DOS6", "DOS7", "DOS3-HD"])
def test_bridge_reads_and_writes_without_driver(tmp_path, no_user_driver, dos, hd):
    """List and read xdftool's files, then create, mkdir, delete and flush;
    xdftool reads the result back."""
    image = _seed_adf(tmp_path, dos, hd)
    adf_info = detect_adf(image)
    resolved = resolve_driver(image, None, None, None, adf_info=adf_info)
    assert resolved.source == "bundled"

    bridge = HandlerBridge(image, resolved.path, read_only=False, adf_info=adf_info)
    try:
        names = {e["name"] for e in bridge.list_dir_path("/")}
        assert {"Root.txt", "Sub"} <= names
        assert {e["name"] for e in bridge.list_dir_path("/Sub")} == {"Multi.bin"}
        assert bridge.read_file("/Sub/Multi.bin", len(MULTI_DATA) + 1, 0) == MULTI_DATA

        _write_file(bridge, "/Sub/New.bin", NEW_DATA)
        root_lock, res2 = bridge.locate(0, "")
        assert root_lock, f"locate root failed: {res2}"
        try:
            lock, res2 = bridge.create_dir(root_lock, "NewDir")
            assert lock, f"create_dir failed: {res2}"
            bridge.free_lock(lock)
            res1, res2 = bridge.delete_object(root_lock, "Root.txt")
            assert res1, f"delete failed: {res2}"
        finally:
            bridge.free_lock(root_lock)
        bridge.flush_volume()
    finally:
        bridge.close()

    listing = _xdftool(image, "list")
    assert "NewDir" in listing
    assert "Root.txt" not in listing
    assert _xdf_read(image, "Sub/New.bin", tmp_path) == NEW_DATA
    assert _xdf_read(image, "Sub/Multi.bin", tmp_path) == MULTI_DATA


def test_long_name_on_dos7(tmp_path, no_user_driver):
    """A DOS7 volume keeps a name longer than the 30 bytes DOS0-DOS5 allow;
    xdftool lists the file and reads it back."""
    image = _seed_adf(tmp_path, "DOS7")
    adf_info = detect_adf(image)
    resolved = resolve_driver(image, None, None, None, adf_info=adf_info)
    assert resolved.source == "bundled"
    name = "A name longer than the 30 bytes of DOS0-DOS5.bin"
    bridge = HandlerBridge(image, resolved.path, read_only=False, adf_info=adf_info)
    try:
        _write_file(bridge, "/" + name, NEW_DATA)
        assert name in {e["name"] for e in bridge.list_dir_path("/")}
        bridge.flush_volume()
    finally:
        bridge.close()
    assert name in _xdftool(image, "list")
    assert _xdf_read(image, name, tmp_path) == NEW_DATA


def test_bridge_starts_quickly(tmp_path, no_user_driver):
    """The handler starts in under 2 s (timer.device completes on time)."""
    image = _seed_adf(tmp_path, "DOS1")
    adf_info = detect_adf(image)
    resolved = resolve_driver(image, None, None, None, adf_info=adf_info)
    start = time.monotonic()
    bridge = HandlerBridge(image, resolved.path, read_only=True, adf_info=adf_info)
    elapsed = time.monotonic() - start
    bridge.close()
    assert elapsed < 2.0


# ---------------------------------------------------------------------------
# B. File commands on DOS1
# ---------------------------------------------------------------------------


class TestCliWithoutDriver:
    """ls, read, write and verify resolve the bundled handler themselves."""

    def test_ls(self, tmp_path, no_user_driver):
        result = _amifuse("ls", str(_seed_adf(tmp_path, "DOS1")), "--path", "Sub", "--json")
        assert result.returncode == 0, result.stdout + result.stderr
        entries = json.loads(result.stdout)["entries"]
        assert [e["name"] for e in entries] == ["Multi.bin"]

    def test_read(self, tmp_path, no_user_driver):
        out = tmp_path / "out.bin"
        result = _amifuse("read", str(_seed_adf(tmp_path, "DOS1")),
                          "--file", "Sub/Multi.bin", "--out", str(out))
        assert result.returncode == 0, result.stdout + result.stderr
        assert out.read_bytes() == MULTI_DATA

    def test_verify(self, tmp_path, no_user_driver):
        result = _amifuse("verify", str(_seed_adf(tmp_path, "DOS1")),
                          "--file", "Sub/Multi.bin", "--expect-size", str(len(MULTI_DATA)),
                          "--json")
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["status"] == "ok"

    def test_write(self, tmp_path, no_user_driver):
        image = _seed_adf(tmp_path, "DOS1")
        src = tmp_path / "new.bin"
        src.write_bytes(NEW_DATA)
        result = _amifuse("write", str(image), "--file", "Sub/Cli.bin", "--in", str(src))
        assert result.returncode == 0, result.stdout + result.stderr
        assert _xdf_read(image, "Sub/Cli.bin", tmp_path) == NEW_DATA

    def test_inspect_names_the_bundled_handler(self, tmp_path, no_user_driver):
        """In the environment the commands above run in, the bundled
        handler serves DOS1, not the dev fallback CI has beside the repo."""
        result = _amifuse("inspect", "--json", str(_seed_adf(tmp_path, "DOS1")))
        assert result.returncode == 0, result.stdout + result.stderr
        driver = json.loads(result.stdout)["driver"]
        assert driver["source"] == "bundled", driver
        assert Path(driver["path"]) == driver_resolver.BUNDLED_DRIVER


# ---------------------------------------------------------------------------
# C. Hard disks: an RDB partition with no filesystem driver
# ---------------------------------------------------------------------------


def test_format_rdb_partition_without_driver(tmp_path, no_user_driver):
    """format uses the bundled handler for a DOS3 partition the RDB has no
    driver for; xdftool lists the new volume."""
    image = tmp_path / "disk.hdf"
    _amitool(
        "amitools.tools.rdbtool", str(image),
        "create", "size=8Mi", "+", "init", "rdb_cyls=2",
        "+", "add", "size=6MiB", "name=DH0", "fs=DOS3",
    )
    result = _amifuse("format", str(image), "DH0", "Fresh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Filesystem driver: FastFileSystem 0.2 (21.10.2026) (bundled" in result.stdout
    listing = _xdftool(image, "open", "part=DH0", "+", "list")
    assert "Fresh" in listing and "DOS3" in listing


def test_partition_beyond_4gib(tmp_path, no_user_driver, capsys):
    """A DOS3 partition that ends past 4 GiB formats, writes and reads back.

    The handler takes its 64-bit path only for such a partition. The root
    block, in the middle of the partition, lies past 4 GiB, and is read
    straight from the image at its expected offset.
    """
    image = _big_partition_image(tmp_path)
    format_volume(image, None, None, "DH0", volname="Big")
    assert "(bundled," in capsys.readouterr().out

    resolved = resolve_driver(image, None, None, "DH0")
    bridge = HandlerBridge(image, resolved.path, read_only=False, partition="DH0")
    try:
        _write_file(bridge, "/New.bin", NEW_DATA)
        bridge.flush_volume()
    finally:
        bridge.close()

    bridge = HandlerBridge(image, resolved.path, read_only=True, partition="DH0")
    try:
        assert bridge.read_file("/New.bin", len(NEW_DATA) + 1, 0) == NEW_DATA
    finally:
        bridge.close()

    _, heads, secs = BIG_CHS
    blocks_per_cyl = heads * secs
    num_blocks = BIG_CYLS * blocks_per_cyl
    reserved = 2
    root = BIG_START_CYL * blocks_per_cyl + (reserved + num_blocks - 1) // 2
    assert root * 512 > 4 * 1024 ** 3
    with open(image, "rb") as f:
        f.seek(root * 512)
        block = f.read(512)
    assert int.from_bytes(block[0:4], "big") == 2  # T_HEADER
    assert int.from_bytes(block[508:512], "big", signed=True) == 1  # ST_ROOT
    assert block[433:433 + block[432]] == b"Big"
    assert _xdf_read(image, "New.bin", tmp_path, "open", "part=DH0", "+") == NEW_DATA


# ---------------------------------------------------------------------------
# D. Mounts (fuse-marked; the WinFsp proof on Windows)
# ---------------------------------------------------------------------------


def _unmount(mountpoint, proc, timeout: float = 15.0) -> str:
    """Unmount and return what the mount process printed to stdout."""
    result = _amifuse("unmount", str(mountpoint))
    assert result.returncode == 0, result.stdout + result.stderr
    proc.wait(timeout=timeout)
    if proc.stdout is None:
        return ""
    return proc.stdout.read().decode(errors="replace")


@pytest.mark.fuse
class TestMountWithoutDriver:
    """mount resolves the bundled handler; writes land in the image."""

    def test_mount_read_write_unmount(self, mount_image, tmp_path, no_user_driver):
        image = _seed_adf(tmp_path, "DOS1")
        proc, mp = mount_image(image, driver=None, extra_args=["--write"])
        mp = Path(mp)
        assert sorted(os.listdir(mp)) == ["Root.txt", "Sub"]
        assert (mp / "Sub" / "Multi.bin").read_bytes() == MULTI_DATA
        (mp / "Sub" / "Mount.bin").write_bytes(NEW_DATA)
        out = _unmount(mp, proc)
        assert "Filesystem driver: FastFileSystem 0.2 (21.10.2026) (bundled," in out, out
        assert _xdf_read(image, "Sub/Mount.bin", tmp_path) == NEW_DATA

    def test_write_survives_a_killed_mount(self, mount_image, tmp_path, no_user_driver):
        """The handler writes back on its own timer, so a mount killed 5 s
        after a write keeps the file. The kill loses the buffered banner;
        the test above shows that this environment mounts the bundled
        handler."""
        image = _seed_adf(tmp_path, "DOS1")
        proc, mp = mount_image(image, driver=None, extra_args=["--write"])
        (Path(mp) / "Kept.bin").write_bytes(NEW_DATA)
        time.sleep(5)
        proc.kill()
        proc.wait(timeout=15)
        assert "Kept.bin" in _xdftool(image, "list")
        assert _xdf_read(image, "Kept.bin", tmp_path) == NEW_DATA

    @pytest.mark.skip(reason=(
        "FastFileSystem v0.2 refuses to rename an open or locked object "
        "(ERROR_OBJECT_IN_USE), and Windows opens a file before renaming it; "
        "remove this skip when the pinned release fixes it"
    ))
    def test_file_rename(self, mount_image, tmp_path, no_user_driver):
        image = _seed_adf(tmp_path, "DOS1")
        proc, mp = mount_image(image, driver=None, extra_args=["--write"])
        os.rename(Path(mp) / "Root.txt", Path(mp) / "Renamed.txt")
        _unmount(mp, proc)
        listing = _xdftool(image, "list")
        assert "Renamed.txt" in listing and "Root.txt" not in listing

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher argv")
    def test_launcher_argv(self, fuse_available, tmp_path, no_user_driver):
        """The command a double-click runs mounts a DOS1 ADF read-write."""
        from tests.integration import test_launcher_e2e as e2e

        image = _seed_adf(tmp_path, "DOS1")
        drive = e2e._find_free_drive()
        proc = subprocess.Popen(
            [sys.executable, "-m", "amifuse", "mount", "--write",
             "--mountpoint", drive, "--daemon", str(image)],
            creationflags=e2e.DETACHED_FLAGS,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            assert e2e._wait_for_mount(drive), f"Drive {drive} did not become ready"
            root = Path(drive + "\\")
            assert (root / "Sub" / "Multi.bin").read_bytes() == MULTI_DATA
            (root / "Launcher.bin").write_bytes(NEW_DATA)
            _unmount(drive, proc)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            e2e._wait_for_unmount(drive)
        assert _xdf_read(image, "Launcher.bin", tmp_path) == NEW_DATA
