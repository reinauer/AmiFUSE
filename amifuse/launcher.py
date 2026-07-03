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
from dataclasses import dataclass
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
    if not sys.platform.startswith("win"):
        raise SystemExit("The AmiFUSE launcher is only supported on Windows.")

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


# Drive letters A:/B:/C: are reserved (floppy / system), so auto-selection
# starts at D:. Shared by _select_drive_letter and _select_drive_letters so the
# single-letter and multi-letter paths iterate one identical alphabet -- an A-Z
# alphabet would hand back A:/B:/C: and over-count the free total.
_DRIVE_LETTER_ALPHABET = "DEFGHIJKLMNOPQRSTUVWXYZ"


def _select_drive_letters(n: int) -> tuple[list[str], int]:
    """Return up to ``n`` free drive letters plus the total number available.

    Selection uses ``platform._windows_allocated_drive_letters()`` (the Win32
    GetLogicalDrives bitmask -- the *allocation* probe) rather than
    ``os.path.exists`` (the *arrival* probe) so an assigned-but-empty removable
    slot (e.g. an empty card-reader D:) is correctly treated as taken. The
    helper returns bare uppercase letters, so candidates are compared as
    uppercase and formatted back into ``"X:"`` strings.

    Args:
        n: Maximum number of free letters to return.

    Returns:
        A ``(chosen, total_free)`` tuple where ``chosen`` is up to ``n`` free
        letters as ``"X:"`` strings in ``_DRIVE_LETTER_ALPHABET`` order, and
        ``total_free`` is the total count of free letters in that alphabet. When
        ``total_free < n`` the caller has more partitions than free letters and
        can mount ``chosen`` best-effort while reporting the shortfall.
    """
    allocated = platform._windows_allocated_drive_letters()
    free = [f"{c}:" for c in _DRIVE_LETTER_ALPHABET if c not in allocated]
    return free[:n], len(free)


def _select_drive_letter() -> Optional[str]:
    """Return the first unallocated drive letter as an ``"X:"`` string, or None.

    Thin N=1 wrapper over :func:`_select_drive_letters`; returns ``None`` when no
    letter is free, preserving the original no-free-letter contract. Both share
    ``_DRIVE_LETTER_ALPHABET`` so the single- and multi-letter paths never drift.
    """
    chosen, _ = _select_drive_letters(1)
    return chosen[0] if chosen else None


@dataclass
class _MountUnit:
    """One drive to mount in a fan-out.

    ``name`` is the ``--partition`` key (the RDB drive name). It is ``None`` for
    the degenerate single-unit case -- an ADF/ISO, a single-partition RDB, the
    duplicate-name fallback, or an un-enumerable image -- so the spawned command
    omits ``--partition`` and is byte-for-byte the legacy single-partition path
    (a lone partition IS index 0, which the no-``--partition`` default resolves
    to). ``label`` is the human name used in the summary dialog.
    """
    name: Optional[str]
    label: str


def _python_exe() -> str:
    """Return the console-free ``pythonw.exe`` beside the interpreter, or the
    interpreter itself when it is absent. Same resolution the handlers used
    before the fan-out."""
    python_exe = str(Path(sys.executable).parent / "pythonw.exe")
    if not os.path.isfile(python_exe):
        python_exe = sys.executable
    return python_exe


def _aggregate_timeout(n: int) -> float:
    """Seconds to wait for ``n`` concurrently-spawned mounts to all appear.

    N=1 stays at exactly ``MOUNT_POLL_TIMEOUT`` (15.0) so the single-partition
    path is unchanged. For N>1 the fan-out spawns all mounts first and then
    polls a SINGLE shared deadline (not 15s x N); each extra partition adds 5s
    of headroom for its concurrent vamos + m68k cold-init, capped at 45s:
    ``min(15 + 5*(n - 1), 45)``. N=3 -> 25s; N>=7 -> 45s.
    """
    if n <= 1:
        return MOUNT_POLL_TIMEOUT
    return float(min(15 + 5 * (n - 1), 45))


def _enumerate_mount_units(image: str) -> tuple[list[_MountUnit], Optional[str]]:
    """Return ``(units, fallback_reason)`` describing what to mount.

    Mirrors ``fuse_fs.mount_fuse``'s detection order: an ADF or an ISO is a
    single logical unit (no fan-out); otherwise the RDB partitions are
    enumerated by name via :func:`rdb_inspect.list_partitions`.

    A lone partition (single-partition RDB, or the duplicate-name fallback where
    ``list_partitions`` returns index 0 of the first RDB) collapses to a single
    ``_MountUnit`` with ``name=None`` so the spawn is byte-for-byte the legacy
    single-partition command.

    Any enumeration failure (non-RDB / unreadable image, IOError from
    ``open_rdisk``) degrades to a single no-``--partition`` unit -- exactly the
    legacy behavior: spawn one mount and let the mount process itself surface a
    failure via the poll timeout. The launcher never crashes on enumeration.
    """
    try:
        from amifuse import rdb_inspect
        img = Path(image)
        if rdb_inspect.detect_adf(img) is not None:
            return [_MountUnit(name=None, label="floppy disk")], None
        if rdb_inspect.detect_iso(img) is not None:
            return [_MountUnit(name=None, label="disc")], None
        result = rdb_inspect.list_partitions(img)
    except Exception as exc:
        _log(
            f"Partition enumeration failed for {image}: {exc!r}; "
            "falling back to a single-partition mount"
        )
        return [_MountUnit(name=None, label="disk image")], None

    parts = result.partitions
    if len(parts) <= 1:
        label = parts[0].name if parts else "disk image"
        return [_MountUnit(name=None, label=label)], result.fallback_reason
    units = [_MountUnit(name=p.name, label=p.name) for p in parts]
    return units, result.fallback_reason


def _report_summary(succeeded, skipped, spawn_failed, failed, fallback_reason) -> None:
    """Show at most ONE summary dialog for the whole fan-out.

    No dialog on a clean full success (Explorer opening the drive(s) is the
    confirmation) -- this preserves the pre-fan-out single-partition behavior.
    Otherwise a single ``_show_error`` names, in one composed body, the mounted
    drives, the skipped (no free letter) partitions, the ``spawn_failed`` ones
    (the process never started -- DEFINITE wording), and the ``failed`` ones (the
    process spawned but its drive never appeared -- SOFT wording, may be a slow
    cold mount), plus any ``fallback_reason``. Never one dialog per partition.
    """
    if not skipped and not spawn_failed and not failed and not fallback_reason:
        return

    parts = []
    if succeeded:
        got = ", ".join(f"{u.label} ({letter})" for u, letter in succeeded)
        parts.append(f"Mounted {got}.")
    if skipped:
        names = ", ".join(u.label for u in skipped)
        parts.append(f"Could not mount {names}: no free drive letter.")
    if spawn_failed:
        # Definite wording: the mount process never launched, so this is a hard
        # failure -- do NOT soften it into "may still be starting".
        for u, letter, reason in spawn_failed:
            parts.append(
                f"Could not start mount for {u.label} ({letter}): {reason}"
            )
    if failed:
        names = ", ".join(f"{u.label} ({letter})" for u, letter in failed)
        # Soft wording: a timed-out drive may be a slow cold mount, not a
        # genuine failure -- do not assert failure.
        parts.append(
            f"{names} did not appear yet -- may still be starting; "
            "check Explorer for the drive, or see the log if it doesn't appear."
        )
    if fallback_reason:
        parts.append(fallback_reason)

    _show_error("AmiFUSE", "\n\n".join(parts))


def mount_image_all(image: str, *, write: bool, explorer: str) -> None:
    """Mount every partition of ``image``, one per free drive letter (fan-out).

    Shared core behind both shell paths: ``_do_open`` calls it with
    ``write=True, explorer="per_drive"`` (one Explorer window per confirmed
    drive) and ``_do_mount`` with ``write=False, explorer="refresh"`` (no
    windows; each mount's FUSE ``init()`` refreshes its own drive via
    ``platform.notify_shell_drive_change`` at fuse_fs.py:2715-2719, so the
    launcher adds no refresh code).

    Steps: enumerate units; allocate up to N free letters (best-effort partial
    on exhaustion); spawn all mounts FIRST; poll a single aggregate deadline for
    every drive to appear; start the tray once; show at most one summary dialog.

    N=1 is the identity case -- a single unit reproduces the pre-fan-out path
    exactly: one spawn (no ``--partition``), a 15s poll, one Explorer window on
    ``open``, and no dialog on success.
    """
    import time

    units, fallback_reason = _enumerate_mount_units(image)
    if fallback_reason:
        _log(fallback_reason)

    chosen, total_free = _select_drive_letters(len(units))
    if total_free == 0:
        _log("No available drive letter found")
        _show_error("AmiFUSE", "No available drive letter found. Cannot mount.")
        return

    # Best-effort partial: mount as many partitions as there are free letters,
    # in stable RDB order; the rest are skipped (never abort the ones that fit).
    pairs = list(zip(units, chosen))
    skipped = units[len(pairs):]

    python_exe = _python_exe()

    # Spawn ALL mounts first (they are independent processes -- verified N=2
    # concurrent RW), then poll a single shared deadline below.
    pending: dict[str, _MountUnit] = {}   # letter -> unit awaiting arrival
    # Two distinct failure buckets. spawn_failed: the process NEVER launched
    # (_spawn_detached raised) -- a definite, hard failure. failed (below): the
    # process spawned but its drive never appeared by the deadline -- a SOFT
    # timeout that may still be a slow cold mount. Folding the two together would
    # mislabel a never-started mount as "may still be starting".
    spawn_failed: list[tuple[_MountUnit, str, str]] = []  # (unit, letter, reason)
    failed: list[tuple[_MountUnit, str]] = []
    for unit, letter in pairs:
        cmd = [python_exe, "-m", "amifuse", "mount"]
        if unit.name is not None:
            cmd += ["--partition", unit.name]
        if write:
            cmd.append("--write")
        cmd += ["--mountpoint", letter, "--daemon", image]

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
            _log(f"Failed to launch mount for {letter}: {exc}")
            spawn_failed.append((unit, letter, str(exc)))
            continue
        pending[letter] = unit

    # Single aggregate-deadline poll. os.path.exists("<letter>\\") is the CORRECT
    # "did my mount appear" probe (a live WinFSP mount has media) and is distinct
    # from the GetLogicalDrives allocation bitmask used to SELECT letters -- do
    # not merge the two probes. This blocks up to the aggregate deadline before
    # main() reaches os._exit(0); safe because both shell verbs launch hidden and
    # non-waiting via `WScript.Shell.Run cmd, 0, False`, so Explorer's UI thread
    # is not blocked. Do not "optimize" the poll away -- it is what surfaces
    # mount failures instead of re-silencing them.
    succeeded: list[tuple[_MountUnit, str]] = []
    if pending:
        timeout = _aggregate_timeout(len(pending))
        poll_interval = 0.25
        elapsed = 0.0
        while pending and elapsed < timeout:
            time.sleep(poll_interval)
            elapsed += poll_interval
            for letter in list(pending):
                if os.path.exists(letter + "\\"):
                    unit = pending.pop(letter)
                    succeeded.append((unit, letter))
                    _log(f"Mount ready at {letter} after {elapsed:.1f}s")
                    if explorer == "per_drive":
                        # One Explorer window per confirmed drive, opened only
                        # once the drive is present (skipped/failed get none).
                        try:
                            os.startfile(letter + "\\")
                        except OSError as exc:
                            _log(f"Could not open Explorer for {letter}: {exc}")

        # Whatever is still pending timed out -- report softly (may be a slow
        # cold mount, an unformatted/no-driver partition that SystemExited in its
        # own process, or a TOCTOU-lost letter). The good drives are unaffected.
        if pending:
            _log(f"Mounts did not appear within {timeout:.0f}s: {list(pending)}")
            failed.extend((unit, letter) for letter, unit in pending.items())

    # Start the tray exactly ONCE after the poll (idempotent via the
    # Local\AmiFUSE_Tray_Mutex). A slow cold mount may still be initializing, so
    # start it even on a timeout; the tray auto-exits if no mounts are present.
    _ensure_tray_running()

    _report_summary(succeeded, skipped, spawn_failed, failed, fallback_reason)


def _do_mount(args) -> None:
    """Read-only fan-out (the ``mountro`` shell verb). Opens no windows -- each
    mount's ``init()`` refreshes its own drive. The only shell verb routing here
    is ``mountro``, which passes no ``--write``, so ``args.write`` is False."""
    mount_image_all(args.image, write=args.write, explorer="refresh")


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
    """Read-write fan-out (the ``open`` shell verb -- double-click and "Mount &&
    Open"). Mounts every partition ``--write`` and opens one Explorer window per
    confirmed drive."""
    mount_image_all(args.image, write=True, explorer="per_drive")


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
