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

# Prevent subprocess calls from flashing a console window when invoked from
# pythonw.exe (e.g. tray/launcher).
_CREATE_NO_WINDOW = 0x08000000


# ---------------------------------------------------------------------------
# Driver resolution
# ---------------------------------------------------------------------------

# DOS type tag string to handler binary name mapping.
# DOS0-DOS7 are all handled by FastFileSystem (OFS = DOS0, FFS = DOS1, etc.)
_DOSTYPE_DRIVER_MAP = {
    "DOS0": "FastFileSystem",
    "DOS1": "FastFileSystem",
    "DOS2": "FastFileSystem",
    "DOS3": "FastFileSystem",
    "DOS4": "FastFileSystem",
    "DOS5": "FastFileSystem",
    "DOS6": "FastFileSystem",
    "DOS7": "FastFileSystem",
}


def get_driver_search_dirs() -> List[Path]:
    """Return list of directories to search for handler binaries.

    Search order:
    1. Bundled with package (always available after pip install)
    2. User-installed overrides (platform-specific data directories)
    3. Dev fallback: AmiFUSE-testing sibling repo
    """
    dirs: List[Path] = []

    # 1. Bundled with package (always available)
    bundled = Path(__file__).parent / "drivers"
    if bundled.is_dir():
        dirs.append(bundled)

    # 2. User-installed overrides
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            dirs.append(Path(local) / "amifuse" / "drivers")
    else:
        dirs.append(Path.home() / ".local" / "share" / "amifuse" / "drivers")

    # 3. Dev fallback: AmiFUSE-testing sibling
    testing_drivers = Path(__file__).parent.parent.parent / "AmiFUSE-testing" / "drivers"
    if testing_drivers.is_dir():
        dirs.append(testing_drivers)

    return dirs


def find_driver_for_dostype(dos_type_str: str) -> Optional[Path]:
    """Find a driver binary for the given DOS type tag string (e.g. "DOS0").

    Args:
        dos_type_str: DOS type as a 4-char tag string (e.g. "DOS0", "DOS1").

    Returns:
        Path to the driver binary, or None if not found.
    """
    driver_name = _DOSTYPE_DRIVER_MAP.get(dos_type_str)
    if not driver_name:
        return None
    for search_dir in get_driver_search_dirs():
        candidate = search_dir / driver_name
        if candidate.is_file():
            return candidate
    return None


def get_primary_driver_dir() -> Path:
    """Return the primary (preferred) directory for installing drivers."""
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            return Path(local) / "amifuse" / "drivers"
    return Path.home() / ".local" / "share" / "amifuse" / "drivers"


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
    through to process-termination, which is the reliable approach on
    Windows.
    """
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
        mounts = _find_amifuse_mounts_windows()
    else:
        mounts = _find_amifuse_mounts_unix()

    _enrich_null_mountpoints(mounts)
    return mounts


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
    claimed = {m["mountpoint"] for m in all_mounts if m.get("mountpoint")}

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


def _enrich_mountpoints_macos(null_mounts, all_mounts):
    """Scan /Volumes/ for amifuse mount points and assign to null mounts.

    Checks entries in /Volumes/ that are actual mount points.
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

    for entry in sorted(entries):
        vol_path = volumes_dir + "/" + entry
        if vol_path in claimed:
            continue
        try:
            if os.path.ismount(vol_path):
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
