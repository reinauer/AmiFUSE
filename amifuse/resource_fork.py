"""
macOS Resource Fork builder for custom file icons.

This module builds the minimal resource fork structure needed to attach
custom icons to files on macOS via the com.apple.ResourceFork extended attribute.
"""

import struct
from typing import List, Tuple


# Resource fork constants
ICNS_RESOURCE_TYPE = b'icns'
ICNS_RESOURCE_ID = -16455  # Standard ID for custom file icons


def build_resource_fork(icns_data: bytes, position: int = 0) -> bytes:
    """Build a macOS resource fork containing an ICNS icon resource.

    The resource fork format is:
    - Resource header (256 bytes)
    - Resource data section
    - Resource map section

    Args:
        icns_data: The ICNS icon data.
        position: Byte offset for partial reads (FUSE may request partial data).

    Returns:
        The resource fork data (or portion starting at position).
    """
    # Build resource data section
    # Each resource data entry is: 4-byte length + data
    resource_data = struct.pack(">I", len(icns_data)) + icns_data

    # Calculate offsets (needed before building map since map contains copy)
    data_offset = 256  # Header is 256 bytes
    data_length = len(resource_data)
    # Map offset comes after data
    map_offset = data_offset + data_length

    # Build resource map (pass header info for the copy at start of map)
    resource_map = _build_resource_map(
        [(ICNS_RESOURCE_TYPE, ICNS_RESOURCE_ID, 0)],
        data_offset, map_offset, data_length
    )
    map_length = len(resource_map)

    # Build header (256 bytes)
    # First 16 bytes: offsets and lengths
    header = struct.pack(">IIII",
        data_offset,    # Offset to resource data
        map_offset,     # Offset to resource map
        data_length,    # Length of resource data
        map_length      # Length of resource map
    )
    # Bytes 16-255: reserved (zeros)
    header = header + b'\x00' * (256 - len(header))

    # Combine all sections
    full_fork = header + resource_data + resource_map

    # Handle partial reads
    if position > 0:
        if position >= len(full_fork):
            return b''
        return full_fork[position:]

    return full_fork


def _build_resource_map(
    resources: List[Tuple[bytes, int, int]],
    data_offset: int = 256,
    map_offset: int = 0,
    data_length: int = 0
) -> bytes:
    """Build the resource map section.

    Args:
        resources: List of (type, id, data_offset) tuples.
        data_offset: Offset to resource data section (from file header).
        map_offset: Offset to this resource map (from file header).
        data_length: Length of resource data section.

    Returns:
        The resource map bytes.
    """
    # Resource map structure:
    # - Copy of header (16 bytes) - should match file header
    # - Handle to next resource map (4 bytes) - 0
    # - File reference number (2 bytes) - 0
    # - Resource fork attributes (2 bytes) - 0
    # - Offset to type list from map start (2 bytes)
    # - Offset to name list from map start (2 bytes)
    # - Type list
    # - Reference list(s)
    # - Name list (empty for us)

    # Group resources by type
    types_dict = {}
    for res_type, res_id, res_data_offset in resources:
        if res_type not in types_dict:
            types_dict[res_type] = []
        types_dict[res_type].append((res_id, res_data_offset))

    num_types = len(types_dict)

    # Calculate offsets
    # Map header: 16 + 4 + 2 + 2 + 2 + 2 = 28 bytes
    map_header_size = 28
    # Type list: 2 bytes (count-1) + 8 bytes per type entry
    type_list_offset = map_header_size
    type_list_size = 2 + (8 * num_types)
    # Reference lists follow type list
    ref_list_offset = type_list_offset + type_list_size
    # Name list at the end (empty)
    name_list_offset = ref_list_offset

    # Build type list and reference lists
    type_list = bytearray()
    ref_lists = bytearray()

    # Type count - 1
    type_list.extend(struct.pack(">H", num_types - 1))

    current_ref_offset = 0
    for res_type, refs in types_dict.items():
        # Type entry: type (4) + count-1 (2) + ref offset (2)
        ref_offset_from_type_list = type_list_size + current_ref_offset
        type_list.extend(res_type)
        type_list.extend(struct.pack(">H", len(refs) - 1))
        type_list.extend(struct.pack(">H", ref_offset_from_type_list))

        # Reference entries for this type
        for res_id, res_data_offset in refs:
            # Reference entry (12 bytes total):
            # - Resource ID (2 bytes, signed)
            # - Name offset from name list (2 bytes, 0xFFFF = no name)
            # - Attributes + data offset (4 bytes: attr in high byte, offset in low 3)
            # - Handle to resource (4 bytes, always 0 in files)
            ref_lists.extend(struct.pack(">h", res_id))  # signed 16-bit
            ref_lists.extend(struct.pack(">H", 0xFFFF))  # No name
            ref_lists.extend(struct.pack(">I", res_data_offset & 0x00FFFFFF))
            ref_lists.extend(struct.pack(">I", 0))  # Handle placeholder

        current_ref_offset += len(refs) * 12

    # Update name list offset
    name_list_offset = type_list_offset + len(type_list) + len(ref_lists)

    # Calculate map length for header copy
    map_length = map_header_size + len(type_list) + len(ref_lists)

    # Build map header (28 bytes)
    map_header = bytearray(28)
    # Bytes 0-15: copy of file header (offsets and lengths)
    struct.pack_into(">I", map_header, 0, data_offset)
    struct.pack_into(">I", map_header, 4, map_offset)
    struct.pack_into(">I", map_header, 8, data_length)
    struct.pack_into(">I", map_header, 12, map_length)
    # Bytes 16-19: next resource map handle (0)
    # Bytes 20-21: file ref num (0)
    # Bytes 22-23: attributes (0)
    struct.pack_into(">H", map_header, 24, type_list_offset)
    struct.pack_into(">H", map_header, 26, name_list_offset)

    return bytes(map_header) + bytes(type_list) + bytes(ref_lists)


def build_finder_info(has_custom_icon: bool = True) -> bytes:
    """Build FinderInfo structure with custom icon flag.

    FinderInfo is a 32-byte structure. For our purposes, we only care about
    the flags field which contains the kHasCustomIcon bit.

    Args:
        has_custom_icon: Whether to set the custom icon flag.

    Returns:
        32-byte FinderInfo structure.
    """
    info = bytearray(32)

    if has_custom_icon:
        # FinderInfo structure for files:
        # Bytes 0-3: file type
        # Bytes 4-7: file creator
        # Bytes 8-9: Finder flags
        # Bytes 10-11: location (vertical)
        # Bytes 12-13: location (horizontal)
        # Bytes 14-15: reserved
        # Bytes 16-31: Extended FinderInfo

        # kHasCustomIcon = 0x0400 (bit 10)
        # The flag is at bytes 8-9 (big-endian)
        struct.pack_into(">H", info, 8, 0x0400)

    return bytes(info)


def get_resource_fork_size(icns_data: bytes) -> int:
    """Calculate the size of the resource fork for given ICNS data.

    Args:
        icns_data: The ICNS icon data.

    Returns:
        Total size of the resource fork in bytes.
    """
    # Header: 256 bytes
    # Resource data: 4 (length) + len(icns_data)
    # Resource map: 28 (header) + 2 (type count) + 8 (one type entry) + 12 (one ref entry) = 50
    # Note: reference entry is 12 bytes (id:2 + name_off:2 + attr+off:4 + handle:4)
    resource_data_size = 4 + len(icns_data)
    resource_map_size = 28 + 2 + 8 + 12  # = 50
    return 256 + resource_data_size + resource_map_size
