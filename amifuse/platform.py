"""
Platform abstraction layer for amifuse.

This module provides platform-specific functionality with a unified interface,
including mount options, default mountpoints, unmount commands, icon handling,
and driver resolution.

Platform-specific implementations:
- macOS/Darwin: icon_darwin.py
- Linux: (future) icon_linux.py
- Windows: (future) icon_windows.py
"""

import errno
import json
import logging
import os
import posixpath
import re
import signal
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from .icon_darwin import DarwinIconHandler

# Prevent subprocess calls from flashing a console window when invoked from
# pythonw.exe (e.g. tray/launcher).
_CREATE_NO_WINDOW = 0x08000000

# signal.SIGKILL is not defined on Windows; fall back to SIGTERM so that
# _kill_mount_owner_processes() can still compile and will use the strongest
# signal available.
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

# ---------------------------------------------------------------------------
# Driver folder
# ---------------------------------------------------------------------------


def get_primary_driver_dir() -> Path:
    """Return the user's drivers folder, searched for their own FastFileSystem.

    driver_resolver uses it both to look for the driver and to name the
    folder in messages, so the two always agree.
    """
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            return Path(local) / "amifuse" / "drivers"
    return Path.home() / ".local" / "share" / "amifuse" / "drivers"


def _windows_allocated_drive_letters() -> Set[str]:
    """Return the set of drive letters currently allocated on Windows.

    Uses the Win32 ``GetLogicalDrives`` bitmask, which reports letter
    *allocation* regardless of whether media is present. This is the correct
    probe for "is this drive letter taken" -- unlike ``os.path.exists``, which
    false-negatives on an assigned-but-empty removable slot (e.g. an empty
    card-reader ``D:``) and led AmiFUSE to auto-select a letter that WinFsp
    then refused with "mount point in use".

    Returns:
        A set of bare, uppercase, single-character letters (e.g. ``{"C", "D"}``
        -- not ``"C:"`` strings). On non-Windows platforms returns an empty set
        without ever touching ``windll``.

    Bitmask decode: bit 0 = A:, bit 1 = B:, ... bit 25 = Z:.
    """
    if not sys.platform.startswith("win"):
        return set()

    # Conservative fallback for API failure. ``GetLogicalDrives`` returns 0
    # only when the call fails (a real system always has at least C: allocated).
    # Decoding 0 naively would yield an empty set, making *every* letter look
    # free and re-selecting the empty D: this fix removes. Instead we treat
    # every letter as allocated, so auto-selection declines to pick a letter
    # rather than picking a bad one, and validation rejects rather than
    # waving through. Callers must handle the "no free letter" outcome.
    all_letters = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    try:
        # windll is Windows-only; keep the reference inside the function body so
        # merely importing this module never touches it on macOS/Linux. Matches
        # the ctypes idiom used elsewhere in this file (e.g. _pid_exists).
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return all_letters

    if not mask:
        return all_letters

    return {chr(ord("A") + i) for i in range(26) if mask & (1 << i)}


def get_default_mountpoint(volname: str) -> Optional[Path]:
    """Get the default mountpoint for the current platform.

    Args:
        volname: Volume name to use in the mountpoint path

    Returns:
        Default mountpoint path, or None if platform requires explicit mountpoint
    """
    if sys.platform.startswith("darwin"):
        # The name comes from the image: keep it a single path component so
        # the mountpoint is always a direct child of /Volumes, and printable
        # so that no message naming the mountpoint carries a control character.
        name = "".join(
            "_" if ch == "/" or not ch.isprintable() else ch for ch in volname)
        if name in ("", ".", ".."):
            name = "_" + name
        return Path(f"/Volumes/{name}")
    elif sys.platform.startswith("win"):
        # Find first unallocated drive letter (skip A/B floppy and C system).
        # Use the GetLogicalDrives bitmask, not os.path.exists: the latter
        # false-negatives on assigned-but-empty removable slots (e.g. D:).
        allocated = _windows_allocated_drive_letters()
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            if letter not in allocated:
                return Path(f"{letter}:")
        return None  # No available drive letter
    else:
        # Linux requires explicit mountpoint
        return None


def windows_drive_mountpoint(mountpoint) -> Optional[str]:
    """Return X: for a drive-root alias, including WinFsp mount-manager form."""
    import ntpath
    import re

    path = ntpath.normpath(str(mountpoint))
    if path.startswith(("\\\\.\\", "\\\\?\\")):
        path = path[4:]
    if re.fullmatch(r"[A-Za-z]:\\?", path):
        return path[:2].upper()
    return None


def is_windows_admin() -> bool:
    """Whether the current token can create a global mount-manager drive."""
    if not sys.platform.startswith("win"):
        return False
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def get_fuse_mountpoint(mountpoint: Path) -> str:
    """Use global drive letters for elevated Windows mounts."""
    if sys.platform.startswith("win"):
        drive = windows_drive_mountpoint(mountpoint)
        if drive:
            explicit = str(mountpoint).replace("/", "\\").startswith(
                ("\\\\.\\", "\\\\?\\"))
            elevated = is_windows_admin()
            if explicit and not elevated:
                raise ValueError(
                    "Global drive mounts require Administrator privileges; "
                    "run from an Administrator shell or use a plain drive letter."
                )
            # Do not pass a trailing slash: WinFsp interprets it as a
            # mount-manager directory instead of a mount-manager drive.
            return "\\\\.\\" + drive if elevated else drive
    return str(mountpoint)


def should_auto_create_mountpoint(mountpoint: Path) -> bool:
    """Check if the mountpoint should be auto-created by the FUSE library.

    Args:
        mountpoint: The mountpoint path

    Returns:
        True if FUSE will create it automatically, False if we need to create it
    """
    if sys.platform.startswith("darwin"):
        import posixpath

        # macFUSE creates mount points directly in /Volumes automatically.
        # Compare the normalized parent, so a path such as /Volumes/a/b or
        # /Volumes/../x is created here when missing.
        path = posixpath.normpath(str(mountpoint))
        return posixpath.dirname(path) == "/Volumes"
    if sys.platform.startswith("win"):
        # WinFSP handles drive letter mountpoints; don't mkdir them.
        # Directory mountpoints (e.g. C:\mnt\amiga) still need mkdir.
        return windows_drive_mountpoint(mountpoint) is not None
    return False


def get_unmount_command(mountpoint: Path) -> List[str]:
    """Get the command to unmount a FUSE filesystem.

    Args:
        mountpoint: The mountpoint to unmount

    Returns:
        Command as a list of strings suitable for subprocess.
        Returns an empty list [] on platforms where no unmount command is
        available (Windows/WinFSP). Callers must handle the empty-list
        case -- `amifuse unmount` requests cooperative shutdown from the
        Windows mount owner instead.
    """
    if sys.platform.startswith("darwin"):
        return ["umount", "-f", str(mountpoint)]
    if sys.platform.startswith("win"):
        return _get_windows_unmount_command(mountpoint)
    # Linux - prefer fusermount/fusermount3 if available
    if shutil.which("fusermount"):
        return ["fusermount", "-u", str(mountpoint)]
    if shutil.which("fusermount3"):
        return ["fusermount3", "-u", str(mountpoint)]
    return ["umount", "-f", str(mountpoint)]


def _get_winfsp_install_dir() -> Optional[str]:
    """Locate the WinFSP installation directory.

    Checks (in order): Registry, WINFSP_INSTALL_DIR env var, default path.
    Returns the directory path string, or None if WinFSP is not found.
    """
    # Registry check (most reliable)
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\WinFsp",
        ) as key:
            install_dir = winreg.QueryValueEx(key, "InstallDir")[0]
            if install_dir and os.path.isdir(install_dir):
                return install_dir
    except Exception:
        pass

    # Env var fallback
    winfsp_dir = os.environ.get("WINFSP_INSTALL_DIR", "")
    if winfsp_dir and os.path.isdir(winfsp_dir):
        return winfsp_dir

    # Default install path fallback
    default_dir = r"C:\Program Files (x86)\WinFsp"
    if os.path.isdir(default_dir):
        return default_dir

    return None


def _get_windows_unmount_command(mountpoint: Path) -> List[str]:
    """Build a Windows unmount command for the given mountpoint.

    WinFSP mounts are not network drives, so ``net use /delete`` does not
    work for them.  Return an empty list so that ``cmd_unmount`` falls
    through to the cooperative Windows unmount protocol.
    """
    return []


def check_fuse_available() -> None:
    """Verify that the platform's FUSE driver is installed.

    Raises SystemExit with an actionable error message if the required
    FUSE driver is not found.
    """
    if not sys.platform.startswith("win"):
        # macOS and Linux: fusepy will raise its own error if FUSE is missing.
        # Those errors are already clear enough (libfuse not found, etc.)
        return

    # Windows: delegate to shared WinFSP detection
    if _get_winfsp_install_dir() is not None:
        return

    # Non-standard installs can set WINFSP_INSTALL_DIR env var
    raise SystemExit(
        "WinFSP is not installed. AmiFUSE requires WinFSP to mount filesystems on Windows.\n"
        "\n"
        "Install WinFSP from: https://winfsp.dev/rel/\n"
        "After installing, restart your terminal and try again.\n"
        "\n"
        "If WinFSP is installed in a non-standard location, set the\n"
        "WINFSP_INSTALL_DIR environment variable to the install directory."
    )


def validate_mountpoint(mountpoint: Path) -> Optional[str]:
    """Validate that a mountpoint is available for use.

    Args:
        mountpoint: The mountpoint path to validate

    Returns:
        None if the mountpoint is available, or an error message string
        if it is already in use or otherwise invalid.
    """
    mp_str = str(mountpoint)
    drive = windows_drive_mountpoint(mountpoint) if sys.platform.startswith("win") else None
    if drive:
        # Drive letter mountpoint -- reject if the letter is already allocated.
        # Use the GetLogicalDrives bitmask (via the shared helper), not
        # os.path.exists, which false-negatives on assigned-but-empty removable
        # slots. Normalize case so an explicit lowercase "d:" is still caught.
        if drive[0] in _windows_allocated_drive_letters():
            return (
                f"Drive {drive} is already allocated; choose a different drive "
                f"letter or free it first."
            )
    else:
        # A drive name can match a symlink in /Volumes: /Volumes/Macintosh HD
        # points to /. Checked first, as os.path.exists follows the link, and
        # os.path.ismount below is False for one.
        if sys.platform.startswith("darwin") and os.path.islink(mp_str):
            return (
                f"Mountpoint {mountpoint} is a symbolic link, so a mount there "
                f"could hide the directory it points to; choose another with "
                f"--mountpoint (or, for the default mountpoint, another "
                f"volume name with --volname)."
            )
        try:
            path_exists = os.path.exists(mp_str)
        except OSError as exc:
            if _is_stale_mount_os_error(exc):
                return format_stale_mountpoint_error(mountpoint)
            return f"Mountpoint {mountpoint} is not accessible: {exc.strerror or exc}."
        if not path_exists:
            if not is_stale_mountpoint(mountpoint):
                return None
            return format_stale_mountpoint_error(mountpoint)
        try:
            if os.path.ismount(mp_str):
                if (sys.platform.startswith("darwin")
                        and not is_amifuse_mount_macos(mountpoint)):
                    # Not known to be AmiFUSE's (say, the user's own disk):
                    # unmounting it is not the way to free the path.
                    return (
                        f"Mountpoint {mountpoint} is already a mount, not one "
                        f"AmiFUSE can identify as its own; choose another with "
                        f"--mountpoint (or, for the default mountpoint, "
                        f"another volume name with --volname)."
                    )
                if get_unmount_command(mountpoint):
                    return (
                        f"Mountpoint {mountpoint} is already a mount; unmount it first "
                        f"(e.g. amifuse unmount {mountpoint})."
                    )
                else:
                    return (
                        f"Mountpoint {mountpoint} is already a mount. "
                        f"Stop the amifuse process to unmount (Ctrl+C)."
                    )
        except OSError as exc:
            if _is_stale_mount_os_error(exc):
                return format_stale_mountpoint_error(mountpoint)
            return f"Mountpoint {mountpoint} is not accessible: {exc.strerror or exc}."
    return None


def _is_stale_mount_os_error(exc: OSError) -> bool:
    return exc.errno in (errno.EIO, errno.ENOTCONN)


def is_stale_mountpoint(mountpoint: Path) -> bool:
    try:
        os.lstat(str(mountpoint))
    except FileNotFoundError:
        return False
    except OSError as exc:
        if _is_stale_mount_os_error(exc):
            return True
    return False


def format_stale_mountpoint_error(mountpoint: Path) -> str:
    if (sys.platform.startswith("darwin")
            and not is_amifuse_mount_macos(mountpoint)):
        # `amifuse unmount` refuses a mount it can't tie to AmiFUSE.
        return (
            f"Mountpoint {mountpoint} looks like a stale or broken mount, not "
            f"one AmiFUSE can identify as its own; choose another with "
            f"--mountpoint (or, for the default mountpoint, another "
            f"volume name with --volname)."
        )
    if get_unmount_command(mountpoint):
        return (
            f"Mountpoint {mountpoint} looks like a stale or broken mount; "
            f"unmount it first (e.g. amifuse unmount {mountpoint})."
        )
    return (
        f"Mountpoint {mountpoint} looks like a stale or broken mount. "
        f"Stop the amifuse process to unmount (Ctrl+C)."
    )


def fuse_option_value(value: str) -> str:
    """Replace what could end ``value`` or change the options around it.

    fusepy joins the options with commas into one -o argument and escapes
    nothing, and a volume name comes from the image. So each comma would
    start another option, and a NUL would end the argument and drop the
    options after it. A backslash is the escape of libfuse and WinFsp: at
    the end of a value it would take the comma that follows and join the
    next option to the value. Escaping the comma is no way out: macFUSE's
    mount_macfuse (5.3.3) splits at every comma, escaped or not, to check the
    options and set their flags. Other control characters go the same way.
    """
    return re.sub(r"[,\\\x00-\x1f\x7f]", "_", value)


def get_mount_options(volname: str, volicon_path: Optional[str] = None,
                      icons_enabled: bool = False) -> dict:
    """Get platform-specific FUSE mount options.

    Args:
        volname: Volume name to display
        volicon_path: Path to volume icon file (platform-specific)
        icons_enabled: Whether icon mode is enabled

    Returns:
        Dictionary of mount options for FUSE
    """
    if sys.platform.startswith("darwin"):
        from .icon_darwin import get_darwin_mount_options
        return get_darwin_mount_options(volname, volicon_path, icons_enabled)
    if sys.platform.startswith("win"):
        return {
            "volname": volname,
            "FileSystemName": "AmiFUSE",
            "FileInfoTimeout": "1000",
        }
    # Linux doesn't need special mount options
    return {}


def get_icon_handler(icons_enabled: bool = False, debug: bool = False):
    """Get the platform-specific icon handler.

    Args:
        icons_enabled: Whether icon mode is enabled
        debug: Enable debug output

    Returns:
        Platform-specific icon handler instance, or None if not supported
    """
    if not icons_enabled:
        return None

    if sys.platform.startswith("darwin"):
        from .icon_darwin import DarwinIconHandler
        return DarwinIconHandler(icons_enabled=True, debug=debug)

    # Linux/Windows icon support not yet implemented
    return None


def get_icon_file_names() -> tuple:
    """Get the virtual icon file names for the current platform.

    Returns:
        Tuple of (folder_icon_name, volume_icon_name), or (None, None) if not supported
    """
    if sys.platform.startswith("darwin"):
        from .icon_darwin import ICON_FILE, VOLUME_ICON_FILE
        return (ICON_FILE, VOLUME_ICON_FILE)
    # Other platforms don't use virtual icon files (yet)
    return (None, None)


def supports_icons() -> bool:
    """Check if the current platform supports custom icon display.

    Returns:
        True if icons are supported on this platform
    """
    return sys.platform.startswith("darwin")


def pre_generate_volume_icon(bridge, debug: bool = False) -> Optional[Path]:
    """Pre-generate volume icon before mounting (platform-specific).

    Some platforms (macOS) require the volume icon to be available at mount time.
    This function reads Disk.info and generates the icon file.

    Args:
        bridge: HandlerBridge instance for reading files from the Amiga filesystem
        debug: Enable debug output

    Returns:
        Path to temporary icon file, or None if not applicable/available
    """
    if not sys.platform.startswith("darwin"):
        return None

    # Import here to avoid circular imports
    import tempfile
    from .icon_parser import IconParser
    from .icon_parser import create_icns

    # Find Disk.info case-insensitively by listing root directory
    info_name = None
    try:
        root_entries = bridge.list_dir_path("/")
        for entry in root_entries:
            name = entry.get("name", "")
            if name.lower() == "disk.info":
                info_name = name
                break
    except Exception:
        pass

    if not info_name:
        if debug:
            print("[amifuse] No Disk.info found for volume icon", flush=True)
        return None

    stat = bridge.stat_path("/" + info_name)
    if not stat:
        return None

    file_size = stat.get("size", 0)
    if file_size == 0:
        return None

    data = bridge.read_file("/" + info_name, file_size, 0)
    if not data:
        return None

    if debug:
        print(f"[amifuse] Found {info_name} ({len(data)} bytes)", flush=True)

    # Parse the icon
    parser = IconParser(debug=debug)
    icon_info = parser.parse(data)
    if not icon_info:
        if debug:
            print(f"[amifuse] Failed to parse icon from {info_name}", flush=True)
        return None

    # Generate ICNS
    aspect_ratio = icon_info.get("aspect_ratio", 1.0)
    icns_data = create_icns(
        icon_info["rgba"], icon_info["width"], icon_info["height"],
        debug=debug, aspect_ratio=aspect_ratio
    )
    if not icns_data:
        return None

    # Save to temp file
    fd, temp_path = tempfile.mkstemp(suffix=".icns", prefix="amifuse_volicon_")
    os.write(fd, icns_data)
    os.close(fd)

    if debug:
        print(f"[amifuse] Generated volume icon: {temp_path} ({len(icns_data)} bytes)", flush=True)

    return Path(temp_path)


# ---------------------------------------------------------------------------
# Mount discovery
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


def find_amifuse_mounts():
    """Discover all active amifuse mount processes on the system.

    Returns a list of dicts, each with keys:
        - mountpoint (str)
        - image (str or None)
        - pid (int)
        - uptime_seconds (int or None)
        - filesystem_type (None -- reserved for future use)

    Raises OSError if the process discovery command fails unexpectedly.
    Returns an empty list if the discovery tool is unavailable.
    """
    if sys.platform.startswith("win"):
        mounts = _find_amifuse_mounts_windows()
    else:
        mounts = _find_amifuse_mounts_unix()

    mounts = _deduplicate_fusepy_children(mounts)
    _apply_mount_records(mounts)
    _enrich_null_mountpoints(mounts)
    return mounts


def _strip_matched_quotes(value):
    """Strip a single matched pair of surrounding double quotes.

    On Windows, mount discovery tokenizes the command line with
    ``shlex.split(cmdline, posix=False)``, which retains the surrounding quote
    characters that ``subprocess.list2cmdline`` adds around paths containing
    spaces. This leaks literal quotes into the reported image/mountpoint. Strip
    only a *matched* leading+trailing ``"`` pair; leave unbalanced input (a quote
    on one side only) untouched. On the Unix path (posix=True) tokens carry no
    surrounding quotes, so this is a no-op there.
    """
    if value and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


# The `mount` subcommand's options (fuse_fs.main's mount parser).
_MOUNT_VALUE_OPTIONS = (
    "--driver", "--mountpoint", "--partition", "--block-size", "--volname",
)
_MOUNT_SWITCHES = (
    "--daemon", "--interactive", "--foreground", "--debug", "--trace",
    "--profile", "--write", "--icons", "--help",
)


def _mount_option(name):
    """Return the `mount` option ``name`` stands for, or None.

    argparse takes an option's full name or any unambiguous prefix of it, so
    anything else that starts with "-" can't be an option of a running
    mount: it is a piece of a value `ps` split at its spaces. So is -h or
    --help: argparse prints the help and exits.
    """
    options = tuple(option for option in _MOUNT_VALUE_OPTIONS + _MOUNT_SWITCHES
                    if option != "--help")
    if name in options or name == "--":
        return name
    if len(name) < 3 or not name.startswith("--"):
        return None
    matches = [option for option in options if option.startswith(name)]
    return matches[0] if len(matches) == 1 else None


def _parse_mount_tokens(tokens):
    """Extract image path and --mountpoint value from amifuse command tokens.

    The image is the first positional arg after the 'mount' subcommand.
    Handles both 'python -m amifuse mount <image> ...' and
    'amifuse mount <image> ...' invocation forms.

    Returns (image, mountpoint) where either may be None if not found.
    """
    # Find the index of 'mount' subcommand
    mount_idx = None
    for i, tok in enumerate(tokens):
        if tok == "mount":
            mount_idx = i
            break
    if mount_idx is None:
        return None, None

    # Image is the first positional arg after 'mount' (not starting with '-')
    image = None
    mountpoint = None
    # Positional tokens after the mountpoint value: those right after it
    # (`run`), and all of them (`after`); see the check after the loop.
    run = after = 0
    in_run = False
    image_first = False
    i = mount_idx + 1
    while i < len(tokens):
        tok = tokens[i]
        # argparse also takes "--option=VALUE" and any unambiguous prefix
        # ("--mount PATH"). A Windows command line may quote the whole token
        # ("--mountpoint=C:\a b"), which the Windows tokenizer keeps as one.
        name, eq, value = _strip_matched_quotes(tok).partition("=")
        option = _mount_option(name)
        if option is not None and eq and option not in _MOUNT_VALUE_OPTIONS:
            option = None  # argparse refuses "--write=1" and "--=x"
        if option is not None:
            # Only a value option ends the run of pieces after the
            # mountpoint. A switch or "--" there may be a piece of a split
            # path (`/Volumes/Games --write WHD`), and a "--" in the run
            # counts as one. What `ps` can't settle: a mountpoint like
            # "/Volumes/Games --write" looks exactly like a real --write.
            # A mount that is up has a mount record, which replaces this
            # reading. Before that, or when no record could be written, a
            # wrong reading shows the shorter path in `status`, and `amifuse
            # unmount` of that path can take another program's FUSE-T mount
            # for AmiFUSE's or, in its kill fallback on Linux/macOS, pick
            # the wrong AmiFUSE process (as `main` already could for the
            # `--mountpoint X` form).
            in_run = in_run and option not in _MOUNT_VALUE_OPTIONS
            if option == "--" and mountpoint is not None:
                run += in_run
            i += 1
            if eq and value.count('"') % 2:
                # cmd.exe passes --mountpoint="C:\a b" on as typed, and the
                # Windows tokenizer splits it at each run of spaces, so the
                # value can't be rebuilt exactly. Skip its pieces; for the
                # mountpoint, report none rather than part of one.
                while i < len(tokens) and not tokens[i].count('"') % 2:
                    i += 1
                i += 1
                if option == "--mountpoint":
                    mountpoint = None
                continue
            if option in _MOUNT_VALUE_OPTIONS and not eq:
                if i >= len(tokens):
                    break
                value = tokens[i]
                i += 1
            if option == "--mountpoint":
                mountpoint = value
                run = after = 0
                in_run, image_first = True, image is not None
            continue
        # A positional, or a piece of a value `ps` split at its spaces: a
        # piece that starts with "-" names no option, and argparse would
        # have refused it as the image.
        if image is None and not tok.startswith("-"):
            image = tok
        if mountpoint is not None:
            after += 1
            run += in_run
        i += 1

    # `ps` joins a Unix process's arguments with spaces and no quotes, so a
    # mountpoint with a space arrives split, its rest as positionals after
    # it. `mount` takes one positional, the image. With the image read
    # first, a positional after the mountpoint and before the next value
    # option is its rest (a split --volname value comes after its own
    # option). With the image after it, more than one positional is
    # ambiguous. Part of a path is worse than none.
    if mountpoint is not None and (run if image_first else after > 1):
        mountpoint = None
    return _strip_matched_quotes(image), _strip_matched_quotes(mountpoint)


def _find_amifuse_mounts_unix():
    """Discover amifuse mounts on macOS/Linux using ps."""
    is_mac = sys.platform.startswith("darwin")

    # macOS: lstart gives absolute start time; Linux: etimes gives elapsed seconds
    # -ww prevents truncation of long command lines in headless environments
    if is_mac:
        ps_cmd = ["ps", "-axwwo", "pid=,ppid=,lstart=,command="]
    else:
        ps_cmd = ["ps", "-axwwo", "pid=,ppid=,etimes=,command="]

    try:
        result = subprocess.run(
            ps_cmd, check=False, capture_output=True, text=True,
        )
    except OSError:
        logger.debug("ps not available, cannot discover mounts")
        return []
    if result.returncode != 0:
        logger.debug("ps exited with code %d", result.returncode)
        return []

    current_pid = os.getpid()
    mounts = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if "amifuse" not in line or "mount" not in line:
            continue

        try:
            if is_mac:
                # Format: "  PID  PPID  DAY MON DD HH:MM:SS YYYY COMMAND..."
                # lstart is 5 tokens: e.g. "Sat Apr 19 10:30:00 2026"
                parts = line.split(None, 7)
                if len(parts) < 8:
                    continue
                pid = int(parts[0])
                ppid = int(parts[1])
                lstart_str = " ".join(parts[2:7])
                command = parts[7]
                # Parse lstart to compute uptime
                uptime = _parse_lstart_uptime(lstart_str)
            else:
                # Format: "  PID  PPID ETIMES COMMAND..."
                parts = line.split(None, 3)
                if len(parts) < 4:
                    continue
                pid = int(parts[0])
                ppid = int(parts[1])
                command = parts[3]
                try:
                    uptime = int(parts[2])
                except ValueError:
                    uptime = None
        except ValueError:
            logger.debug("Skipping unparseable ps line: %s", line)
            continue

        if pid == current_pid:
            continue

        # Must contain "mount" as a subcommand, not just in a path
        try:
            tokens = shlex.split(command)
        except ValueError:
            logger.debug("Skipping line with unparseable command: %s", line)
            continue

        if "mount" not in tokens:
            continue

        image, mountpoint = _parse_mount_tokens(tokens)

        mounts.append({
            "mountpoint": mountpoint,
            "image": image,
            "pid": pid,
            "uptime_seconds": uptime,
            "filesystem_type": None,
            "parent_pid": ppid,
        })

    return mounts


def _parse_lstart_uptime(lstart_str):
    """Parse macOS ps lstart string and return uptime in seconds."""
    import calendar

    try:
        # lstart format: "Sat Apr 19 10:30:00 2026"
        # Parse with time.strptime (locale-independent for English month/day names)
        t = time.strptime(lstart_str, "%a %b %d %H:%M:%S %Y")
        start_epoch = calendar.timegm(t)
        return max(0, int(time.time() - start_epoch))
    except (ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# CIM/PowerShell fallback for mount discovery (wmic deprecated on Win11)
# ---------------------------------------------------------------------------


def _find_amifuse_mounts_cim():
    """Discover amifuse mounts on Windows using PowerShell Get-CimInstance.

    Fallback for when wmic is unavailable (removed on newer Windows 11 builds).

    Note: The CIM filter does not restrict by session, so on multi-user systems
    this may return processes from other logged-in users.  A SessionId filter
    could be added if this becomes a practical issue.
    """
    ps_cmd = [
        "powershell", "-NoProfile", "-Command",
        'Get-CimInstance Win32_Process -Filter "Name like \'%python%\'" '
        "| Select-Object ProcessId,CommandLine,CreationDate,ParentProcessId "
        "| ConvertTo-Json",
    ]
    try:
        result = subprocess.run(
            ps_cmd,
            check=False,
            capture_output=True,
            timeout=15,
            creationflags=_CREATE_NO_WINDOW if sys.platform.startswith("win") else 0,
        )
    except subprocess.TimeoutExpired:
        logger.debug("PowerShell CIM query timed out")
        return []
    except OSError:
        logger.debug("PowerShell not available for CIM fallback")
        return []
    if result.returncode != 0:
        logger.debug("PowerShell CIM query exited with code %d", result.returncode)
        return []

    # PowerShell may emit a BOM; handle utf-8-sig
    try:
        text = result.stdout.decode("utf-8-sig")
    except (UnicodeDecodeError, AttributeError):
        text = result.stdout if isinstance(result.stdout, str) else result.stdout.decode("utf-8", errors="replace")

    import json
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.debug("Failed to parse CIM JSON output")
        return []

    # Single result comes as a dict, multiple as a list
    if isinstance(data, dict):
        data = [data]

    current_pid = os.getpid()
    mounts = []

    for entry in data:
        cmdline = entry.get("CommandLine") or ""
        if "amifuse" not in cmdline:
            continue

        try:
            pid = int(entry.get("ProcessId", 0))
        except (ValueError, TypeError):
            continue
        if pid == current_pid or pid == 0:
            continue

        try:
            tokens = shlex.split(cmdline, posix=False)
        except ValueError:
            tokens = cmdline.split()

        if "mount" not in tokens:
            continue

        image, mountpoint = _parse_mount_tokens(tokens)

        # Parse CreationDate -- CIM returns ISO 8601 or .NET datetime string
        uptime = None
        creation = entry.get("CreationDate")
        if creation and isinstance(creation, str):
            # CIM JSON may include /Date(milliseconds)/ format
            import re
            m = re.search(r"/Date\((\d+)\)/", creation)
            if m:
                start_epoch = int(m.group(1)) / 1000.0
                uptime = max(0, int(time.time() - start_epoch))

        parent_pid = None
        try:
            parent_pid = int(entry.get("ParentProcessId", 0)) or None
        except (ValueError, TypeError):
            pass

        mounts.append({
            "mountpoint": mountpoint,
            "image": image,
            "pid": pid,
            "uptime_seconds": uptime,
            "filesystem_type": None,
            "parent_pid": parent_pid,
        })

    return mounts


# ---------------------------------------------------------------------------
# Mount records
# ---------------------------------------------------------------------------
#
# `ps` shows a mount's command line, which holds no path for a default
# mountpoint, a path relative to a working directory that a daemon has left,
# and a path with spaces only in pieces. So a mount that is up records its
# absolute mountpoint in <pid>.json, and process discovery prefers that. A
# record counts only for a running `amifuse mount` process of that pid, so
# one left by a killed mount does nothing.


def _mount_record_dir() -> Optional[Path]:
    """Return this user's directory of mount records, or None on Windows.

    The Windows process discovery reads the mountpoint another way.
    """
    if not hasattr(os, "getuid"):
        return None
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / f"amifuse-{os.getuid()}"


def _is_private_dir(path: Path) -> bool:
    """Tell whether only this user can write to directory ``path``.

    The directory may be under a shared /tmp, where anyone can create it
    first, and a record decides what `amifuse unmount` may unmount.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077)


def _read_mount_records() -> dict:
    """Return the readable mount records, as {pid: record}."""
    directory = _mount_record_dir()
    if directory is None or not _is_private_dir(directory):
        return {}
    try:
        names = os.listdir(directory)
    except OSError:
        return {}
    records = {}
    for name in names:
        stem, _, suffix = name.partition(".")
        if suffix != "json" or not stem.isdigit():
            continue
        try:
            with open(directory / name, encoding="utf-8") as stream:
                record = json.load(stream)
        except (OSError, ValueError):
            continue
        if (isinstance(record, dict) and record.get("pid") == int(stem)
                and isinstance(record.get("mountpoint"), str)):
            records[int(stem)] = record
    return records


def write_mount_record(record: dict) -> None:
    """Record this process's mount, and drop the records of dead processes.

    ``record`` holds the absolute mountpoint and image, and the partition.
    A mount goes on without a record: process discovery falls back to `ps`.
    """
    directory = _mount_record_dir()
    if directory is None:
        return
    pid = os.getpid()
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if not _is_private_dir(directory):
            return
        for other in _read_mount_records():
            if not _pid_exists(other):
                (directory / f"{other}.json").unlink(missing_ok=True)
        temp = directory / f"{pid}.tmp"
        with open(temp, "w", encoding="utf-8") as stream:
            json.dump(dict(record, pid=pid, created_at=time.time()), stream)
        os.replace(temp, directory / f"{pid}.json")
    except OSError as exc:
        logger.debug("Could not write a mount record: %s", exc)


def remove_mount_record() -> None:
    """Remove this process's mount record, if it has one."""
    directory = _mount_record_dir()
    if directory is None:
        return
    try:
        (directory / f"{os.getpid()}.json").unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not remove a mount record: %s", exc)


def _apply_mount_records(mounts) -> None:
    """Replace what `ps` showed of each process with its mount record."""
    records = _read_mount_records()
    for mount in mounts:
        record = records.get(mount["pid"])
        if record is not None:
            created = record.get("created_at")
            uptime = mount.get("uptime_seconds")
            # ps rounds process start times to seconds. Allow two seconds
            # of rounding, but never apply a previous owner's record.
            if not isinstance(created, (int, float)):
                continue
            if uptime is not None and not created >= time.time() - uptime - 2:
                continue
            mount["mountpoint"] = record["mountpoint"]
            mount["image"] = record.get("image") or mount.get("image")


# ---------------------------------------------------------------------------
# fusepy child deduplication
# ---------------------------------------------------------------------------


def _deduplicate_fusepy_children(mounts: list) -> list:
    """Collapse a mount's process chain to the process that serves it.

    One mount can appear as a parent and child with the same command line:
    a Windows venv's python.exe/pythonw.exe is a launcher that runs the base
    interpreter as a child, and a daemonizing FUSE mount forks. Both show up
    in process scanning, causing duplicate tray entries (e.g., "D:" and "?").
    Keep the innermost process: it owns the mount and, on Windows, the
    unmount events named after its PID.
    """
    parent_pids = {m.get("parent_pid") for m in mounts}
    return [m for m in mounts if m["pid"] not in parent_pids]


# ---------------------------------------------------------------------------
# Process killing
# ---------------------------------------------------------------------------


# How old a temporary file must be before a sweep may remove it, in seconds.
# Far longer than it takes to load an extracted driver.
_STALE_TEMP_AGE = 3600


def _left_by_ended_run(pid: int, mtime: float) -> bool:
    """Tell if a temporary file named for PID, last changed at mtime, is stale.

    It is stale only if that process is gone and the file is older than
    _STALE_TEMP_AGE. The age covers a daemon mount whose parent, the process
    named, is killed before the child it forked has loaded the driver. A PID
    in use again only keeps a file for longer.
    """
    return time.time() - mtime > _STALE_TEMP_AGE and _temp_owner_gone(pid)


def _temp_owner_gone(pid: int) -> bool:
    """Require evidence that an owner exited before deleting its files."""
    if not sys.platform.startswith("win"):
        return not _pid_exists(pid)
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                      wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            kernel.CloseHandle(handle)
            return False
        # Access denied and other failures do not prove the process exited.
        return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER
    except Exception:
        return False


def _pid_exists(pid: int) -> bool:
    """Check if a process with the given PID exists."""
    if sys.platform.startswith("win"):
        # os.kill(pid, 0) is unreliable on Windows -- it raises OSError with
        # errno EINVAL (WinError 87) for processes we didn't spawn, making
        # live processes look dead.  Use OpenProcess instead.
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _find_mount_owner_pids(mountpoint: Path) -> List[int]:
    """Find PIDs of amifuse processes that own the given mountpoint.

    Uses find_amifuse_mounts() for process discovery, then filters to those
    matching the given mountpoint.
    """
    try:
        all_mounts = find_amifuse_mounts()
    except OSError:
        return []

    if sys.platform.startswith("win"):
        import ntpath

        def key(path):
            drive = windows_drive_mountpoint(path)
            if drive:
                return drive.lower() + "\\"
            path = ntpath.normpath(str(path))
            # AmiFUSE uses E: as a drive mountpoint, not as Windows' current
            # working directory on E. Match E:\ and case variants lexically:
            # resolving through WinFsp can produce a different device path.
            return ntpath.normcase(ntpath.abspath(path))

        target = key(mountpoint)
        return [mount["pid"] for mount in all_mounts
                if mount.get("mountpoint") is not None
                and key(mount["mountpoint"]) == target]

    raw_mountpoint = str(mountpoint)
    abs_mountpoint = str(mountpoint.resolve(strict=False))
    pids = []
    for mount in all_mounts:
        mp = mount.get("mountpoint")
        if mp is None:
            continue
        if mp == raw_mountpoint or mp == abs_mountpoint:
            pids.append(mount["pid"])
            continue
        try:
            resolved = str(Path(mp).expanduser().resolve(strict=False))
        except OSError:
            continue
        if resolved == abs_mountpoint:
            pids.append(mount["pid"])

    return pids


def _verify_amifuse_process(pid: int) -> bool:
    """Verify that the given PID is still a python/amifuse process.

    Guards against PID reuse: between discovery and kill, the OS may have
    recycled the PID for an unrelated process.  Returns True if the process
    executable looks like python or amifuse, False otherwise.

    On non-Windows or if verification cannot be performed, returns True
    (optimistic -- better to attempt kill than silently skip).
    """
    if not sys.platform.startswith("win"):
        return True
    try:
        import ctypes
        import ctypes.wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
        )
        if not handle:
            # Cannot open -- process is gone or inaccessible
            return False

        try:
            # QueryFullProcessImageNameW to get the executable path
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.wintypes.DWORD(1024)
            success = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size),
            )
            if not success:
                # Can't query name -- optimistically assume it's ours
                return True
            exe_path = buf.value.lower()
            return "python" in exe_path or "amifuse" in exe_path
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        # Any failure in verification -- be optimistic
        return True


def kill_pids(pids: List[int], timeout: float = 10.0) -> List[int]:
    """Kill a list of PIDs with graceful-then-force strategy.

    On Windows: uses taskkill /PID (graceful, sends WM_CLOSE), then
    taskkill /F /PID (force).  CTRL_BREAK_EVENT is not used because it is
    ineffective for detached processes launched with CREATE_NEW_PROCESS_GROUP.
    On Unix: sends SIGTERM (graceful), then SIGKILL (force).

    Args:
        pids: Process IDs to kill.
        timeout: Seconds to wait for each graceful shutdown before force-kill.

    Returns:
        List of PIDs that were successfully killed.
    """
    if not pids:
        return []

    killed = []
    is_win = sys.platform.startswith("win")

    # Phase 1: graceful signal
    remaining = []
    for pid in pids:
        # Guard against PID reuse -- verify the process is still ours
        if not _verify_amifuse_process(pid):
            logger.debug("PID %d is no longer an amifuse process (reused); skipping", pid)
            killed.append(pid)
            continue

        try:
            if is_win:
                # taskkill without /F sends WM_CLOSE -- works for detached
                # and GUI processes unlike CTRL_BREAK_EVENT.
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid)],
                    check=False,
                    capture_output=True,
                    creationflags=_CREATE_NO_WINDOW,
                )
                if result.returncode == 0:
                    remaining.append(pid)
                else:
                    # taskkill failed -- process may already be dead
                    if not _pid_exists(pid):
                        killed.append(pid)
                    else:
                        # Still alive but taskkill rejected it; force-kill later
                        remaining.append(pid)
            else:
                os.kill(pid, signal.SIGTERM)
                remaining.append(pid)
        except ProcessLookupError:
            # Already dead -- count as killed
            killed.append(pid)
        except OSError:
            if is_win:
                # Unexpected OS error; still try force-kill in Phase 3
                remaining.append(pid)
            else:
                # On Unix, OSError from os.kill usually means the process
                # is gone or inaccessible.
                killed.append(pid)

    # Phase 2: wait for graceful shutdown
    deadline = time.time() + timeout
    while remaining and time.time() < deadline:
        still_alive = []
        for pid in remaining:
            if _pid_exists(pid):
                still_alive.append(pid)
            else:
                killed.append(pid)
        if not still_alive:
            return killed
        remaining = still_alive
        time.sleep(0.1)

    # Phase 3: force kill
    for pid in remaining:
        try:
            if is_win:
                result = subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    check=False,
                    capture_output=True,
                    creationflags=_CREATE_NO_WINDOW,
                )
                if result.returncode == 0:
                    killed.append(pid)
            else:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
        except (ProcessLookupError, OSError):
            # Process already dead = success
            killed.append(pid)

    return killed


def stop_mount_processes(pids: List[int], timeout: float = 30.0) -> List[int]:
    """Unmount cooperatively on Windows; never force-kill after a timeout."""
    if sys.platform.startswith("win"):
        from .windows_unmount import request_unmount_many
        return request_unmount_many(pids, timeout=timeout)
    return kill_pids(pids, timeout=timeout)


def kill_mount_owner_processes(mountpoint: Path) -> List[int]:
    """Kill all amifuse processes that own the given mountpoint.

    Returns list of PIDs that were targeted (whether or not each one died).
    """
    pids = _find_mount_owner_pids(mountpoint)
    if not pids:
        return []
    kill_pids(pids, timeout=1.0)
    return pids


def _find_amifuse_mounts_windows():
    """Discover amifuse mounts on Windows using wmic.

    Falls back to PowerShell Get-CimInstance when wmic is missing or fails
    (wmic is deprecated and removed on newer Windows 11 builds).
    """
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             "name like '%python%'",
             "get", "ProcessId,CommandLine,CreationDate,ParentProcessId",
             "/FORMAT:LIST"],
            check=False,
            capture_output=True,
            text=True,
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError:
        # wmic not available (e.g., removed on newer Windows 11 builds)
        logger.debug("wmic not available, cannot discover mounts")
        return _find_amifuse_mounts_cim()
    if result.returncode != 0:
        # wmic may exist as a broken/deprecated stub -- try CIM instead of
        # silently reporting no mounts.
        logger.debug("wmic exited with code %d", result.returncode)
        return _find_amifuse_mounts_cim()

    current_pid = os.getpid()
    mounts = []

    # wmic /FORMAT:LIST outputs key=value pairs separated by blank lines
    current_cmdline = None
    current_pid_val = None
    current_creation = None
    current_parent_pid = None

    def _process_record():
        if current_cmdline is None or current_pid_val is None:
            return
        if current_pid_val == current_pid:
            return
        if "amifuse" not in current_cmdline:
            return

        try:
            tokens = shlex.split(current_cmdline, posix=False)
        except ValueError:
            tokens = current_cmdline.split()

        if "mount" not in tokens:
            return

        image, mountpoint = _parse_mount_tokens(tokens)

        uptime = _parse_wmic_creation_date_uptime(current_creation)

        mounts.append({
            "mountpoint": mountpoint,
            "image": image,
            "pid": current_pid_val,
            "uptime_seconds": uptime,
            "filesystem_type": None,
            "parent_pid": current_parent_pid,
        })

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            # wmic uses \r\r\n which creates spurious empty lines between
            # fields within a record.  Only finalise when we have a complete
            # record (both CommandLine and ProcessId collected).
            if current_cmdline is not None and current_pid_val is not None:
                _process_record()
                current_cmdline = None
                current_pid_val = None
                current_creation = None
                current_parent_pid = None
            continue
        if line.startswith("CommandLine="):
            current_cmdline = line[len("CommandLine="):]
        elif line.startswith("ProcessId="):
            try:
                current_pid_val = int(line[len("ProcessId="):])
            except ValueError:
                current_pid_val = None
        elif line.startswith("CreationDate="):
            current_creation = line[len("CreationDate="):]
        elif line.startswith("ParentProcessId="):
            try:
                current_parent_pid = int(line[len("ParentProcessId="):])
            except ValueError:
                current_parent_pid = None

    # Handle last record if no trailing blank line
    _process_record()

    return mounts


# ---------------------------------------------------------------------------
# Mountpoint enrichment for auto-assigned mounts
# ---------------------------------------------------------------------------


def _enrich_null_mountpoints(mounts):
    """Post-process mounts to fill in mountpoint=None entries.

    When users mount without --mountpoint, the CLI args don't contain
    the mountpoint, so process scanning yields None.  We heuristically
    detect the actual mountpoint via OS-specific APIs.

    Limitation: when multiple auto-assigned mounts exist, the pairing
    of process-to-mountpoint is non-deterministic (we lack PID-to-handle
    mapping without admin privileges).  For the common single-mount case,
    the 1:1 match is reliable.
    """
    null_mounts = [m for m in mounts if m.get("mountpoint") is None]
    if not null_mounts:
        return

    if sys.platform.startswith("win"):
        _enrich_mountpoints_windows(null_mounts, mounts)
    elif sys.platform.startswith("darwin"):
        _enrich_mountpoints_macos(null_mounts, mounts)
    # Linux requires explicit --mountpoint; no enrichment needed.


def _enrich_mountpoints_windows(null_mounts, all_mounts):
    """Scan drive letters for WinFSP/AmiFUSE volumes and assign to null mounts.

    Uses GetVolumeInformationW to check the filesystem name on each drive.
    Drives with fs name starting with 'FUSE-AmiFUSE' are amifuse mounts.
    """
    try:
        import ctypes
    except ImportError:
        return

    # Collect mountpoints already known (claimed by explicit --mountpoint)
    claimed = {windows_drive_mountpoint(m["mountpoint"]) or m["mountpoint"]
               for m in all_mounts if m.get("mountpoint")}

    amifuse_drives = []
    fs_name_buf = ctypes.create_unicode_buffer(256)
    vol_name_buf = ctypes.create_unicode_buffer(256)

    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        drive = f"{letter}:\\"
        try:
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                drive,
                vol_name_buf, 256,
                None, None, None,
                fs_name_buf, 256,
            )
        except Exception:
            continue
        if not ok:
            continue
        fs_name = fs_name_buf.value
        if fs_name.startswith("FUSE-AmiFUSE"):
            drive_mp = f"{letter}:"
            if drive_mp not in claimed:
                amifuse_drives.append(drive_mp)

    # Match null-mountpoint processes to discovered drives.
    # Non-deterministic for multiple mounts -- best-effort pairing.
    for mount, drive in zip(null_mounts, amifuse_drives):
        mount["mountpoint"] = drive


# mount_fuse() sets the FUSE fsname to "amifuse:<volume name>", which macOS
# lists as the mount's source.
_AMIFUSE_MOUNT_SOURCE_PREFIX = "amifuse:"


def _macos_mount_table() -> List[str]:
    """Return macOS `mount` entries as "<source> on <mountpoint>" strings.

    Each line of `mount` reads "<source> on <mountpoint> (<options>)"; the
    options are dropped. A hung mount can make `mount` wait, so it gets a
    timeout. Returns an empty list when the table can't be read.
    """
    try:
        # Decode paths as os.fsdecode does, so they compare with os paths.
        result = subprocess.run(
            ["/sbin/mount"], capture_output=True, encoding="utf-8",
            errors="surrogateescape", timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    entries = []
    for line in result.stdout.splitlines():
        entry, sep, _options = line.rpartition(" (")
        if sep:
            entries.append(entry)
    return entries


def _mount_source(mountpoint, mount_table) -> Optional[str]:
    """Return the source `mount` lists for ``mountpoint``, or None.

    None also when entries for the path disagree: a stack of different
    mounts, or another mountpoint whose own path ends in " on <mountpoint>".
    """
    raw = os.fspath(mountpoint)
    # `mount` lists real paths: resolve symlinks (/var -> /private/var) and
    # relative paths, and keep the lexical form for the unresolved spelling.
    targets = {posixpath.normpath(raw), os.path.realpath(raw)}
    sources = set()
    for entry in mount_table:
        for target in targets:
            suffix = " on " + target
            if entry.endswith(suffix):
                sources.add(entry[:-len(suffix)])
    return sources.pop() if len(sources) == 1 else None


def _has_amifuse_mount_source(mountpoint, mount_table) -> bool:
    """Tell whether `mount` lists ``mountpoint`` with AmiFUSE's source.

    macFUSE lists the fsname as the source. FUSE-T doesn't (see
    ``_is_fuse_t_source``), so its mounts never match here.
    """
    source = _mount_source(mountpoint, mount_table)
    return source is not None and source.startswith(_AMIFUSE_MOUNT_SOURCE_PREFIX)


def _is_fuse_t_source(source: str) -> bool:
    """Tell whether a mount source is FUSE-T's.

    FUSE-T serves a mount over NFS (source "fuse-t:/<volume name>") or SMB
    ("//Guest@fuse-t._smb._tcp.local/<share>") and never passes the fsname
    on, so its source doesn't say which program the mount belongs to.
    """
    return source.startswith("fuse-t:/") or "@fuse-t._smb._tcp.local/" in source


def is_amifuse_mount_macos(mountpoint, mount_table=None) -> bool:
    """Tell whether ``mountpoint`` is an AmiFUSE mount on macOS.

    It is when `mount` lists it with the source AmiFUSE sets (macFUSE). A
    FUSE-T mount, whose source doesn't name AmiFUSE, is when a running
    AmiFUSE process recorded it or was given its path, or when it is dead and
    nothing serves it any more. Anything else (another disk or share, or a
    path the table doesn't list) is not, so no caller mistakes the user's own
    volume for an AmiFUSE mount.

    Args:
        mountpoint: The path to check.
        mount_table: Entries from ``_macos_mount_table()``; read afresh when
            None.
    """
    if mount_table is None:
        mount_table = _macos_mount_table()
    source = _mount_source(mountpoint, mount_table)
    if source is None:
        return False
    if source.startswith(_AMIFUSE_MOUNT_SOURCE_PREFIX):
        return True
    if not _is_fuse_t_source(source):
        return False
    path = Path(os.fspath(mountpoint))
    # Process discovery pairs a mount with no mount record and no
    # --mountpoint only with a volume the table names as AmiFUSE's, so this
    # finds just the processes that recorded this path or were given it on
    # their command lines. The caller's parent is left out: a wrapper such
    # as `sudo` or `uv run` repeats the command line of the `amifuse mount`
    # checking its own path.
    parent = os.getppid()
    if any(pid != parent for pid in _find_mount_owner_pids(path)):
        return True
    # A dead FUSE-T mount: unmounting it can't disturb a working filesystem.
    return is_stale_mountpoint(path)


def _enrich_mountpoints_macos(null_mounts, all_mounts):
    """Scan /Volumes/ for amifuse mount points and assign to null mounts.

    Takes only the entries in /Volumes/ that `mount` lists as AmiFUSE mounts,
    so on FUSE-T a mount without a mount record stays unpaired.
    """
    volumes_dir = "/Volumes"
    if not os.path.isdir(volumes_dir):
        return

    claimed = {m["mountpoint"] for m in all_mounts if m.get("mountpoint")}

    amifuse_volumes = []
    try:
        entries = os.listdir(volumes_dir)
    except OSError:
        return

    # Only AmiFUSE's own volumes: another disk in /Volumes (a USB stick, the
    # system volume) must never be paired with an AmiFUSE process.
    mount_table = _macos_mount_table()
    for entry in sorted(entries):
        vol_path = volumes_dir + "/" + entry
        if vol_path in claimed:
            continue
        try:
            # A dead AmiFUSE mount keeps its entry in the table, but it
            # isn't a mount any more and has no process to pair with.
            if (os.path.ismount(vol_path)
                    and _has_amifuse_mount_source(vol_path, mount_table)):
                amifuse_volumes.append(vol_path)
        except OSError:
            continue

    # Match null-mountpoint processes to discovered volumes.
    # Non-deterministic for multiple mounts -- best-effort pairing.
    for mount, vol in zip(null_mounts, amifuse_volumes):
        mount["mountpoint"] = vol


def _parse_wmic_creation_date_uptime(creation_str):
    """Parse wmic CreationDate (yyyymmddHHMMSS.ffffff+ZZZ) to uptime seconds."""
    if not creation_str:
        return None
    try:
        # Format: 20260419103000.123456+060
        # Take first 14 chars: yyyymmddHHMMSS
        dt_str = creation_str[:14]
        t = time.strptime(dt_str, "%Y%m%d%H%M%S")
        import calendar
        start_epoch = calendar.timegm(t)
        # Adjust for timezone offset in wmic output (+/- minutes)
        if len(creation_str) > 21:
            tz_str = creation_str[21:]
            try:
                tz_minutes = int(tz_str)
                start_epoch -= tz_minutes * 60
            except ValueError:
                pass
        return max(0, int(time.time() - start_epoch))
    except (ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# FUSE backend detection
# ---------------------------------------------------------------------------


def detect_fuse_backend() -> dict:
    """Detect the installed FUSE backend and return info about it.

    This is a doctor-specific query function. Does NOT raise.

    Returns:
        {"installed": bool, "name": str, "version": Optional[str]}
    """
    if sys.platform.startswith("win"):
        install_dir = _get_winfsp_install_dir()
        if install_dir is None:
            return {"installed": False, "name": "WinFSP", "version": None}
        version = None
        # Optionally try to read version from the DLL
        dll_path = os.path.join(install_dir, "bin", "winfsp-x64.dll")
        if not os.path.isfile(dll_path):
            dll_path = os.path.join(install_dir, "bin", "winfsp-x86.dll")
        if os.path.isfile(dll_path):
            try:
                version = _get_file_version_win(dll_path)
            except Exception:
                pass
        return {"installed": True, "name": "WinFSP", "version": version}

    if sys.platform.startswith("darwin"):
        # Check macFUSE
        if os.path.isdir("/Library/Filesystems/macfuse.fs/"):
            version = _read_macfuse_version()
            return {"installed": True, "name": "macFUSE", "version": version}
        # Check fuse-t
        for libpath in ("/usr/local/lib/libfuse-t.dylib", "/opt/homebrew/lib/libfuse-t.dylib"):
            if os.path.isfile(libpath):
                return {"installed": True, "name": "fuse-t", "version": None}
        # Check generic mount_fusefs
        if shutil.which("mount_fusefs"):
            return {"installed": True, "name": "FUSE (generic)", "version": None}
        return {"installed": False, "name": "macFUSE", "version": None}

    # Linux
    fm = shutil.which("fusermount3") or shutil.which("fusermount")
    if fm:
        name = "FUSE3" if "fusermount3" in fm else "FUSE"
        return {"installed": True, "name": name, "version": None}
    # Check for libfuse
    for lib_dir in ("/usr/lib", "/usr/lib64", "/usr/local/lib"):
        for lib_name in ("libfuse3.so", "libfuse.so"):
            if os.path.isfile(os.path.join(lib_dir, lib_name)):
                name = "FUSE3" if "fuse3" in lib_name else "FUSE"
                return {"installed": True, "name": name, "version": None}
    return {"installed": False, "name": "FUSE", "version": None}


def _read_macfuse_version() -> Optional[str]:
    """Try to read macFUSE version from its plist."""
    plist_path = "/Library/Filesystems/macfuse.fs/Contents/Info.plist"
    try:
        import plistlib
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
        return plist.get("CFBundleVersion")
    except Exception:
        return None


def _get_file_version_win(filepath: str) -> Optional[str]:
    """Read file version from a Windows DLL/EXE using GetFileVersionInfoW."""
    try:
        import ctypes
        from ctypes import wintypes

        version_dll = ctypes.windll.version
        size = version_dll.GetFileVersionInfoSizeW(filepath, None)
        if not size:
            return None

        data = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(filepath, 0, size, data):
            return None

        buf = ctypes.c_void_p()
        buf_len = wintypes.UINT()
        if not version_dll.VerQueryValueW(
            data, "\\", ctypes.byref(buf), ctypes.byref(buf_len)
        ):
            return None

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        info = ctypes.cast(buf, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        ms = info.dwFileVersionMS
        ls = info.dwFileVersionLS
        return f"{(ms >> 16) & 0xFFFF}.{ms & 0xFFFF}.{(ls >> 16) & 0xFFFF}.{ls & 0xFFFF}"
    except Exception:
        return None


def notify_shell_drive_change(drive_letter: str, added: bool) -> None:
    """Notify Windows Explorer that a drive was added or removed.

    Uses SHChangeNotify to trigger immediate Explorer sidebar refresh.
    No-op on non-Windows platforms.

    Args:
        drive_letter: Drive letter with colon (e.g., "D:" or "D:\\")
        added: True for mount (SHCNE_DRIVEADD), False for unmount (SHCNE_DRIVEREMOVED)
    """
    if not sys.platform.startswith("win"):
        return

    import ctypes

    # SHChangeNotify constants
    SHCNE_DRIVEADD = 0x00000100
    SHCNE_DRIVEREMOVED = 0x00000080
    SHCNF_PATH = 0x0005

    event = SHCNE_DRIVEADD if added else SHCNE_DRIVEREMOVED
    # SHChangeNotify expects a null-terminated path string
    # Normalize to "X:\" format
    path = (windows_drive_mountpoint(drive_letter) or drive_letter.rstrip("\\")) + "\\"

    try:
        ctypes.windll.shell32.SHChangeNotify(event, SHCNF_PATH, ctypes.c_wchar_p(path), None)
    except Exception:
        pass  # Best-effort; don't crash if shell notification fails
