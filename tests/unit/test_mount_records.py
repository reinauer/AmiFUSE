"""A mount that is up records its mountpoint for status and unmount (#322).

`ps` can't identify a FUSE-T mount with no --mountpoint, with a relative one,
or with one like "/Volumes/Games --write". Process discovery runs here for
real, over `ps` and a stand-in process with such a command line; only the
mount table and the mount itself are stubbed, so nothing is mounted.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from amifuse import fuse_fs
from amifuse import platform as plat

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"),
                                reason="Windows has its own mount discovery")


@pytest.fixture
def record_dir(monkeypatch, tmp_path):
    directory = tmp_path / "records"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(plat, "_mount_record_dir", lambda: directory)
    # A real AmiFUSE volume on this machine would pair with a stand-in.
    monkeypatch.setattr(plat, "_enrich_null_mountpoints", lambda mounts: None)
    return directory


@pytest.fixture
def mount_process():
    """Start a stand-in for `amifuse mount` with the given arguments."""
    processes = []

    def start(*arguments):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)",
             "amifuse", "mount", *arguments])
        processes.append(process)
        # An interpreter that re-executes itself shows the arguments late.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(m["pid"] == process.pid for m in plat.find_amifuse_mounts()):
                return process.pid
            time.sleep(0.05)
        raise AssertionError("ps does not show the stand-in process")

    yield start
    for process in processes:
        process.kill()
        process.wait()


def _record(directory, pid, mountpoint, image="/Users/a/disk.hdf"):
    (directory / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "mountpoint": mountpoint, "image": image, "partition": None, "created_at": time.time()}))


def _discovered(pid):
    return next(m for m in plat.find_amifuse_mounts() if m["pid"] == pid)


# (arguments on the command line, mountpoint in the record, what `ps` shows)
CASES = {
    "no-mountpoint": (["disk.hdf"], "/Volumes/Work", None),
    "relative": (["disk.hdf", "--mountpoint", "mnt"], "/Users/a/mnt", "mnt"),
    "space-and-option": (["disk.hdf", "--mountpoint", "/Volumes/Games --write"],
                         "/Volumes/Games --write", "/Volumes/Games"),
}


@pytest.mark.parametrize("arguments, mountpoint, from_ps", CASES.values(), ids=CASES)
def test_status_shows_the_recorded_mountpoint(record_dir, mount_process, arguments,
                                              mountpoint, from_ps):
    pid = mount_process(*arguments)
    assert _discovered(pid)["mountpoint"] == from_ps

    _record(record_dir, pid, mountpoint)

    mount = _discovered(pid)
    assert mount["mountpoint"] == mountpoint
    assert mount["image"] == "/Users/a/disk.hdf"


@pytest.mark.parametrize("arguments, mountpoint, from_ps", CASES.values(), ids=CASES)
def test_owner_is_found_from_any_working_directory(record_dir, mount_process, tmp_path,
                                                   monkeypatch, arguments, mountpoint,
                                                   from_ps):
    pid = mount_process(*arguments)
    monkeypatch.chdir(tmp_path)
    assert pid not in plat._find_mount_owner_pids(Path(mountpoint))

    _record(record_dir, pid, mountpoint)

    assert pid in plat._find_mount_owner_pids(Path(mountpoint))


@pytest.mark.parametrize("arguments, mountpoint, from_ps", CASES.values(), ids=CASES)
def test_fuse_t_mount_is_identified_by_its_record(record_dir, mount_process, monkeypatch,
                                                  arguments, mountpoint, from_ps):
    pid = mount_process(*arguments)
    table = [f"fuse-t:/Work on {mountpoint}"]
    assert not plat.is_amifuse_mount_macos(PurePosixPath(mountpoint), table)

    _record(record_dir, pid, mountpoint)

    assert plat.is_amifuse_mount_macos(PurePosixPath(mountpoint), table)


def test_another_fuse_t_mount_is_not_taken_for_a_recorded_one(record_dir, mount_process):
    pid = mount_process("disk.hdf")
    _record(record_dir, pid, "/Volumes/Work")

    assert not plat.is_amifuse_mount_macos(
        PurePosixPath("/Volumes/Other"), ["fuse-t:/Other on /Volumes/Other"])
    assert plat.is_amifuse_mount_macos(
        PurePosixPath("/Volumes/Work"), ["fuse-t:/Work on /Volumes/Work"])


def test_a_record_counts_only_for_a_running_mount_process(record_dir, mount_process):
    live = mount_process("disk.hdf")
    _record(record_dir, live, "/Volumes/Live")
    # A killed mount's record, its pid gone or reused by another program.
    _record(record_dir, os.getpid(), "/Volumes/Dead")

    assert plat._find_mount_owner_pids(Path("/Volumes/Live")) == [live]
    assert plat._find_mount_owner_pids(Path("/Volumes/Dead")) == []


def test_a_damaged_record_is_ignored(record_dir, mount_process):
    pid = mount_process("disk.hdf", "--mountpoint", "/Volumes/Work")
    (record_dir / f"{pid}.json").write_text('{"pid": ')
    assert _discovered(pid)["mountpoint"] == "/Volumes/Work"

    (record_dir / f"{pid}.json").write_text(json.dumps({"pid": pid + 1, "mountpoint": "/x"}))
    assert _discovered(pid)["mountpoint"] == "/Volumes/Work"


def test_records_in_a_directory_others_can_write_are_ignored(record_dir, mount_process):
    pid = mount_process("disk.hdf", "--mountpoint", "/Volumes/Work")
    _record(record_dir, pid, "/Volumes/Other")
    assert _discovered(pid)["mountpoint"] == "/Volumes/Other"

    record_dir.chmod(0o777)

    assert _discovered(pid)["mountpoint"] == "/Volumes/Work"


def test_a_record_written_can_be_read_and_removed(record_dir):
    record = {"mountpoint": "/Volumes/Work", "image": "/a b/caf\xe9.hdf", "partition": "DH0"}

    plat.write_mount_record(record)

    saved = plat._read_mount_records()[os.getpid()]
    assert abs(saved.pop("created_at") - time.time()) < 5
    assert saved == dict(record, pid=os.getpid())
    plat.remove_mount_record()
    assert plat._read_mount_records() == {}
    plat.remove_mount_record()  # nothing left to remove


def test_writing_a_record_creates_a_private_directory(monkeypatch, tmp_path):
    directory = tmp_path / "amifuse-1"
    monkeypatch.setattr(plat, "_mount_record_dir", lambda: directory)

    plat.write_mount_record({"mountpoint": "/Volumes/Work"})

    assert os.stat(directory).st_mode & 0o777 == 0o700
    assert list(plat._read_mount_records()) == [os.getpid()]


def test_writing_a_record_drops_those_of_dead_processes(record_dir):
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    _record(record_dir, process.pid, "/Volumes/Dead")
    _record(record_dir, os.getppid(), "/Volumes/Alive")

    plat.write_mount_record({"mountpoint": "/Volumes/Work"})

    assert sorted(plat._read_mount_records()) == sorted([os.getppid(), os.getpid()])


def test_a_record_that_cannot_be_written_leaves_the_mount_alone(monkeypatch, tmp_path):
    occupied = tmp_path / "file"
    occupied.write_text("")
    monkeypatch.setattr(plat, "_mount_record_dir", lambda: occupied / "records")

    plat.write_mount_record({"mountpoint": "/Volumes/Work"})
    plat.remove_mount_record()

    assert plat._read_mount_records() == {}


def test_record_directory_is_per_user_under_the_runtime_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert plat._mount_record_dir() == tmp_path / f"amifuse-{os.getuid()}"

    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setattr(plat.tempfile, "gettempdir", lambda: "/var/folders/x/T")
    assert plat._mount_record_dir() == Path(f"/var/folders/x/T/amifuse-{os.getuid()}")


@pytest.fixture
def mount(monkeypatch, record_dir):
    """Run mount_fuse with FUSE replaced by `during`, called once it is up."""
    rdb = MagicMock()
    rdb.detect_adf.return_value = SimpleNamespace(dos_type=0x444F5301, is_hd=False)
    monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", rdb)
    bridge = MagicMock()
    bridge.volume_name.return_value = "Work"
    monkeypatch.setattr(fuse_fs, "HandlerBridge", MagicMock(return_value=bridge))
    monkeypatch.setattr(fuse_fs, "_handler_has_crashed", lambda bridge: False)
    monkeypatch.setattr(fuse_fs, "_fork_daemon_parent", lambda: None)
    monkeypatch.setattr(fuse_fs, "resolve_driver", lambda *a, **kw: fuse_fs.ResolvedDriver(
        Path("driver"), "driver", False))
    monkeypatch.setattr(plat, "should_auto_create_mountpoint", lambda mountpoint: True)
    monkeypatch.setattr(plat, "validate_mountpoint", lambda mountpoint: None)
    monkeypatch.setattr(plat, "get_mount_options", lambda **kw: {})

    def run(image, mountpoint, during):
        def fake_fuse(operations, fuse_mountpoint, **kwargs):
            operations.init("/")
            during(operations)

        monkeypatch.setattr(fuse_fs, "_require_fuse", lambda: fake_fuse)
        fuse_fs.mount_fuse(image, None, mountpoint, block_size=None)

    return run


def test_a_mount_records_itself_while_up_with_absolute_paths(mount, record_dir, tmp_path,
                                                             monkeypatch):
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    seen = {}

    mount(Path("disk.adf"), Path("mnt"),
          lambda operations: seen.update(plat._read_mount_records()))

    assert abs(seen[os.getpid()].pop("created_at") - time.time()) < 5
    assert seen == {os.getpid(): {
        "pid": os.getpid(),
        "mountpoint": os.path.join(cwd, "mnt"),
        "image": os.path.join(cwd, "disk.adf"),
        "partition": None,
    }}
    assert plat._read_mount_records() == {}


def test_a_mount_that_ends_with_an_error_leaves_no_record(mount, record_dir):
    def during(operations):
        assert plat._read_mount_records()
        raise RuntimeError("the FUSE loop failed")

    with pytest.raises(RuntimeError, match="FUSE loop"):
        mount(Path("/a/disk.adf"), Path("/a/mnt"), during)

    assert plat._read_mount_records() == {}


@pytest.mark.parametrize("created,expected", [
    (900, "/new"), (989, "/old"), (990, "/old"),
    (None, "/new"), ("invalid", "/new"), (float("nan"), "/new"),
])
def test_record_from_previous_pid_owner_is_ignored(monkeypatch, created, expected):
    monkeypatch.setattr(plat.time, "time", lambda: 1000)
    monkeypatch.setattr(plat, "_read_mount_records", lambda: {
        123: {"mountpoint": "/old", "created_at": created}})
    mounts = [{"pid": 123, "mountpoint": "/new", "uptime_seconds": 10}]

    plat._apply_mount_records(mounts)

    assert mounts[0]["mountpoint"] == expected


@pytest.mark.parametrize("zone", ["UTC0", "CET-1CEST,M3.5.0,M10.5.0/3", "JST-9", "PST8PDT,M3.2.0,M11.1.0"])
@pytest.mark.parametrize("created_delta", [3, -3600])
def test_local_start_time_validates_mount_record(monkeypatch, zone, created_delta):
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = zone
        time.tzset()
        now = 1794078000
        start = now - 120
        stamp = time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(start))
        monkeypatch.setattr(plat.time, "time", lambda: now)
        uptime = plat._parse_lstart_uptime(stamp)
        assert uptime == 120
        mounts = [{"pid": 42, "mountpoint": "mnt", "uptime_seconds": uptime}]
        monkeypatch.setattr(plat, "_read_mount_records", lambda: {
            42: {"created_at": start + created_delta, "mountpoint": "/Users/a/mnt"}})
        plat._apply_mount_records(mounts)
        assert mounts[0]["mountpoint"] == ("/Users/a/mnt" if created_delta > 0 else "mnt")
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()
