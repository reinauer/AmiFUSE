"""The unit tests keep %APPDATA%\\AmiFUSE below tmp_path.

The autouse ``appdata_sandbox`` fixture in conftest.py moves APPDATA and the
AmiFUSE paths that windows_shell and launcher work out from it at import.
Each test checks the path before it writes, so a missing sandbox fails the
test without touching the real folder.
"""

import os
from pathlib import Path


def test_appdata_points_into_tmp_path(tmp_path):
    """APPDATA, which tray and child processes read, is below tmp_path."""
    assert tmp_path in Path(os.environ["APPDATA"]).parents


def test_windows_shell_paths_point_into_tmp_path(tmp_path):
    """The AmiFUSE folder, icons and launch.vbs that register() writes are
    below tmp_path."""
    from amifuse import windows_shell

    for path in (windows_shell._AMIFUSE_DIR, windows_shell.ICON_DIR,
                 windows_shell._LAUNCH_VBS):
        assert tmp_path in path.parents, path


def test_launcher_log_dir_points_into_tmp_path(tmp_path):
    """launcher.log and the mount-<letter>.log files go below tmp_path."""
    from amifuse import launcher

    assert tmp_path in launcher._LOG_DIR.parents


def test_launch_vbs_write_and_remove_stay_in_tmp_path(tmp_path):
    """Writing and removing launch.vbs, as register() and unregister() do,
    acts on the sandboxed file."""
    from amifuse import windows_shell

    vbs = windows_shell._LAUNCH_VBS
    assert tmp_path in vbs.parents
    windows_shell._ensure_vbs_launcher()
    assert vbs.read_text(encoding="utf-8") == windows_shell._LAUNCH_VBS_CONTENT
    windows_shell._remove_icons()
    assert not vbs.exists()


def test_launcher_log_writes_into_tmp_path(tmp_path):
    """launcher._log appends to the sandboxed launcher.log."""
    from amifuse import launcher

    assert tmp_path in launcher._LOG_DIR.parents
    launcher._log("appdata sandbox probe")
    log = launcher._LOG_DIR / "launcher.log"
    assert log.read_text().endswith(" INFO appdata sandbox probe\n")
