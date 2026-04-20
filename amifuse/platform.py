"""
Platform abstraction layer for amifuse.

This module provides platform-specific functionality with a unified interface,
including mount options, default mountpoints, unmount commands, and icon handling.

Platform-specific implementations:
- macOS/Darwin: icon_darwin.py
- Linux: (future) icon_linux.py
- Windows: (future) icon_windows.py
"""

import errno
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .icon_darwin import DarwinIconHandler


def get_default_mountpoint(volname: str) -> Optional[Path]:
    """Get the default mountpoint for the current platform.

    Args:
        volname: Volume name to use in the mountpoint path

    Returns:
        Default mountpoint path, or None if platform requires explicit mountpoint
    """
    if sys.platform.startswith("darwin"):
        return Path(f"/Volumes/{volname}")
    elif sys.platform.startswith("win"):
        # Find first available drive letter (skip A/B floppy and C system)
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            drive = f"{letter}:"
            if not os.path.exists(drive):
                return Path(drive)
        return None  # No available drive letter
    else:
        # Linux requires explicit mountpoint
        return None


def should_auto_create_mountpoint(mountpoint: Path) -> bool:
    """Check if the mountpoint should be auto-created by the FUSE library.

    Args:
        mountpoint: The mountpoint path

    Returns:
        True if FUSE will create it automatically, False if we need to create it
    """
    if sys.platform.startswith("darwin"):
        # macFUSE will create mount points in /Volumes automatically
        return str(mountpoint).startswith("/Volumes/")
    if sys.platform.startswith("win"):
        # WinFSP handles drive letter mountpoints; don't mkdir them.
        # Directory mountpoints (e.g. C:\mnt\amiga) still need mkdir.
        mp_str = str(mountpoint)
        return len(mp_str) == 2 and mp_str[1] == ":"
    return False


def get_unmount_command(mountpoint: Path) -> List[str]:
    """Get the command to unmount a FUSE filesystem.

    Args:
        mountpoint: The mountpoint to unmount

    Returns:
        Command as a list of strings suitable for subprocess.
        Returns an empty list [] on platforms where no unmount command is
        available (e.g. Windows foreground mounts). Callers must handle
        the empty-list case -- typically by skipping subprocess invocation
        and providing a user hint instead.

        Note: A future `amifuse unmount` command will need a different
        strategy on Windows (process termination or signal) since no CLI
        unmount tool exists for foreground WinFSP mounts.
    """
    if sys.platform.startswith("darwin"):
        return ["umount", "-f", str(mountpoint)]
    if sys.platform.startswith("win"):
        return _get_windows_unmount_command(mountpoint)
    # Linux - prefer fusermount if available
    if shutil.which("fusermount"):
        return ["fusermount", "-u", str(mountpoint)]
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

    WinFSP drive-letter mounts (e.g. ``Z:``) can be detached with
    ``net use Z: /delete``.  Non-drive-letter mounts have no standalone
    unmount CLI -- callers handle the empty-list case by falling back to
    process termination.
    """
    mp_str = str(mountpoint)
    if len(mp_str) == 2 and mp_str[1] == ":":
        return ["net", "use", mp_str, "/delete"]
    return []


def mount_runs_in_foreground_by_default() -> bool:
    """Return the safest default mount mode for the current platform."""
    # WinFSP foreground mounts do not expose a standalone unmount command.
    # Keep the process attached by default until we have PID tracking there.
    return sys.platform.startswith("win")


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
    if sys.platform.startswith("win") and len(mp_str) == 2 and mp_str[1] == ":":
        # Drive letter mountpoint -- check if drive exists (i.e., is assigned)
        if os.path.exists(mp_str + "\\"):
            return (
                f"Drive {mp_str} is already in use; choose a different drive letter "
                f"or free it first."
            )
    else:
        try:
            path_exists = os.path.exists(mp_str)
        except OSError as exc:
            if _is_stale_mount_os_error(exc):
                return _format_stale_mountpoint_error(mountpoint)
            return f"Mountpoint {mountpoint} is not accessible: {exc.strerror or exc}."
        if not path_exists:
            if not _is_stale_mountpoint(mountpoint):
                return None
            return _format_stale_mountpoint_error(mountpoint)
        try:
            if os.path.ismount(mp_str):
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
                return _format_stale_mountpoint_error(mountpoint)
            return f"Mountpoint {mountpoint} is not accessible: {exc.strerror or exc}."
    return None


def _is_stale_mount_os_error(exc: OSError) -> bool:
    return exc.errno in (errno.EIO, errno.ENOTCONN)


def _is_stale_mountpoint(mountpoint: Path) -> bool:
    try:
        os.lstat(str(mountpoint))
    except FileNotFoundError:
        return False
    except OSError as exc:
        if _is_stale_mount_os_error(exc):
            return True
    return False


def _format_stale_mountpoint_error(mountpoint: Path) -> str:
    if get_unmount_command(mountpoint):
        return (
            f"Mountpoint {mountpoint} looks like a stale or broken mount; "
            f"unmount it first (e.g. amifuse unmount {mountpoint})."
        )
    return (
        f"Mountpoint {mountpoint} looks like a stale or broken mount. "
        f"Stop the amifuse process to unmount (Ctrl+C)."
    )


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
        return _find_amifuse_mounts_windows()
    return _find_amifuse_mounts_unix()


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
    i = mount_idx + 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--mountpoint" and i + 1 < len(tokens):
            mountpoint = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("-"):
            # Skip flags; if flag takes a value, skip next token too
            # Known value-taking flags in mount subcommand
            value_flags = {"--driver", "--partition", "--block-size", "--volname"}
            if tok in value_flags and i + 1 < len(tokens):
                i += 2
            else:
                i += 1
            continue
        if image is None:
            image = tok
        i += 1

    return image, mountpoint


def _find_amifuse_mounts_unix():
    """Discover amifuse mounts on macOS/Linux using ps."""
    is_mac = sys.platform.startswith("darwin")

    # macOS: lstart gives absolute start time; Linux: etimes gives elapsed seconds
    if is_mac:
        ps_cmd = ["ps", "-axo", "pid=,lstart=,command="]
    else:
        ps_cmd = ["ps", "-axo", "pid=,etimes=,command="]

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
                # Format: "  PID  DAY MON DD HH:MM:SS YYYY COMMAND..."
                # lstart is 5 tokens: e.g. "Sat Apr 19 10:30:00 2026"
                parts = line.split(None, 6)
                if len(parts) < 7:
                    continue
                pid = int(parts[0])
                lstart_str = " ".join(parts[1:6])
                command = parts[6]
                # Parse lstart to compute uptime
                uptime = _parse_lstart_uptime(lstart_str)
            else:
                # Format: "  PID ETIMES COMMAND..."
                parts = line.split(None, 2)
                if len(parts) < 3:
                    continue
                pid = int(parts[0])
                command = parts[2]
                try:
                    uptime = int(parts[1])
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


def _find_amifuse_mounts_windows():
    """Discover amifuse mounts on Windows using wmic.

    Note: wmic is deprecated on Windows 11. Future fallback: use
    Get-CimInstance Win32_Process via PowerShell if wmic is unavailable.
    """
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             "name like '%python%'",
             "get", "ProcessId,CommandLine,CreationDate",
             "/FORMAT:LIST"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        # wmic not available (e.g., removed on newer Windows 11 builds)
        logger.debug("wmic not available, cannot discover mounts")
        return []
    if result.returncode != 0:
        logger.debug("wmic exited with code %d", result.returncode)
        return []

    current_pid = os.getpid()
    mounts = []

    # wmic /FORMAT:LIST outputs key=value pairs separated by blank lines
    current_cmdline = None
    current_pid_val = None
    current_creation = None

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

    # Handle last record if no trailing blank line
    _process_record()

    return mounts


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
