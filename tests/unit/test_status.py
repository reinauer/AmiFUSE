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
# C. find_amifuse_mounts -- Windows (mocked wmic)
# ---------------------------------------------------------------------------


class TestFindAmifuseMountsWindows:
    """Test _find_amifuse_mounts_windows with mocked subprocess output."""

    @pytest.fixture(autouse=True)
    def _force_windows(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")

    def _mock_wmic(self, monkeypatch, stdout, returncode=0):
        result = MagicMock()
        result.stdout = stdout
        result.returncode = returncode
        monkeypatch.setattr(
            "amifuse.platform.subprocess.run",
            MagicMock(return_value=result),
        )

    def test_single_mount(self, monkeypatch):
        from amifuse.platform import _find_amifuse_mounts_windows

        monkeypatch.setattr("amifuse.platform.os.getpid", lambda: 99999)
        self._mock_wmic(monkeypatch,
            "CommandLine=python -m amifuse mount C:/images/test.hdf --mountpoint D:\r\n"
            "CreationDate=20260419103000.123456+000\r\n"
            "ParentProcessId=1\r\n"
            "ProcessId=12345\r\n"
            "\r\n")

        mounts = _find_amifuse_mounts_windows()
        assert len(mounts) == 1
        assert mounts[0]["pid"] == 12345
        assert mounts[0]["image"] == "C:/images/test.hdf"
        assert mounts[0]["mountpoint"] == "D:"
        assert mounts[0]["filesystem_type"] is None
        assert mounts[0]["parent_pid"] == 1

    def test_empty_process_list(self, monkeypatch):
        from amifuse.platform import _find_amifuse_mounts_windows

        self._mock_wmic(monkeypatch, "")
        mounts = _find_amifuse_mounts_windows()
        assert mounts == []

    def test_non_amifuse_filtered(self, monkeypatch):
        from amifuse.platform import _find_amifuse_mounts_windows

        monkeypatch.setattr("amifuse.platform.os.getpid", lambda: 99999)
        self._mock_wmic(monkeypatch,
            "CommandLine=python some_script.py\r\n"
            "CreationDate=20260419100000.000000+000\r\n"
            "ParentProcessId=1\r\n"
            "ProcessId=111\r\n"
            "\r\n"
            "CommandLine=python -m amifuse mount C:/img.hdf --mountpoint E:\r\n"
            "CreationDate=20260419100000.000000+000\r\n"
            "ParentProcessId=1\r\n"
            "ProcessId=222\r\n"
            "\r\n")

        mounts = _find_amifuse_mounts_windows()
        assert len(mounts) == 1
        assert mounts[0]["pid"] == 222

    def test_wmic_failure_returns_empty(self, monkeypatch):
        from amifuse.platform import _find_amifuse_mounts_windows

        self._mock_wmic(monkeypatch, "", returncode=1)
        mounts = _find_amifuse_mounts_windows()
        assert mounts == []

    def test_wmic_not_found_returns_empty(self, monkeypatch):
        from amifuse.platform import _find_amifuse_mounts_windows

        monkeypatch.setattr(
            "amifuse.platform.subprocess.run",
            MagicMock(side_effect=OSError("[WinError 2] The system cannot find the file specified")),
        )
        mounts = _find_amifuse_mounts_windows()
        assert mounts == []

    def test_multiple_mounts(self, monkeypatch):
        from amifuse.platform import _find_amifuse_mounts_windows

        monkeypatch.setattr("amifuse.platform.os.getpid", lambda: 99999)
        self._mock_wmic(monkeypatch,
            "CommandLine=python -m amifuse mount C:/a.hdf --mountpoint D:\r\n"
            "CreationDate=20260419100000.000000+000\r\n"
            "ParentProcessId=1\r\n"
            "ProcessId=100\r\n"
            "\r\n"
            "CommandLine=python -m amifuse mount C:/b.hdf --mountpoint E:\r\n"
            "CreationDate=20260419100000.000000+000\r\n"
            "ParentProcessId=1\r\n"
            "ProcessId=200\r\n"
            "\r\n")

        mounts = _find_amifuse_mounts_windows()
        assert len(mounts) == 2


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
        sentinel = [{"mountpoint": "D:", "pid": 2, "image": "b",
                     "uptime_seconds": None, "filesystem_type": None}]
        monkeypatch.setattr(plat, "_find_amifuse_mounts_windows", lambda: sentinel)
        assert plat.find_amifuse_mounts() == sentinel


# ---------------------------------------------------------------------------
# E. cmd_status
# ---------------------------------------------------------------------------


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

        mounts = [{"mountpoint": None, "pid": 456}]
        all_mounts = mounts[:]
        _enrich_mountpoints_macos(mounts, all_mounts)
        assert mounts[0]["mountpoint"] == "/Volumes/DH0"

    def test_enrich_macos_skips_claimed(self, monkeypatch):
        from amifuse.platform import _enrich_mountpoints_macos

        monkeypatch.setattr("os.path.isdir", lambda p: p == "/Volumes")
        monkeypatch.setattr("os.listdir", lambda p: ["DH0", "DH1"])
        monkeypatch.setattr("os.path.ismount", lambda p: True)

        null_mount = {"mountpoint": None, "pid": 456}
        claimed_mount = {"mountpoint": "/Volumes/DH0", "pid": 789}
        all_mounts = [claimed_mount, null_mount]
        _enrich_mountpoints_macos([null_mount], all_mounts)
        assert null_mount["mountpoint"] == "/Volumes/DH1"

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
