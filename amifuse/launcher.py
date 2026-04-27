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

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

_DETACHED_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

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

    inspect_p = sub.add_parser("inspect", help="Open inspect in a new console")
    inspect_p.add_argument("image")

    args = parser.parse_args(argv)

    if args.command == "mount":
        _do_mount(args)
    elif args.command == "inspect":
        _do_inspect(args)

    # Force-exit immediately. Python's normal shutdown (atexit handlers,
    # logging.shutdown, GC, module cleanup) is unnecessary for a launcher
    # and can delay process exit enough to hang Explorer.
    os._exit(0)


def _do_mount(args) -> None:
    python_dir = Path(sys.executable).parent
    python_exe = str(python_dir / "pythonw.exe")
    if not os.path.isfile(python_exe):
        python_exe = sys.executable

    cmd = [python_exe, "-m", "amifuse", "mount"]
    if args.write:
        cmd.append("--write")
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
        return

    _ensure_tray_running()


def _do_inspect(args) -> None:
    cmd = ["cmd", "/k", sys.executable, "-m", "amifuse", "inspect", args.image]
    subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE)


def _ensure_tray_running() -> None:
    handle = ctypes.windll.kernel32.OpenMutexW(
        0x00100000, False, "AmiFUSE_Tray_Mutex"
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
