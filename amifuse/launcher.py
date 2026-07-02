"""Console-free launcher for AmiFUSE context menu actions.

This launcher is invoked by Explorer shell verbs. It MUST exit as fast as
possible -- any delay blocks the Explorer UI thread. All file I/O uses
open/write/close immediately; process exit uses os._exit() to skip Python
shutdown overhead.
"""

import argparse
import ctypes
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import amifuse.platform as platform

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

_DETACHED_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

# Seconds to wait for a mount to appear before surfacing an error dialog.
# Shared by _do_open and _do_mount so both poll loops stay in sync.
MOUNT_POLL_TIMEOUT = 15.0

_LOG_DIR = Path(os.environ.get("APPDATA", "")) / "AmiFUSE"


def _log(msg: str) -> None:
    """Append a single log line, opening and closing the file immediately."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        with open(str(_LOG_DIR / "launcher.log"), "a") as f:
            f.write(f"{ts} INFO {msg}\n")
    except OSError:
        pass


def _spawn_detached(cmd: list[str], **kwargs) -> None:
    """Spawn a fully detached process. Tries CREATE_BREAKAWAY_FROM_JOB first
    to escape Explorer's job object; falls back without it if the job
    doesn't allow breakaway."""
    flags = _DETACHED_FLAGS | CREATE_BREAKAWAY_FROM_JOB
    try:
        subprocess.Popen(cmd, creationflags=flags, **kwargs)
    except OSError:
        # Job doesn't allow breakaway -- retry without it
        subprocess.Popen(cmd, creationflags=_DETACHED_FLAGS, **kwargs)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="AmiFUSE launcher")
    sub = parser.add_subparsers(dest="command", required=True)

    mount_p = sub.add_parser("mount", help="Mount a disk image")
    mount_p.add_argument("image")
    mount_p.add_argument("--write", action="store_true")

    open_p = sub.add_parser("open", help="Mount and open in Explorer")
    open_p.add_argument("image")

    inspect_p = sub.add_parser("inspect", help="Open inspect in a new console")
    inspect_p.add_argument("image")

    args = parser.parse_args(argv)

    if args.command == "mount":
        _do_mount(args)
    elif args.command == "open":
        _do_open(args)
    elif args.command == "inspect":
        _do_inspect(args)

    # Force-exit immediately. Python's normal shutdown (atexit handlers,
    # logging.shutdown, GC, module cleanup) is unnecessary for a launcher
    # and can delay process exit enough to hang Explorer.
    os._exit(0)


def _select_drive_letter() -> Optional[str]:
    """Return the first unallocated drive letter as an ``"X:"`` string, or None.

    Selection uses ``platform._windows_allocated_drive_letters()`` (the Win32
    GetLogicalDrives bitmask) rather than ``os.path.exists`` so an
    assigned-but-empty removable slot (e.g. an empty card-reader D:) is
    correctly treated as taken -- the launcher and the core mount path share
    one correct implementation. The helper returns bare uppercase letters, so
    compare against uppercase candidates.
    """
    allocated = platform._windows_allocated_drive_letters()
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        if letter not in allocated:
            return f"{letter}:"
    return None


def _do_mount(args) -> None:
    import time

    # Select the drive letter here (via the shared helper) so we know which
    # letter to poll for after spawning the detached mount.
    drive_letter = _select_drive_letter()
    if drive_letter is None:
        _log("No available drive letter found")
        _show_error("AmiFUSE", "No available drive letter found. Cannot mount.")
        return

    python_dir = Path(sys.executable).parent
    python_exe = str(python_dir / "pythonw.exe")
    if not os.path.isfile(python_exe):
        python_exe = sys.executable

    cmd = [python_exe, "-m", "amifuse", "mount"]
    if args.write:
        cmd.append("--write")
    cmd.append("--mountpoint")
    cmd.append(drive_letter)
    cmd.append("--daemon")
    cmd.append(args.image)

    _log(f"Launching mount: {cmd}")
    try:
        _spawn_detached(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as exc:
        _log(f"Failed to launch mount subprocess: {exc}")
        _show_error("AmiFUSE", f"Failed to launch mount process:\n{exc}")
        return

    # Poll for the mount to appear, surfacing failures via a dialog instead of
    # exiting silently. os.path.exists is the CORRECT probe here for "did my
    # mount appear" -- a live WinFSP mount has media, so the path becomes
    # reachable -- but it is WRONG for "is a letter allocated" (that is the bug
    # _select_drive_letter avoids by using the GetLogicalDrives bitmask). Do
    # not consolidate the two probes.
    #
    # This intentionally blocks up to MOUNT_POLL_TIMEOUT before main() reaches
    # os._exit(0). Safe because the mountro verb is launched hidden and
    # non-waiting via `WScript.Shell.Run cmd, 0, False`, so Explorer's UI
    # thread is not blocked by this wait. Do not "optimize" the poll away -- it
    # is what surfaces mount failures instead of re-silencing them.
    mount_path = f"{drive_letter}\\"
    poll_interval = 0.25
    elapsed = 0.0
    while elapsed < MOUNT_POLL_TIMEOUT:
        time.sleep(poll_interval)
        elapsed += poll_interval
        if os.path.exists(mount_path):
            _log(f"Mount ready at {drive_letter} after {elapsed:.1f}s")
            _ensure_tray_running()
            return

    _log(f"Mount timed out after {MOUNT_POLL_TIMEOUT}s for {drive_letter}")
    # Start the tray on the timeout path too: a slow cold mount (vamos + m68k
    # init on a large multi-partition HDF) may still be initializing, so this
    # may be a slow success rather than a failure. The tray self-manages and
    # auto-exits shortly if no mounts are present, so starting it here is
    # harmless even on a genuine failure.
    _ensure_tray_running()
    _show_error(
        "AmiFUSE",
        f"Drive {drive_letter} did not appear within "
        f"{MOUNT_POLL_TIMEOUT:.0f} seconds.\n\n"
        "It may still be starting up -- check Explorer for the drive, or see "
        "the log if it doesn't appear.",
    )


def _do_inspect(args) -> None:
    # Launch python directly with CREATE_NEW_CONSOLE to avoid cmd.exe
    # metacharacter injection. The -c script uses sys.argv for the path
    # (never interpolated into code) and waits for a keypress so the user
    # can read output before the console closes.
    wrapper = [
        sys.executable, "-c",
        "import sys; from amifuse.fuse_fs import main; main(['inspect'] + sys.argv[1:]); input('\\nPress Enter to close...')",
        args.image,
    ]
    subprocess.Popen(wrapper, creationflags=CREATE_NEW_CONSOLE)


def _do_open(args) -> None:
    """Mount an image and open Explorer to the mounted drive."""
    import time

    # Select an available drive letter via the shared helper (GetLogicalDrives
    # bitmask), so the launcher and the core mount path agree on which letters
    # are free -- see _select_drive_letter for why os.path.exists is wrong here.
    drive_letter = _select_drive_letter()

    if drive_letter is None:
        _log("No available drive letter found")
        _show_error("AmiFUSE", "No available drive letter found. Cannot mount.")
        return

    # Launch mount with explicit mountpoint
    python_dir = Path(sys.executable).parent
    python_exe = str(python_dir / "pythonw.exe")
    if not os.path.isfile(python_exe):
        python_exe = sys.executable

    cmd = [python_exe, "-m", "amifuse", "mount", "--write", "--mountpoint", drive_letter, "--daemon", args.image]

    _log(f"Launching open-mount: {cmd}")
    try:
        _spawn_detached(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as exc:
        _log(f"Failed to launch mount: {exc}")
        _show_error("AmiFUSE", f"Failed to launch mount process:\n{exc}")
        return

    # Poll for mount to become ready. os.path.exists is the CORRECT probe here
    # for "did my mount appear" (a live WinFSP mount has media) but WRONG for
    # "is a letter allocated" -- selection above uses the GetLogicalDrives
    # bitmask via _select_drive_letter instead. Do not merge the two probes.
    mount_path = f"{drive_letter}\\"
    timeout = MOUNT_POLL_TIMEOUT
    poll_interval = 0.25
    elapsed = 0.0

    while elapsed < timeout:
        time.sleep(poll_interval)
        elapsed += poll_interval
        if os.path.exists(mount_path):
            _log(f"Mount ready at {drive_letter} after {elapsed:.1f}s, opening Explorer")
            os.startfile(mount_path)
            _ensure_tray_running()
            return

    # Timeout — the drive may still be initializing (slow cold mount)
    _log(f"Mount timed out after {timeout}s for {drive_letter}")
    # Start the tray on the timeout path too: a slow cold mount (vamos + m68k
    # init on a large multi-partition HDF) may still be initializing, so this
    # may be a slow success rather than a failure. The tray self-manages and
    # auto-exits shortly if no mounts are present, so starting it here is
    # harmless even on a genuine failure.
    _ensure_tray_running()
    _show_error(
        "AmiFUSE",
        f"Drive {drive_letter} did not appear within "
        f"{timeout:.0f} seconds.\n\n"
        "It may still be starting up -- check Explorer for the drive, or see "
        "the log if it doesn't appear.",
    )


def _show_error(title: str, message: str) -> None:
    """Show a Windows MessageBox error. Best-effort; never raises."""
    try:
        ctypes.windll.user32.MessageBoxW(0, ctypes.c_wchar_p(message), ctypes.c_wchar_p(title), 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _ensure_tray_running() -> None:
    handle = ctypes.windll.kernel32.OpenMutexW(
        0x00100000, False, "Local\\AmiFUSE_Tray_Mutex"
    )
    if handle != 0:
        ctypes.windll.kernel32.CloseHandle(handle)
        return

    tray_exe = str(Path(sys.executable).parent / "amifuse-tray.exe")
    if os.path.isfile(tray_exe):
        cmd = [tray_exe]
    else:
        cmd = [sys.executable, "-m", "amifuse.tray"]

    _log(f"Starting tray: {cmd}")
    try:
        _spawn_detached(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as exc:
        _log(f"Failed to start tray: {exc}")


if __name__ == "__main__":
    main()
