"""
macOS/Darwin-specific icon handling for FUSE filesystem.

This module contains all Finder-specific logic for displaying Amiga icons
on macOS, including:
- Virtual Icon file for folder custom icons
- Virtual .VolumeIcon.icns for volume icons
- Extended attributes (com.apple.ResourceFork, com.apple.FinderInfo)
- Finder flags (kHasCustomIcon, kIsInvisible)

For Linux/Windows support, create icon_linux.py or icon_windows.py with
the same interface.
"""

from typing import List, Optional, Tuple

# Import resource fork builders from the dedicated module
from .resource_fork import build_resource_fork, build_finder_info as _build_finder_info


# Virtual file names for macOS icon support
ICON_FILE = "Icon\r"  # Folder custom icon marker
VOLUME_ICON_FILE = ".VolumeIcon.icns"  # Volume custom icon

# Finder flags
kHasCustomIcon = 0x0400  # Folder/file has custom icon
kIsInvisible = 0x4000    # File is invisible in Finder


def is_icon_file(path: str) -> bool:
    """Check if path is the virtual Icon\\r file for folder icons."""
    return path.endswith("/" + ICON_FILE) or path == ICON_FILE


def is_volume_icon_file(path: str) -> bool:
    """Check if path is the virtual .VolumeIcon.icns file."""
    return path == "/" + VOLUME_ICON_FILE


def get_icon_xattr_names() -> List[str]:
    """Get list of extended attribute names used for custom icons."""
    return ["com.apple.FinderInfo", "com.apple.ResourceFork"]


def get_hidden_xattr_names() -> List[str]:
    """Get list of extended attribute names used for hiding files."""
    return ["com.apple.FinderInfo"]


def build_finder_info(has_custom_icon: bool = False, is_invisible: bool = False) -> bytes:
    """Build a 32-byte FinderInfo structure with the specified flags.

    Args:
        has_custom_icon: Set kHasCustomIcon flag (0x0400)
        is_invisible: Set kIsInvisible flag (0x4000)

    Returns:
        32 bytes of FinderInfo data
    """
    import struct

    # Use resource_fork's builder for custom icon case
    if has_custom_icon and not is_invisible:
        return _build_finder_info(has_custom_icon=True)

    # For invisible flag or combined flags, build manually
    data = bytearray(32)
    flags = 0
    if has_custom_icon:
        flags |= kHasCustomIcon
    if is_invisible:
        flags |= kIsInvisible

    # Flags are at bytes 8-9, big-endian
    struct.pack_into(">H", data, 8, flags)
    return bytes(data)


def get_darwin_mount_options(volname: str, volicon_path: Optional[str] = None,
                              icons_enabled: bool = False) -> dict:
    """Get macOS-specific FUSE mount options.

    Args:
        volname: Volume name to display in Finder
        volicon_path: Path to volume icon file (for volicon option)
        icons_enabled: Whether icon mode is enabled

    Returns:
        Dictionary of mount options for macFUSE
    """
    options = {
        "volname": volname,  # Volume name shown in Finder
        "local": True,  # Tell macOS this is a local FS (not network)
        "noappledouble": True,  # Disable AppleDouble ._ files
    }

    # Only disable xattrs if icons mode is not enabled
    if not icons_enabled:
        options["noapplexattr"] = True

    # Use volicon mount option if we have a pre-generated icon
    if volicon_path:
        options["volicon"] = volicon_path

    return options


class DarwinIconHandler:
    """Handler for macOS-specific icon operations in FUSE filesystem.

    This class provides methods for handling virtual icon files and
    extended attributes that Finder uses for custom icons.
    """

    def __init__(self, icons_enabled: bool = False, debug: bool = False):
        self._icons_enabled = icons_enabled
        self._debug = debug

    def is_icon_file(self, path: str) -> bool:
        """Check if path is the virtual Icon\\r file."""
        return self._icons_enabled and is_icon_file(path)

    def is_volume_icon_file(self, path: str) -> bool:
        """Check if path is the virtual .VolumeIcon.icns file."""
        return self._icons_enabled and is_volume_icon_file(path)

    def is_info_file(self, path: str) -> bool:
        """Check if path is a .info file that should be hidden."""
        return path.lower().endswith(".info")

    def get_listxattr_for_path(self, path: str, has_icon: bool) -> List[str]:
        """Get list of xattrs to report for a path.

        Args:
            path: The file/folder path
            has_icon: Whether this path has a valid custom icon.
                      For Icon\\r files, this indicates whether the PARENT has a valid icon.

        Returns:
            List of xattr names
        """
        result = []

        # .info files get FinderInfo to hide them
        if self.is_info_file(path):
            result.extend(get_hidden_xattr_names())

        # Icon\r files and paths with valid icons get ResourceFork and FinderInfo
        # For Icon\r files, has_icon should reflect whether parent directory has an icon
        elif has_icon:
            result.extend(get_icon_xattr_names())

        return result

    def get_xattr_value(self, path: str, name: str, icns_data: Optional[bytes],
                        has_icon: bool, position: int = 0) -> Optional[bytes]:
        """Get the value of an extended attribute.

        Args:
            path: The file/folder path
            name: The xattr name
            icns_data: ICNS data if available (for ResourceFork)
            has_icon: Whether this path has a valid custom icon
            position: Byte offset for partial reads (FUSE may request partial data)

        Returns:
            The xattr value, or None if not applicable
        """
        # .info files: return FinderInfo with invisible flag
        if self.is_info_file(path):
            if name == "com.apple.FinderInfo":
                return build_finder_info(is_invisible=True)
            return None

        # Icon\r file or path with icon
        if self.is_icon_file(path) or has_icon:
            if name == "com.apple.ResourceFork" and icns_data:
                return build_resource_fork(icns_data, position)
            elif name == "com.apple.FinderInfo":
                return build_finder_info(has_custom_icon=True)

        return None

    def get_icon_file_stat(self, icns_size: int, uid: int, gid: int) -> dict:
        """Get stat dict for virtual Icon\\r file.

        Args:
            icns_size: Size of ICNS data (not used, file appears empty)
            uid: User ID
            gid: Group ID

        Returns:
            Stat dictionary for the virtual file
        """
        import time
        now = int(time.time())
        return {
            "st_mode": 0o100444,  # Regular file, read-only
            "st_nlink": 1,
            "st_size": 0,  # File appears empty; icon is in ResourceFork
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
            "st_uid": uid,
            "st_gid": gid,
            "st_flags": 0x8000,  # UF_HIDDEN
        }

    def get_volume_icon_stat(self, icns_size: int, uid: int, gid: int) -> dict:
        """Get stat dict for virtual .VolumeIcon.icns file.

        Args:
            icns_size: Size of ICNS data
            uid: User ID
            gid: Group ID

        Returns:
            Stat dictionary for the virtual file
        """
        import time
        now = int(time.time())
        return {
            "st_mode": 0o100444,  # Regular file, read-only
            "st_nlink": 1,
            "st_size": icns_size,
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
            "st_uid": uid,
            "st_gid": gid,
        }
