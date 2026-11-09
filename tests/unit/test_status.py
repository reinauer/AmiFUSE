"""Unit tests for mount discovery (platform.find_amifuse_mounts) and cmd_status.

All subprocess calls are mocked -- no real processes needed.
"""

import json
import os
import subprocess
import sys
import types
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# A. _parse_mount_tokens
# ---------------------------------------------------------------------------


class TestParseMountTokens:
    """Tests for platform._parse_mount_tokens helper."""

    def test_standard_invocation(self):
        from amifuse.platform import _parse_mount_tokens

        tokens = ["python", "-m", "amifuse", "mount", "/images/test.hdf",
                  "--mountpoint", "/Volumes/DH0"]
        image, mp = _parse_mount_tokens(tokens)
        assert image == "/images/test.hdf"
        assert mp == "/Volumes/DH0"

    def test_direct_invocation(self):
        from amifuse.platform import _parse_mount_tokens

        tokens = ["amifuse", "mount", "C:/images/test.hdf",
                  "--mountpoint", "D:"]
        image, mp = _parse_mount_tokens(tokens)
        assert image == "C:/images/test.hdf"
        assert mp == "D:"

    @pytest.mark.parametrize("args", [
        ["--mountpoint=/Users/me/mnt"],
        ["--mount", "/Users/me/mnt"],
        ["--m=/Users/me/mnt"],
    ])
    def test_mountpoint_forms_argparse_accepts(self, args):
        """`amifuse unmount` on FUSE-T ties a mount to its process by this."""
        from amifuse.platform import _parse_mount_tokens

        tokens = ["amifuse", "mount", "/images/test.hdf", *args, "--write"]
        image, mp = _parse_mount_tokens(tokens)
        assert image == "/images/test.hdf"
        assert mp == "/Users/me/mnt"

    @pytest.mark.parametrize("cmdline, expected", [
        # cmd.exe passes the quotes on as typed; the Windows tokenizer splits
        # the value at the space, so no mountpoint beats half of one.
        ('amifuse mount C:\\img.hdf --mountpoint="C:\\a b" --write', None),
        ('amifuse mount --mountpoint="C:\\a  b" C:\\img.hdf', None),
        # list2cmdline quotes the whole token, which stays one token.
        ('amifuse mount C:\\img.hdf "--mountpoint=C:\\a b"', "C:\\a b"),
        ('amifuse mount C:\\img.hdf --mountpoint="C:\\ab"', "C:\\ab"),
        ('amifuse mount C:\\img.hdf --mountpoint "C:\\a b"', "C:\\a b"),
        # A quote inside the value splits it too.
        ('amifuse mount C:\\img.hdf --mountpoint=C:\\Users\\"My Mounts"\\x',
         None),
        # Another option's split or quoted value leaves the mountpoint alone.
        ('amifuse mount --mountpoint E: --volname="Work Disk" C:\\img.hdf',
         "E:"),
        ('amifuse mount --mountpoint E: "--volname=Work Disk" C:\\img.hdf',
         "E:"),
    ])
    def test_windows_quoted_mountpoint(self, cmdline, expected):
        """Windows discovery tokenizes with shlex.split(posix=False)."""
        import shlex
        from amifuse.platform import _parse_mount_tokens

        image, mp = _parse_mount_tokens(shlex.split(cmdline, posix=False))
        assert image == "C:\\img.hdf"
        assert mp == expected

    @pytest.mark.parametrize("command, expected", [
        # `ps` shows a Unix process's arguments joined with spaces and no
        # quotes, so a mountpoint with a space arrives split.
        ("amifuse mount img.hdf --mountpoint=/Volumes/Work Disk", None),
        ("amifuse mount img.hdf --mountpoint /Volumes/Work Disk --write", None),
        ("amifuse mount --mountpoint /Volumes/Work Disk --driver D img.hdf",
         None),
        # Split values elsewhere leave the mountpoint alone.
        ("amifuse mount My Disk.hdf --mountpoint /Volumes/Work", "/Volumes/Work"),
        ("amifuse mount img.hdf --mountpoint /Volumes/Work --volname Work Disk",
         "/Volumes/Work"),
        ("amifuse mount --mountpoint /Volumes/Work img.hdf", "/Volumes/Work"),
        # A piece starting with "-" is no option.
        ("amifuse mount img.hdf --mountpoint=/Volumes/Games - WHD", None),
        ("amifuse mount img.hdf --mountpoint /Volumes/Games -WHD --write", None),
        ("amifuse mount --mount=/Volumes/A -B img.hdf", None),
        ("amifuse mount img.hdf --mountpoint=/Volumes/Games -- WHD", None),
        ("amifuse mount img.hdf --mountpoint /Volumes/Games -h WHD", None),
        ("amifuse mount img.hdf --mountpoint /Volumes/Games --help", None),
        # A real "--" before the image.
        ("amifuse mount --mountpoint /V/W -- img.hdf", "/V/W"),
        # A switch or "--" piece doesn't end the mountpoint's pieces: the
        # shortened readings are command lines argparse would refuse.
        ("amifuse mount img.hdf --mountpoint /Volumes/Games --", None),
        ("amifuse mount img.hdf --mountpoint /Volumes/Games --write WHD", None),
        ("amifuse mount img.hdf --mountpoint /Volumes/Games -- --write", None),
        ("amifuse mount img.hdf --mountpoint /Volumes/Games --write=1", None),
        # Real switches and a spaced --volname after it leave it alone.
        ("amifuse mount img.hdf --mountpoint /V/W --daemon --volname My Disk",
         "/V/W"),
        # Abbreviated and "=" value options take their value with them.
        ("amifuse mount --vol Work --part DH1 --mountpoint /V/W img.hdf", "/V/W"),
        ("amifuse mount --volname=Work --mountpoint /V/W img.hdf", "/V/W"),
    ])
    def test_unix_ps_command_with_split_mountpoint(self, command, expected):
        """Unix discovery splits `ps` output with posix shlex."""
        import shlex
        from amifuse.platform import _parse_mount_tokens

        _image, mp = _parse_mount_tokens(shlex.split(command))
        assert mp == expected

    def test_option_lists_match_the_mount_parser(self, capsys, fuse_mock):
        """Discovery tells options from split values by these lists."""
        import re
        from amifuse import fuse_fs, platform as plat

        with pytest.raises(SystemExit):
            fuse_fs.main(["mount", "--help"])
        out = capsys.readouterr().out
        options = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", out))
        assert options == set(plat._MOUNT_VALUE_OPTIONS
                              + plat._MOUNT_SWITCHES)
        # The usage line shows each option that takes a value with its
        # metavar: "[--driver DRIVER]".
        usage = " ".join(out.split("\n\n")[0].split())
        assert set(re.findall(r"\[(--[a-z-]+) [A-Z_]+\]", usage)) == set(
            plat._MOUNT_VALUE_OPTIONS)

    def test_no_mount_subcommand(self):
        from amifuse.platform import _parse_mount_tokens

        tokens = ["amifuse", "doctor", "--json"]
        image, mp = _parse_mount_tokens(tokens)
        assert image is None
        assert mp is None

    def test_no_mountpoint_flag(self):
        from amifuse.platform import _parse_mount_tokens

        tokens = ["amifuse", "mount", "/images/test.hdf"]
        image, mp = _parse_mount_tokens(tokens)
        assert image == "/images/test.hdf"
        assert mp is None  # auto-assigned mountpoints won't appear in CLI args

    def test_image_path_with_spaces(self):
        from amifuse.platform import _parse_mount_tokens

        tokens = ["amifuse", "mount", "/my images/test file.hdf",
                  "--mountpoint", "/Volumes/DH0"]
        image, mp = _parse_mount_tokens(tokens)
        assert image == "/my images/test file.hdf"
        assert mp == "/Volumes/DH0"

    def test_flags_before_image(self):
        from amifuse.platform import _parse_mount_tokens

        tokens = ["amifuse", "mount", "--driver", "/path/to/pfs3",
                  "test.hdf", "--mountpoint", "/mnt/amiga"]
        image, mp = _parse_mount_tokens(tokens)
        assert image == "test.hdf"
        assert mp == "/mnt/amiga"

    def test_boolean_flags_skipped(self):
        from amifuse.platform import _parse_mount_tokens

        tokens = ["amifuse", "mount", "test.hdf", "--debug",
                  "--mountpoint", "/mnt/amiga", "--interactive"]
        image, mp = _parse_mount_tokens(tokens)
        assert image == "test.hdf"
        assert mp == "/mnt/amiga"


# ---------------------------------------------------------------------------
# B. find_amifuse_mounts -- Unix (mocked ps)
# ---------------------------------------------------------------------------


class TestFindAmifuseMountsUnix:
    """Test _find_amifuse_mounts_unix with mocked subprocess output."""

    @pytest.fixture(autouse=True)
    def _force_unix(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")

    def _mock_ps(self, monkeypatch, stdout, returncode=0):
        result = MagicMock()
        result.stdout = stdout
        result.returncode = returncode
        monkeypatch.setattr(
            "amifuse.platform.subprocess.run",
            MagicMock(return_value=result),
        )

    def test_single_mount(self, monkeypatch):
        from amifuse.platform import _find_amifuse_mounts_unix

        # Simulate current PID != 12345
        monkeypatch.setattr("amifuse.platform.os.getpid", lambda: 99999)
        self._mock_ps(monkeypatch,
            "12345  1  3600 python -m amifuse mount /images/test.hdf --mountpoint /mnt/amiga\n")

        mounts = _find_amifuse_mounts_unix()
        assert len(mounts) == 1
        assert mounts[0]["pid"] == 12345
        assert mounts[0]["image"] == "/images/test.hdf"
        assert mounts[0]["mountpoint"] == "/mnt/amiga"
        assert mounts[0]["uptime_seconds"] == 3600
        assert mounts[0]["filesystem_type"] is None
        assert mounts[0]["parent_pid"] == 1

    def test_empty_process_list(self, monkeypatch):
        from amifuse.platform import _find_amifuse_mounts_unix

        self._mock_ps(monkeypatch, "")
        mounts = _find_amifuse_mounts_unix()
        assert mounts == []

    def test_non_amifuse_processes_filtered(self, monkeypatch):
        from amifuse.platform import _find_amifuse_mounts_unix

        monkeypatch.setattr("amifuse.platform.os.getpid", lambda: 99999)
        self._mock_ps(monkeypatch,
            "111  1  100 python some_other_script.py\n"
            "222  1  200 python -m amifuse doctor --json\n"
            "333  1  300 python -m amifuse mount /img.hdf --mountpoint /mnt/x\n")

        mounts = _find_amifuse_mounts_unix()
        assert len(mounts) == 1
        assert mounts[0]["pid"] == 333

    def test_current_pid_excluded(self, monkeypatch):
        from amifuse.platform import _find_amifuse_mounts_unix

        monkeypatch.setattr("amifuse.platform.os.getpid", lambda: 12345)
        self._mock_ps(monkeypatch,
            "12345  1  100 python -m amifuse mount /img.hdf --mountpoint /mnt/x\n")

        mounts = _find_amifuse_mounts_unix()
        assert mounts == []

    def test_multiple_mounts(self, monkeypatch):
        from amifuse.platform import _find_amifuse_mounts_unix

        monkeypatch.setattr("amifuse.platform.os.getpid", lambda: 99999)
        self._mock_ps(monkeypatch,
            "100  1  60 python -m amifuse mount /a.hdf --mountpoint /mnt/a\n"
            "200  1  120 python -m amifuse mount /b.hdf --mountpoint /mnt/b\n")

        mounts = _find_amifuse_mounts_unix()
        assert len(mounts) == 2
        assert {m["pid"] for m in mounts} == {100, 200}

    def test_malformed_line_skipped(self, monkeypatch):
        from amifuse.platform import _find_amifuse_mounts_unix

        monkeypatch.setattr("amifuse.platform.os.getpid", lambda: 99999)
        self._mock_ps(monkeypatch,
            "not_a_pid amifuse mount something\n"
            "100  1  60 python -m amifuse mount /a.hdf --mountpoint /mnt/a\n")

        mounts = _find_amifuse_mounts_unix()
        assert len(mounts) == 1

    def test_ps_failure_returns_empty(self, monkeypatch):
        from amifuse.platform import _find_amifuse_mounts_unix

        self._mock_ps(monkeypatch, "", returncode=1)
        mounts = _find_amifuse_mounts_unix()
        assert mounts == []

    def test_ps_not_found_returns_empty(self, monkeypatch):
        from amifuse.platform import _find_amifuse_mounts_unix

        monkeypatch.setattr(
            "amifuse.platform.subprocess.run",
            MagicMock(side_effect=OSError("No such file")),
        )
        mounts = _find_amifuse_mounts_unix()
        assert mounts == []


# ---------------------------------------------------------------------------
# C. find_amifuse_mounts -- Windows (mocked CIM)


class TestFindAmifuseMountsWindows:
    @pytest.mark.parametrize("entries, expected", [
        ([], []),
        ([{"ProcessId": 12345, "ParentProcessId": 1,
           "CommandLine": "python -m amifuse mount C:/José-Ω-ā.hdf --mountpoint D:",
           "CreationDate": "/Date(1700000000000)/"}], [12345]),
        ([{"ProcessId": 111, "CommandLine": "python other.py"},
          {"ProcessId": 222, "CommandLine": "python -m amifuse mount C:/a.hdf"},
          {"ProcessId": 333, "CommandLine": "python -m amifuse mount C:/b.hdf"}], [222, 333]),
    ])
    def test_cim_discovery(self, monkeypatch, entries, expected):
        from amifuse import platform as plat
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(plat.os, "getpid", lambda: 99999)
        run = MagicMock(return_value=MagicMock(returncode=0,
            stdout=json.dumps(entries, ensure_ascii=False).encode("utf-8-sig")))
        monkeypatch.setattr(plat.subprocess, "run", run)
        mounts = plat._find_amifuse_mounts_windows()
        assert [m["pid"] for m in mounts] == expected
        command = run.call_args.args[0]
        assert command[0] == "powershell"
        assert "OutputEncoding" in command[-1] and "UTF8Encoding" in command[-1]
        if expected == [12345]:
            assert mounts[0]["image"] == "C:/José-Ω-ā.hdf"
            assert mounts[0]["mountpoint"] == "D:"
            assert mounts[0]["parent_pid"] == 1
            assert mounts[0]["uptime_seconds"] is not None

    @pytest.mark.parametrize("stdout", [b"", b"\r\n", "\ufeff".encode("utf-8")])
    def test_no_output_means_no_mounts(self, monkeypatch, stdout):
        # PowerShell prints nothing when no process matches the filter.
        from amifuse import platform as plat
        monkeypatch.setattr(plat.subprocess, "run", MagicMock(
            return_value=MagicMock(returncode=0, stdout=stdout)))
        assert plat._find_amifuse_mounts_windows() == []

    def test_missing_powershell_returns_empty(self, monkeypatch):
        from amifuse import platform as plat
        monkeypatch.setattr(plat.subprocess, "run",
                            MagicMock(side_effect=FileNotFoundError("powershell")))
        assert plat._find_amifuse_mounts_windows() == []

    @pytest.mark.parametrize("run, message", [
        (MagicMock(side_effect=subprocess.TimeoutExpired("powershell", 15)), "timed out"),
        (MagicMock(return_value=MagicMock(returncode=1, stdout=b"")), "exited with code 1"),
        (MagicMock(return_value=MagicMock(returncode=0, stdout=b"Get-CimInstance : ...")),
         "no valid JSON"),
    ], ids=["timeout", "exit-code", "not-json"])
    def test_query_failure_is_not_taken_for_no_mounts(self, monkeypatch, run, message):
        # "No mounts" would make status say so, unmount find no owner and
        # the tray clear its menu and exit.
        from amifuse import platform as plat
        monkeypatch.setattr(plat.subprocess, "run", run)
        with pytest.raises(OSError, match=message):
            plat._find_amifuse_mounts_windows()


# ---------------------------------------------------------------------------
# D. find_amifuse_mounts (dispatch)
# ---------------------------------------------------------------------------


class TestFindAmifuseMountsDispatch:
    """Test the top-level find_amifuse_mounts() dispatches correctly."""

    def test_dispatches_to_unix(self, monkeypatch):
        from amifuse import platform as plat

        monkeypatch.setattr("sys.platform", "linux")
        sentinel = [{"mountpoint": "/mnt/x", "pid": 1, "image": "a",
                     "uptime_seconds": None, "filesystem_type": None}]
        monkeypatch.setattr(plat, "_find_amifuse_mounts_unix", lambda: sentinel)
        assert plat.find_amifuse_mounts() == sentinel

    def test_dispatches_to_windows(self, monkeypatch):
        from amifuse import platform as plat

        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr("amifuse.windows_unmount.is_mount_ready",
                            lambda pid: pid == 2)
        sentinel = [{"mountpoint": "D:", "pid": 2, "image": "b",
                     "uptime_seconds": None, "filesystem_type": None},
                    {"mountpoint": "E:", "pid": 3, "image": "c",
                     "uptime_seconds": 0, "filesystem_type": None}]
        monkeypatch.setattr(plat, "_find_amifuse_mounts_windows", lambda: sentinel)
        mounts = plat.find_amifuse_mounts()
        # Windows marks a process that has not reached FUSE init (#428).
        assert [(m["pid"], m["starting"]) for m in mounts] == [(2, False), (3, True)]

    def test_unreadable_mount_state_is_not_taken_for_starting(self, monkeypatch):
        # A process an Administrator started may not let this one open its
        # events: the unmount request then reports why, as before.
        from amifuse import platform as plat

        def denied(pid):
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr("amifuse.windows_unmount.is_mount_ready", denied)
        assert plat.mount_is_starting(4) is False


# ---------------------------------------------------------------------------
# E. cmd_status
# ---------------------------------------------------------------------------


# requests fuse_mock so importing amifuse.fuse_fs here cannot pin FUSE=None for later tests
@pytest.mark.usefixtures("fuse_mock")
class TestCmdStatus:
    """Test the cmd_status handler in fuse_fs."""

    def _make_args(self, json_flag=False):
        args = MagicMock()
        args.json = json_flag
        return args

    def test_json_no_mounts(self, monkeypatch, capsys):
        from amifuse.fuse_fs import cmd_status

        monkeypatch.setattr(
            "amifuse.platform.find_amifuse_mounts", lambda: [])
        cmd_status(self._make_args(json_flag=True))
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["command"] == "status"
        assert out["mounts"] == []

    def test_json_with_mounts(self, monkeypatch, capsys):
        from amifuse.fuse_fs import cmd_status

        mounts = [{
            "mountpoint": "D:",
            "image": "C:/images/test.hdf",
            "pid": 12345,
            "uptime_seconds": 120,
            "filesystem_type": None,
        }]
        monkeypatch.setattr(
            "amifuse.platform.find_amifuse_mounts", lambda: mounts)
        cmd_status(self._make_args(json_flag=True))
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert len(out["mounts"]) == 1
        m = out["mounts"][0]
        assert m["mountpoint"] == "D:"
        assert m["image"] == "C:/images/test.hdf"
        assert m["pid"] == 12345
        assert m["uptime_seconds"] == 120
        assert m["filesystem_type"] is None

    def test_json_error(self, monkeypatch, capsys):
        from amifuse.fuse_fs import cmd_status

        def _raise():
            raise OSError("wmic not found")
        monkeypatch.setattr(
            "amifuse.platform.find_amifuse_mounts", _raise)
        with pytest.raises(SystemExit):
            cmd_status(self._make_args(json_flag=True))
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert out["command"] == "status"
        assert "wmic not found" in out["error"]
        assert out["mounts"] == []

    def test_text_no_mounts(self, monkeypatch, capsys):
        from amifuse.fuse_fs import cmd_status

        monkeypatch.setattr(
            "amifuse.platform.find_amifuse_mounts", lambda: [])
        cmd_status(self._make_args(json_flag=False))
        out = capsys.readouterr().out
        assert "No active AmiFUSE mounts." in out

    def test_text_with_mounts(self, monkeypatch, capsys):
        from amifuse.fuse_fs import cmd_status

        mounts = [{
            "mountpoint": "/mnt/amiga",
            "image": "/images/test.hdf",
            "pid": 999,
            "uptime_seconds": 3661,
            "filesystem_type": None,
        }]
        monkeypatch.setattr(
            "amifuse.platform.find_amifuse_mounts", lambda: mounts)
        cmd_status(self._make_args(json_flag=False))
        out = capsys.readouterr().out
        assert "999" in out
        assert "/mnt/amiga" in out
        assert "/images/test.hdf" in out

    def test_text_marks_a_starting_mount(self, monkeypatch, capsys):
        from amifuse.fuse_fs import cmd_status

        mounts = [{"mountpoint": "R:", "image": "C:/a.hdf", "pid": 7156,
                   "uptime_seconds": 0, "filesystem_type": None, "starting": True}]
        monkeypatch.setattr("amifuse.platform.find_amifuse_mounts", lambda: mounts)
        cmd_status(self._make_args(json_flag=False))
        row = capsys.readouterr().out.splitlines()[-1]
        assert row.startswith("7156") and row.endswith("0h 0m 0s (starting)")

    def test_json_multiple_mounts(self, monkeypatch, capsys):
        from amifuse.fuse_fs import cmd_status

        mounts = [
            {"mountpoint": "/mnt/a", "image": "a.hdf", "pid": 1,
             "uptime_seconds": 10, "filesystem_type": None},
            {"mountpoint": "/mnt/b", "image": "b.hdf", "pid": 2,
             "uptime_seconds": None, "filesystem_type": None},
        ]
        monkeypatch.setattr(
            "amifuse.platform.find_amifuse_mounts", lambda: mounts)
        cmd_status(self._make_args(json_flag=True))
        out = json.loads(capsys.readouterr().out)
        assert len(out["mounts"]) == 2


# ---------------------------------------------------------------------------
# F. _find_mount_owner_pids refactor
# ---------------------------------------------------------------------------


class TestFindMountOwnerPidsRefactored:
    """Verify the refactored _find_mount_owner_pids wraps find_amifuse_mounts.

    _find_mount_owner_pids moved to platform.py in Phase 8.
    """

    @pytest.fixture(autouse=True)
    def mounted_processes(self, monkeypatch):
        monkeypatch.setattr("amifuse.windows_unmount.is_mount_ready", lambda pid: True)

    def test_filters_by_mountpoint(self, monkeypatch):
        from amifuse.platform import _find_mount_owner_pids

        mounts = [
            {"mountpoint": "/mnt/a", "image": "a.hdf", "pid": 100,
             "uptime_seconds": None, "filesystem_type": None},
            {"mountpoint": "/mnt/b", "image": "b.hdf", "pid": 200,
             "uptime_seconds": None, "filesystem_type": None},
        ]
        monkeypatch.setattr(
            "amifuse.platform.find_amifuse_mounts", lambda: mounts)
        pids = _find_mount_owner_pids(Path("/mnt/a"))
        assert pids == [100]

    def test_returns_empty_on_no_match(self, monkeypatch):
        from amifuse.platform import _find_mount_owner_pids

        mounts = [
            {"mountpoint": "/mnt/a", "image": "a.hdf", "pid": 100,
             "uptime_seconds": None, "filesystem_type": None},
        ]
        monkeypatch.setattr(
            "amifuse.platform.find_amifuse_mounts", lambda: mounts)
        pids = _find_mount_owner_pids(Path("/mnt/z"))
        assert pids == []

    def test_returns_empty_on_oserror(self, monkeypatch):
        from amifuse.platform import _find_mount_owner_pids

        def _raise():
            raise OSError("ps not found")
        monkeypatch.setattr(
            "amifuse.platform.find_amifuse_mounts", _raise)
        pids = _find_mount_owner_pids(Path("/mnt/a"))
        assert pids == []


# ---------------------------------------------------------------------------
# G. _truncate_left
# ---------------------------------------------------------------------------


# requests fuse_mock so importing amifuse.fuse_fs here cannot pin FUSE=None for later tests
@pytest.mark.usefixtures("fuse_mock")
class TestTruncateLeft:
    """Tests for _truncate_left helper."""

    def test_short_string_unchanged(self):
        from amifuse.fuse_fs import _truncate_left

        assert _truncate_left("short.hdf", 40) == "short.hdf"

    def test_exact_length_unchanged(self):
        from amifuse.fuse_fs import _truncate_left

        s = "x" * 40
        assert _truncate_left(s, 40) == s

    def test_long_string_truncated_from_left(self):
        from amifuse.fuse_fs import _truncate_left

        s = "C:\\GitHub\\AmiFUSE-testing\\fixtures\\readonly\\pfs.hdf"
        result = _truncate_left(s, 40)
        assert len(result) == 40
        assert result.startswith("..")
        assert result.endswith("pfs.hdf")

    def test_preserves_filename(self):
        from amifuse.fuse_fs import _truncate_left

        s = "/very/long/path/to/some/deeply/nested/file.hdf"
        result = _truncate_left(s, 20)
        assert result.endswith("file.hdf")
        assert len(result) == 20

    def test_empty_string(self):
        from amifuse.fuse_fs import _truncate_left

        assert _truncate_left("", 40) == ""


# ---------------------------------------------------------------------------
# H. Mountpoint enrichment
# ---------------------------------------------------------------------------


# requests fuse_mock so importing amifuse.fuse_fs here cannot pin FUSE=None for later tests
@pytest.mark.usefixtures("fuse_mock")
class TestEnrichNullMountpoints:
    """Tests for _enrich_null_mountpoints and platform-specific helpers."""

    def test_no_null_mountpoints_is_noop(self):
        from amifuse.platform import _enrich_null_mountpoints

        mounts = [{"mountpoint": "/mnt/a", "pid": 1}]
        _enrich_null_mountpoints(mounts)
        assert mounts[0]["mountpoint"] == "/mnt/a"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only ctypes.windll")
    def test_enrich_windows_single_mount(self, monkeypatch):
        from amifuse.platform import _enrich_mountpoints_windows

        # Mock ctypes.windll.kernel32.GetVolumeInformationW
        mock_kernel32 = MagicMock()
        call_count = [0]
        drive_info = {
            "D:\\": ("PFS3AIO Volume", "FUSE-AmiFUSE"),
            "E:\\": ("SomeOther", "NTFS"),
        }

        def fake_get_vol_info(drive, vol_buf, vol_sz, *rest):
            info = drive_info.get(drive)
            if info is None:
                return 0
            import ctypes
            # Write to the buffers
            vol_name, fs_name = info
            for i, c in enumerate(vol_name):
                vol_buf[i] = c
            vol_buf[len(vol_name)] = '\0'
            # fs_name_buf is rest[-2]
            fs_buf = rest[-2]
            for i, c in enumerate(fs_name):
                fs_buf[i] = c
            fs_buf[len(fs_name)] = '\0'
            return 1

        import ctypes
        monkeypatch.setattr(
            "ctypes.windll.kernel32.GetVolumeInformationW",
            fake_get_vol_info,
        )

        mounts = [{"mountpoint": None, "pid": 123}]
        _enrich_mountpoints_windows(mounts, mounts)
        assert mounts[0]["mountpoint"] == "D:"

    def test_enrich_macos(self, monkeypatch):
        from amifuse.platform import _enrich_mountpoints_macos

        monkeypatch.setattr("os.path.isdir", lambda p: p == "/Volumes")
        monkeypatch.setattr("os.listdir", lambda p: ["DH0", "Macintosh HD"])
        monkeypatch.setattr("os.path.ismount", lambda p: True)
        monkeypatch.setattr(
            "amifuse.platform._macos_mount_table",
            lambda **kwargs: ["/dev/disk3s1s1 on /",
                              "amifuse:DH0 on /Volumes/DH0"],
        )

        mounts = [{"mountpoint": None, "pid": 456}]
        all_mounts = mounts[:]
        _enrich_mountpoints_macos(mounts, all_mounts)
        assert mounts[0]["mountpoint"] == "/Volumes/DH0"

    def test_enrich_macos_skips_claimed(self, monkeypatch):
        from amifuse.platform import _enrich_mountpoints_macos

        monkeypatch.setattr("os.path.isdir", lambda p: p == "/Volumes")
        monkeypatch.setattr("os.listdir", lambda p: ["DH0", "DH1"])
        monkeypatch.setattr("os.path.ismount", lambda p: True)
        monkeypatch.setattr(
            "amifuse.platform._macos_mount_table",
            lambda **kwargs: ["amifuse:DH0 on /Volumes/DH0",
                              "amifuse:DH1 on /Volumes/DH1"],
        )

        null_mount = {"mountpoint": None, "pid": 456}
        claimed_mount = {"mountpoint": "/Volumes/DH0", "pid": 789}
        all_mounts = [claimed_mount, null_mount]
        _enrich_mountpoints_macos([null_mount], all_mounts)
        assert null_mount["mountpoint"] == "/Volumes/DH1"

    def test_enrich_macos_skips_volume_that_is_not_amifuse(self, monkeypatch):
        """#296: a USB stick that sorts first is not paired with the mount."""
        from amifuse.platform import _enrich_mountpoints_macos

        monkeypatch.setattr("os.path.isdir", lambda p: p == "/Volumes")
        monkeypatch.setattr(
            "os.listdir", lambda p: ["Macintosh HD", "Work", "Backup"])
        monkeypatch.setattr("os.path.ismount", lambda p: True)
        monkeypatch.setattr(
            "amifuse.platform._macos_mount_table",
            lambda **kwargs: ["/dev/disk3s1s1 on /",
                              "/dev/disk4s1 on /Volumes/Backup",
                              "amifuse:Work on /Volumes/Work"],
        )

        mounts = [{"mountpoint": None, "image": "/Users/me/work.hdf",
                   "pid": 4242}]
        _enrich_mountpoints_macos(mounts, mounts)
        assert mounts[0]["mountpoint"] == "/Volumes/Work"

    def test_enrich_macos_leaves_mount_unpaired_without_amifuse_volume(
            self, monkeypatch):
        """With only the user's own disks in /Volumes, nothing is paired."""
        from amifuse.platform import _enrich_mountpoints_macos

        monkeypatch.setattr("os.path.isdir", lambda p: p == "/Volumes")
        monkeypatch.setattr("os.listdir", lambda p: ["Macintosh HD", "Backup"])
        monkeypatch.setattr("os.path.ismount", lambda p: True)
        monkeypatch.setattr(
            "amifuse.platform._macos_mount_table",
            lambda **kwargs: ["/dev/disk3s1s1 on /",
                              "/dev/disk4s1 on /Volumes/Backup"],
        )

        mounts = [{"mountpoint": None, "pid": 4242}]
        _enrich_mountpoints_macos(mounts, mounts)
        assert mounts[0]["mountpoint"] is None

    def test_enrich_macos_skips_dead_amifuse_mount(self, monkeypatch):
        """A crashed mount's /Volumes/Alpha keeps its table entry; the live
        mount is paired with its own /Volumes/Work, not with Alpha."""
        from amifuse.platform import _enrich_mountpoints_macos

        monkeypatch.setattr("os.path.isdir", lambda p: p == "/Volumes")
        monkeypatch.setattr("os.listdir", lambda p: ["Alpha", "Work"])
        # ismount() is False on a dead FUSE mount: lstat fails.
        monkeypatch.setattr("os.path.ismount", lambda p: p == "/Volumes/Work")
        monkeypatch.setattr(
            "amifuse.platform._macos_mount_table",
            lambda **kwargs: ["amifuse:Alpha on /Volumes/Alpha",
                              "amifuse:Work on /Volumes/Work"],
        )

        mounts = [{"mountpoint": None, "pid": 4242}]
        _enrich_mountpoints_macos(mounts, mounts)
        assert mounts[0]["mountpoint"] == "/Volumes/Work"

    def test_text_output_uses_truncation(self, monkeypatch, capsys):
        from amifuse.fuse_fs import cmd_status

        long_path = "C:\\GitHub\\AmiFUSE-testing\\fixtures\\readonly\\pfs.hdf"
        mounts = [{
            "mountpoint": "D:",
            "image": long_path,
            "pid": 999,
            "uptime_seconds": 60,
            "filesystem_type": None,
        }]
        monkeypatch.setattr(
            "amifuse.platform.find_amifuse_mounts", lambda: mounts)
        args = MagicMock()
        args.json = False
        cmd_status(args)
        out = capsys.readouterr().out
        # Should contain truncation marker, not the full path
        assert ".." in out
        assert "pfs.hdf" in out
