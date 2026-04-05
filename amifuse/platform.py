"""
Platform abstraction layer for amifuse.

This module provides platform-specific functionality with a unified interface,
including mount options, default mountpoints, unmount commands, and icon handling.

Platform-specific implementations:
- macOS/Darwin: icon_darwin.py
- Linux: (future) icon_linux.py
- Windows: (future) icon_windows.py
"""

import os
import shutil
import sys
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
        # WinFSP foreground mounts have no standalone unmount CLI tool.
        # The filesystem unmounts when the FUSE process exits (Ctrl+C).
        return []
    # Linux - prefer fusermount if available
    if shutil.which("fusermount"):
        return ["fusermount", "-u", str(mountpoint)]
    return ["umount", "-f", str(mountpoint)]


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

    # Windows: check for WinFSP via Registry (most reliable)
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\WinFsp",
        ) as key:
            install_dir = winreg.QueryValueEx(key, "InstallDir")[0]
            if install_dir and os.path.isdir(install_dir):
                return
    except (OSError, FileNotFoundError):
        pass  # Registry key not found -- WinFSP may not be installed

    # Fallback: check WINFSP_INSTALL_DIR env var
    winfsp_dir = os.environ.get("WINFSP_INSTALL_DIR", "")
    if winfsp_dir and os.path.isdir(winfsp_dir):
        return

    # Fallback: check default install location
    default_dir = r"C:\Program Files (x86)\WinFsp"
    if os.path.isdir(default_dir):
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
    elif os.path.exists(mp_str) and os.path.ismount(mp_str):
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
    return None


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
