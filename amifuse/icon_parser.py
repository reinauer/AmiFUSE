"""
Amiga icon file (.info) parser supporting Traditional, NewIcons, and GlowIcons formats.

This module parses Amiga .info files and converts them to RGBA pixel data
that can be used to generate macOS ICNS icons.
"""

import struct
import traceback
import zlib
from typing import Dict, List, Optional, Tuple

# DiskObject magic number
WB_DISKMAGIC = 0xE310
WB_DISKVERSION = 1

# Icon types
WBDISK = 1
WBDRAWER = 2
WBTOOL = 3
WBPROJECT = 4
WBGARBAGE = 5
WBDEVICE = 6
WBKICK = 7
WBAPPICON = 8

# Workbench 1.3 default palette (8 colors) - used when UserData=0
WB13_PALETTE = [
    (0x00, 0x55, 0xAA),  # 0: blue
    (0xFF, 0xFF, 0xFF),  # 1: white
    (0x00, 0x00, 0x22),  # 2: black
    (0xFF, 0x88, 0x00),  # 3: orange
    (0x66, 0x66, 0x66),  # 4: gray
    (0xEE, 0xEE, 0xEE),  # 5: light gray
    (0xDD, 0x77, 0x44),  # 6: brown
    (0xFF, 0xEE, 0x11),  # 7: yellow
]

# Workbench 2.0+ default palette (8 colors) - used when UserData!=0
WB20_PALETTE = [
    (0xAA, 0xAA, 0xAA),  # 0: gray
    (0x00, 0x00, 0x00),  # 1: black
    (0xFF, 0xFF, 0xFF),  # 2: white
    (0x66, 0x88, 0xBB),  # 3: blue
    (0xEE, 0x44, 0x44),  # 4: red
    (0x55, 0xDD, 0x54),  # 5: green
    (0x00, 0x44, 0xDD),  # 6: dark blue
    (0xEE, 0x99, 0x00),  # 7: orange
]

# Extended 16-color palette for 4+ plane icons (MagicWB style)
PALETTE_16 = WB20_PALETTE + [
    (0x00, 0x00, 0x00),  # 8: Black
    (0xFF, 0x00, 0x00),  # 9: Red
    (0x00, 0xFF, 0x00),  # 10: Green
    (0xFF, 0xFF, 0x00),  # 11: Yellow
    (0x00, 0x00, 0xFF),  # 12: Blue
    (0xFF, 0x00, 0xFF),  # 13: Magenta
    (0x00, 0xFF, 0xFF),  # 14: Cyan
    (0xFF, 0xFF, 0xFF),  # 15: White
]

# Legacy aliases for compatibility
WORKBENCH_PALETTE_4 = WB13_PALETTE[:4]
MAGICWB_PALETTE_8 = WB20_PALETTE


class IconParser:
    """Parser for Amiga .info icon files."""

    def __init__(self, debug: bool = False):
        self._debug = debug

    def parse(self, data: bytes) -> Optional[Dict]:
        """Parse an Amiga .info file and return icon data.

        Returns a dict with:
            - 'width': int
            - 'height': int
            - 'rgba': bytes (width * height * 4 bytes, RGBA format)
            - 'format': str ('traditional', 'newicons', or 'glowicons')

        Returns None if parsing fails.
        """
        if self._debug:
            print(f"[icon_parser] Parsing {len(data)} bytes", flush=True)
            # Show first 100 bytes in hex for debugging
            hex_preview = data[:100].hex(' ')
            print(f"[icon_parser] First 100 bytes: {hex_preview}", flush=True)

        if len(data) < 78:
            if self._debug:
                print(f"[icon_parser] Data too short: {len(data)} < 78", flush=True)
            return None

        # Check magic number
        magic = struct.unpack(">H", data[0:2])[0]
        if magic != WB_DISKMAGIC:
            if self._debug:
                print(f"[icon_parser] Bad magic: {magic:#06x} != {WB_DISKMAGIC:#06x}", flush=True)
            return None

        # Try formats in order of quality (best first)
        if self._debug:
            print(f"[icon_parser] Trying GlowIcons...", flush=True)
        result = self._try_glowicons(data)
        if result:
            if self._debug:
                print(f"[icon_parser] GlowIcons: SUCCESS {result['width']}x{result['height']}", flush=True)
            return result
        if self._debug:
            print(f"[icon_parser] GlowIcons: not found or failed", flush=True)

        if self._debug:
            print(f"[icon_parser] Trying NewIcons...", flush=True)
        result = self._try_newicons(data)
        if result:
            if self._debug:
                print(f"[icon_parser] NewIcons: SUCCESS {result['width']}x{result['height']}", flush=True)
            return result
        if self._debug:
            print(f"[icon_parser] NewIcons: not found or failed", flush=True)

        if self._debug:
            print(f"[icon_parser] Trying Traditional...", flush=True)
        result = self._try_traditional(data)
        if result:
            if self._debug:
                print(f"[icon_parser] Traditional: SUCCESS {result['width']}x{result['height']}", flush=True)
            return result

        if self._debug:
            print(f"[icon_parser] All formats failed", flush=True)
        return None

    def _try_glowicons(self, data: bytes) -> Optional[Dict]:
        """Try to parse as GlowIcons (OS 3.5+) format.

        GlowIcons append IFF FORM ICON data after the traditional icon.
        """
        # Look for FORM ICON chunk - need to search for all occurrences
        # because "FORM" might appear in ToolTypes strings (e.g., "FORMAT=0")
        search_pos = 0
        while True:
            form_pos = data.find(b"FORM", search_pos)
            if form_pos == -1:
                if self._debug:
                    print(f"[icon_parser] GlowIcons: no FORM ICON chunk found", flush=True)
                return None

            # Check if we have enough data for the header
            if len(data) < form_pos + 12:
                if self._debug:
                    print(f"[icon_parser] GlowIcons: FORM at {form_pos} too close to end", flush=True)
                return None

            # Verify this is FORM ICON
            form_type = data[form_pos + 8:form_pos + 12]
            if form_type == b"ICON":
                # Found it!
                break

            # Not ICON, keep searching after this position
            if self._debug:
                print(f"[icon_parser] GlowIcons: FORM at {form_pos} has type {form_type!r}, skipping", flush=True)
            search_pos = form_pos + 1

        if self._debug:
            form_size = struct.unpack(">I", data[form_pos + 4:form_pos + 8])[0]
            print(f"[icon_parser] GlowIcons: FORM ICON found at {form_pos}, size={form_size}", flush=True)

        try:
            return self._parse_iff_icon(data[form_pos:])
        except Exception as e:
            if self._debug:
                print(f"[icon_parser] GlowIcons parse error: {e}")
                traceback.print_exc()
            return None

    def _parse_iff_icon(self, data: bytes) -> Optional[Dict]:
        """Parse IFF FORM ICON structure."""
        if len(data) < 12:
            return None

        # FORM header
        form_size = struct.unpack(">I", data[4:8])[0]
        # Skip "FORM" + size + "ICON"
        pos = 12

        face_info = None
        images = []

        while pos < min(len(data), form_size + 8):
            if pos + 8 > len(data):
                break

            chunk_id = data[pos:pos + 4]
            chunk_size = struct.unpack(">I", data[pos + 4:pos + 8])[0]
            chunk_data = data[pos + 8:pos + 8 + chunk_size]

            if self._debug:
                print(f"[icon_parser] GlowIcons: chunk '{chunk_id.decode('latin-1', errors='replace')}' "
                      f"at pos {pos}, size {chunk_size}", flush=True)

            if chunk_id == b"FACE":
                face_info = self._parse_face_chunk(chunk_data)
                if self._debug:
                    print(f"[icon_parser] GlowIcons: FACE = {face_info}", flush=True)
            elif chunk_id == b"IMAG":
                img = self._parse_imag_chunk(chunk_data, face_info)
                if img:
                    images.append(img)
                    if self._debug:
                        print(f"[icon_parser] GlowIcons: IMAG parsed: {img['width']}x{img['height']}", flush=True)
                elif self._debug:
                    print(f"[icon_parser] GlowIcons: IMAG parse failed", flush=True)
            elif chunk_id == b"ARGB":
                # OS4 true-color format
                img = self._parse_argb_chunk(chunk_data)
                if img:
                    images.append(img)
                    if self._debug:
                        print(f"[icon_parser] GlowIcons: ARGB parsed: {img['width']}x{img['height']}", flush=True)

            # Move to next chunk (aligned to word boundary)
            pos += 8 + chunk_size
            if chunk_size % 2:
                pos += 1

        if not images:
            if self._debug:
                print(f"[icon_parser] GlowIcons: no images found in {len(data)} bytes", flush=True)
            return None

        # Use first image (normal state)
        img = images[0]
        # GlowIcons have square pixels (aspect ratio 1:1)
        return {
            "width": img["width"],
            "height": img["height"],
            "rgba": img["rgba"],
            "format": "glowicons",
            "aspect_ratio": 1.0,  # GlowIcons use square pixels
        }

    def _parse_face_chunk(self, data: bytes) -> Optional[Dict]:
        """Parse FACE chunk (icon properties).

        FACE chunk is 6 bytes:
          $00 byte width - 1
          $01 byte height - 1
          $02 byte flags
          $03 byte aspect ratio
          $04-05 word max palette bytes (optional)
        """
        if len(data) < 4:
            return None
        return {
            "width": data[0] + 1,
            "height": data[1] + 1,
            "flags": data[2],
            "aspect": data[3],
        }

    def _parse_imag_chunk(self, data: bytes, face_info: Optional[Dict] = None) -> Optional[Dict]:
        """Parse IMAG chunk (bitmap data).

        IMAG chunk header (10 bytes):
          $00 byte transparent color number
          $01 byte number of icon colors - 1
          $02 byte flags (bit0=transparency, bit1=sharedcolormap)
          $03 byte compressed image flag (BOOL)
          $04 byte compressed colormap flag (BOOL)
          $05 byte depth (bits per pixel)
          $06 word compressed image size - 1
          $08 word compressed colormap size - 1

        Data order: IMAGE data first, then COLORMAP data.
        Dimensions come from FACE chunk.

        Args:
            data: The IMAG chunk data.
            face_info: FACE chunk info with width/height (required).
        """
        if len(data) < 10:
            if self._debug:
                print(f"[icon_parser] IMAG: data too short ({len(data)} < 10)", flush=True)
            return None

        if not face_info or not face_info.get("width") or not face_info.get("height"):
            if self._debug:
                print(f"[icon_parser] IMAG: no FACE info, cannot determine dimensions", flush=True)
            return None

        width = face_info["width"]
        height = face_info["height"]

        # Parse IMAG header
        transparent_color = data[0]
        num_colors = data[1] + 1  # stored as (colors - 1)
        flags = data[2]
        image_compressed = data[3] != 0
        colormap_compressed = data[4] != 0
        depth = data[5]
        image_size = struct.unpack(">H", data[6:8])[0] + 1  # stored as (size - 1)
        colormap_size = struct.unpack(">H", data[8:10])[0] + 1  # stored as (size - 1)

        has_transparency = (flags & 1) != 0

        if self._debug:
            print(f"[icon_parser] IMAG: {width}x{height}, trans={transparent_color}, "
                  f"colors={num_colors}, depth={depth}, flags={flags:#x}", flush=True)
            print(f"[icon_parser] IMAG: image_compressed={image_compressed}, "
                  f"colormap_compressed={colormap_compressed}", flush=True)
            print(f"[icon_parser] IMAG: image_size={image_size}, colormap_size={colormap_size}", flush=True)
            # Dump raw header bytes for debugging
            print(f"[icon_parser] IMAG header: {data[:10].hex(' ')}", flush=True)
            print(f"[icon_parser] IMAG total chunk size: {len(data)}", flush=True)

        pos = 10  # After header

        # Data order in file: IMAGE first, then COLORMAP
        # Read image data first
        expected_pixels = width * height
        if image_size > 1 and pos + image_size <= len(data):
            raw_image_data = data[pos:pos + image_size]
            pos += image_size
        else:
            raw_image_data = data[pos:]
            pos = len(data)

        # Read colormap data second
        palette = None
        if colormap_size > 1 and pos + colormap_size <= len(data):
            raw_colormap_data = data[pos:pos + colormap_size]

            # Decompress colormap (always uses 8-bit RLE)
            if colormap_compressed:
                colormap_data = self._unpack_rle_8bit(raw_colormap_data, num_colors * 3)
            else:
                colormap_data = raw_colormap_data

            # Parse RGB palette
            palette = []
            for i in range(0, min(len(colormap_data), num_colors * 3), 3):
                if i + 2 < len(colormap_data):
                    palette.append((colormap_data[i], colormap_data[i + 1], colormap_data[i + 2]))

            if self._debug:
                print(f"[icon_parser] IMAG: parsed {len(palette)} palette colors", flush=True)

        # Decompress image data (uses bit-packed RLE with 'depth' bits per pixel)
        if image_compressed:
            image_data = self._unpack_rle_bitpacked(raw_image_data, expected_pixels, depth)
        else:
            image_data = raw_image_data

        if self._debug:
            print(f"[icon_parser] IMAG: decompressed {len(image_data)} pixels", flush=True)

        if len(image_data) < expected_pixels:
            if self._debug:
                print(f"[icon_parser] IMAG: not enough pixels ({len(image_data)} < {expected_pixels})", flush=True)
            return None

        # Convert indexed to RGBA
        if palette is None or len(palette) == 0:
            # Use default palette based on color count
            if num_colors <= 4:
                palette = WORKBENCH_PALETTE_4
            elif num_colors <= 8:
                palette = MAGICWB_PALETTE_8
            else:
                palette = PALETTE_16

        rgba = bytearray(width * height * 4)
        for i in range(width * height):
            idx = image_data[i] if i < len(image_data) else 0
            if idx < len(palette):
                r, g, b = palette[idx]
            else:
                r, g, b = 0, 0, 0

            # Handle transparency
            if has_transparency and idx == transparent_color:
                a = 0
            else:
                a = 255

            rgba[i * 4] = r
            rgba[i * 4 + 1] = g
            rgba[i * 4 + 2] = b
            rgba[i * 4 + 3] = a

        return {
            "width": width,
            "height": height,
            "rgba": bytes(rgba),
        }

    def _parse_argb_chunk(self, data: bytes) -> Optional[Dict]:
        """Parse ARGB chunk (OS4 true-color format)."""
        if len(data) < 8:
            return None

        width = struct.unpack(">H", data[0:2])[0]
        height = struct.unpack(">H", data[2:4])[0]

        if width == 0 or height == 0:
            return None

        expected_size = width * height * 4
        pixel_data = data[4:]

        if len(pixel_data) < expected_size:
            return None

        # ARGB to RGBA conversion
        rgba = bytearray(expected_size)
        for i in range(width * height):
            offset = i * 4
            a = pixel_data[offset]
            r = pixel_data[offset + 1]
            g = pixel_data[offset + 2]
            b = pixel_data[offset + 3]
            rgba[offset] = r
            rgba[offset + 1] = g
            rgba[offset + 2] = b
            rgba[offset + 3] = a

        return {
            "width": width,
            "height": height,
            "rgba": bytes(rgba),
        }

    def _unpack_rle_8bit(self, data: bytes, expected_size: int) -> bytes:
        """Decompress 8-bit RLE data (icon.library format for colormap).

        This is similar to PackBits but:
        - n < 128: copy n+1 literal bytes
        - n >= 128: repeat next byte (256-n)+1 times (no no-op for 128)
        """
        result = bytearray()
        i = 0

        while i < len(data) and len(result) < expected_size:
            if i + 2 > len(data):
                break
            n = data[i]
            i += 1

            if n < 128:
                # Literal run: copy n+1 bytes
                count = n + 1
                result.extend(data[i:i + count])
                i += count
            else:
                # Replicate run: repeat next byte (256-n)+1 times
                # n=128 -> 129 repeats, n=255 -> 2 repeats
                if i < len(data):
                    count = (256 - n) + 1
                    result.extend([data[i]] * count)
                    i += 1

        return bytes(result[:expected_size])

    def _unpack_rle_bitpacked(self, data: bytes, expected_pixels: int, depth: int) -> bytes:
        """Decompress bit-packed RLE data (icon.library format for image).

        The algorithm reads from a bitstream:
        - Control bytes are 8 bits (sign-extended)
        - Data values are 'depth' bits each
        - Output is one byte per pixel

        Based on icon.library decompressdata routine.
        """
        if depth == 8:
            # For depth=8, use the simpler 8-bit RLE
            return self._unpack_rle_8bit(data, expected_pixels)

        result = bytearray()

        # Bit buffer for reading from the packed stream
        bit_buffer = 0  # Holds accumulated bits
        bits_in_buffer = 0  # Number of valid bits in buffer
        data_pos = 0

        def read_bits(num_bits: int) -> int:
            """Read num_bits from the bitstream."""
            nonlocal bit_buffer, bits_in_buffer, data_pos

            # Accumulate enough bits
            while bits_in_buffer < num_bits:
                if data_pos >= len(data):
                    return -1  # No more data
                byte = data[data_pos]
                data_pos += 1
                # Add new byte to buffer (shifted left past existing bits)
                bit_buffer = (bit_buffer << 8) | byte
                bits_in_buffer += 8

            # Extract the requested bits from the top of the buffer
            bits_in_buffer -= num_bits
            value = (bit_buffer >> bits_in_buffer) & ((1 << num_bits) - 1)
            return value

        while len(result) < expected_pixels and data_pos < len(data):
            # Read 8-bit control byte
            control = read_bits(8)
            if control < 0:
                break

            # Sign-extend the control byte
            if control >= 128:
                control = control - 256

            if control >= 0:
                # Literal run: copy (control + 1) pixel values
                count = control + 1
                for _ in range(count):
                    if len(result) >= expected_pixels:
                        break
                    pixel = read_bits(depth)
                    if pixel < 0:
                        break
                    result.append(pixel)
            else:
                # Repeat run: repeat next pixel value (-control + 1) times
                count = -control + 1
                pixel = read_bits(depth)
                if pixel < 0:
                    break
                for _ in range(count):
                    if len(result) >= expected_pixels:
                        break
                    result.append(pixel)

        return bytes(result)

    def _try_newicons(self, data: bytes) -> Optional[Dict]:
        """Try to parse NewIcons data from ToolTypes."""
        tooltypes = self._parse_tooltypes(data)
        if not tooltypes:
            if self._debug:
                print(f"[icon_parser] NewIcons: no ToolTypes found", flush=True)
            return None

        if self._debug:
            print(f"[icon_parser] NewIcons: found {len(tooltypes)} ToolTypes entries", flush=True)
            for i, tt in enumerate(tooltypes[:5]):  # Show first 5
                print(f"[icon_parser]   [{i}]: {tt[:60]}{'...' if len(tt) > 60 else ''}", flush=True)
            if len(tooltypes) > 5:
                print(f"[icon_parser]   ... and {len(tooltypes) - 5} more", flush=True)

        # Look for NewIcons marker
        found_marker = False
        for tt in tooltypes:
            if tt.strip() == "*** DON'T EDIT THE FOLLOWING LINES!! ***":
                found_marker = True
                break

        # Also check for IM1= without marker (some NewIcons)
        has_im1 = any(tt.startswith("IM1=") for tt in tooltypes)

        if not found_marker and not has_im1:
            if self._debug:
                print(f"[icon_parser] NewIcons: no marker or IM1= found", flush=True)
            return None

        if self._debug:
            print(f"[icon_parser] NewIcons: marker={found_marker}, has_im1={has_im1}", flush=True)

        # Collect IM1 lines
        im1_lines = []
        im2_lines = []
        current_list = None

        for tt in tooltypes:
            if tt.startswith("IM1="):
                current_list = im1_lines
                im1_lines.append(tt[4:])
            elif tt.startswith("IM2="):
                current_list = im2_lines
                im2_lines.append(tt[4:])
            elif current_list is not None and not tt.startswith("***") and tt.strip():
                # Continuation line
                current_list.append(tt)

        if not im1_lines:
            return None

        # Decode IM1
        image_data = "".join(im1_lines)
        result = self._decode_newicons_image(image_data)
        if result:
            result["format"] = "newicons"
            result["aspect_ratio"] = 1.0  # NewIcons use square pixels
        return result

    def _decode_newicons_image(self, data: str) -> Optional[Dict]:
        """Decode NewIcons image data.

        NewIcons uses a 7-bit ASCII encoding:
        - Characters 0x20-0x6F map to values 0x00-0x4F
        - Characters 0xA1-0xD0 map to values 0x50-0x7F
        - Characters 0xD1-0xFF are RLE markers
        """
        if len(data) < 5:
            return None

        try:
            # Decode bytes
            decoded = []
            i = 0
            while i < len(data):
                c = ord(data[i])
                if 0x20 <= c <= 0x6F:
                    decoded.append(c - 0x20)
                elif 0xA1 <= c <= 0xD0:
                    decoded.append(c - 0xA1 + 0x50)
                elif 0xD1 <= c <= 0xFF:
                    # RLE: repeat next value (c - 0xD0) times
                    repeat = c - 0xD0
                    i += 1
                    if i < len(data):
                        next_c = ord(data[i])
                        if 0x20 <= next_c <= 0x6F:
                            val = next_c - 0x20
                        elif 0xA1 <= next_c <= 0xD0:
                            val = next_c - 0xA1 + 0x50
                        else:
                            val = 0
                        decoded.extend([val] * repeat)
                i += 1

            if len(decoded) < 4:
                return None

            # Parse header
            # Byte 0: transparency flag (0 = transparent background)
            transparent = decoded[0] == 0
            # Byte 1: width
            width = decoded[1]
            # Byte 2: height
            height = decoded[2]
            # Byte 3+: color count (can be 1 or 2 bytes)

            # Determine color count
            pos = 3
            if decoded[pos] == 0:
                # Two-byte color count
                pos += 1
                num_colors = decoded[pos]
                pos += 1
            else:
                num_colors = decoded[pos]
                pos += 1

            if num_colors == 0:
                num_colors = 256

            if width == 0 or height == 0 or width > 640 or height > 512:
                return None

            # Read palette (3 bytes per color: R, G, B)
            palette = []
            for _ in range(num_colors):
                if pos + 2 >= len(decoded):
                    break
                r = decoded[pos] << 2
                g = decoded[pos + 1] << 2
                b = decoded[pos + 2] << 2
                # Scale 6-bit to 8-bit
                r = min(255, r + (r >> 6))
                g = min(255, g + (g >> 6))
                b = min(255, b + (b >> 6))
                palette.append((r, g, b))
                pos += 3

            # Read pixel data
            pixels = decoded[pos:]

            # Convert to RGBA
            rgba = bytearray(width * height * 4)
            for i in range(width * height):
                idx = pixels[i] if i < len(pixels) else 0
                if idx < len(palette):
                    r, g, b = palette[idx]
                else:
                    r, g, b = 0, 0, 0

                # First palette entry is transparent if flag set
                if transparent and idx == 0:
                    a = 0
                else:
                    a = 255

                rgba[i * 4] = r
                rgba[i * 4 + 1] = g
                rgba[i * 4 + 2] = b
                rgba[i * 4 + 3] = a

            return {
                "width": width,
                "height": height,
                "rgba": bytes(rgba),
            }

        except Exception as e:
            if self._debug:
                print(f"[icon_parser] NewIcons decode error: {e}")
            return None

    def _parse_tooltypes(self, data: bytes) -> List[str]:
        """Parse ToolTypes array from DiskObject data."""
        if len(data) < 78:
            return []

        # DiskObject layout (Gadget is 44 bytes, not 48):
        # 0-1: Magic (0xE310)
        # 2-3: Version
        # 4-47: Gadget structure (44 bytes)
        # 48: do_Type
        # 49: do_Pad
        # 50-53: do_DefaultTool pointer
        # 54-57: do_ToolTypes pointer
        # 58-61: do_CurrentX
        # 62-65: do_CurrentY
        # 66-69: do_DrawerData pointer
        # 70-73: do_ToolWindow pointer
        # 74-77: do_StackSize

        # The pointers in the file are offsets relative to the file, not memory addresses
        # We need to find where the ToolTypes data actually is

        # Check for DrawerData by looking at do_DrawerData pointer (offset 66-69)
        do_drawer_data = struct.unpack(">I", data[66:70])[0]
        has_drawer = do_drawer_data != 0

        # After the 78-byte DiskObject header comes:
        # - DrawerData (56 bytes) if do_DrawerData pointer is non-zero
        # - First Image header (20 bytes)
        # - First Image data
        # - Second Image header (20 bytes) - optional
        # - Second Image data - optional
        # - DefaultTool string (null-terminated)
        # - ToolTypes array

        pos = 78

        # Skip DrawerData if present
        if has_drawer:
            pos += 56

        # Parse first image to skip past it
        if pos + 20 > len(data):
            return []

        # Image structure
        img1_width = struct.unpack(">h", data[pos + 4:pos + 6])[0]
        img1_height = struct.unpack(">h", data[pos + 6:pos + 8])[0]
        img1_depth = struct.unpack(">h", data[pos + 8:pos + 10])[0]

        if img1_width <= 0 or img1_height <= 0 or img1_depth <= 0:
            return []

        # Image data size: depth * height * ((width + 15) / 16) * 2
        row_words = (img1_width + 15) // 16
        img1_data_size = img1_depth * img1_height * row_words * 2

        pos += 20  # Image header
        pos += img1_data_size  # Image data

        # Check for second image
        gadget_flags = struct.unpack(">H", data[16:18])[0]
        has_second_image = (gadget_flags & 0x0002) != 0  # GFLG_GADGHIMAGE

        if has_second_image and pos + 20 <= len(data):
            img2_width = struct.unpack(">h", data[pos + 4:pos + 6])[0]
            img2_height = struct.unpack(">h", data[pos + 6:pos + 8])[0]
            img2_depth = struct.unpack(">h", data[pos + 8:pos + 10])[0]

            if img2_width > 0 and img2_height > 0 and img2_depth > 0:
                row_words = (img2_width + 15) // 16
                img2_data_size = img2_depth * img2_height * row_words * 2
                pos += 20 + img2_data_size

        # Now we should be at DefaultTool string
        # Read DefaultTool pointer from DiskObject - if non-zero, there's a string
        default_tool_ptr = struct.unpack(">I", data[50:54])[0]
        if default_tool_ptr:
            # Skip the null-terminated DefaultTool string
            null_pos = data.find(b'\x00', pos)
            if null_pos != -1:
                pos = null_pos + 1
            else:
                return []

        # Now we should be at ToolTypes
        tooltypes_ptr = struct.unpack(">I", data[54:58])[0]
        if not tooltypes_ptr:
            if self._debug:
                print(f"[icon_parser] ToolTypes: ptr=0, no ToolTypes", flush=True)
            return []

        # ToolTypes format in .info files:
        # - 4-byte total length of ToolTypes block (includes this length field)
        # - For each entry:
        #   - 4-byte string length
        #   - String data (usually null-terminated but we use length)

        if pos + 4 > len(data):
            if self._debug:
                print(f"[icon_parser] ToolTypes: pos={pos} too close to end ({len(data)})", flush=True)
            return []

        total_len = struct.unpack(">I", data[pos:pos + 4])[0]
        if self._debug:
            print(f"[icon_parser] ToolTypes: pos={pos}, total_len={total_len}", flush=True)
        pos += 4
        remaining = total_len - 4  # Length field counts toward total

        tooltypes = []
        while remaining > 0 and pos + 4 <= len(data):
            str_len = struct.unpack(">I", data[pos:pos + 4])[0]
            pos += 4
            remaining -= 4

            if str_len > 0 and pos + str_len <= len(data):
                try:
                    # String may have null terminator, strip it
                    tt_bytes = data[pos:pos + str_len]
                    tt_str = tt_bytes.decode('latin-1').rstrip('\x00')
                    if tt_str:
                        tooltypes.append(tt_str)
                except UnicodeDecodeError:
                    pass
                pos += str_len
            elif str_len == 0:
                # Empty entry, skip
                pass
            else:
                if self._debug:
                    print(f"[icon_parser] ToolTypes: str_len={str_len} invalid at pos={pos}", flush=True)
                break

        if self._debug:
            print(f"[icon_parser] ToolTypes: found {len(tooltypes)} entries", flush=True)

        return tooltypes

    def _try_traditional(self, data: bytes) -> Optional[Dict]:
        """Parse traditional Amiga icon (planar bitmap)."""
        if len(data) < 78:
            if self._debug:
                print(f"[icon_parser] Traditional: data too short ({len(data)} < 78)")
            return None

        try:
            # DiskObject structure layout:
            # 0-1: do_Magic (UWORD)
            # 2-3: do_Version (UWORD)
            # 4-47: do_Gadget (struct Gadget, 44 bytes)
            # 48: do_Type (UBYTE)
            # 49: do_Pad (UBYTE)
            # 50-53: do_DefaultTool (STRPTR, 4 bytes)
            # 54-57: do_ToolTypes (STRPTR*, 4 bytes)
            # 58-61: do_CurrentX (LONG)
            # 62-65: do_CurrentY (LONG)
            # 66-69: do_DrawerData (BPTR, 4 bytes)
            # 70-73: do_ToolWindow (STRPTR)
            # 74-77: do_StackSize (LONG)
            # Total: 78 bytes

            version = struct.unpack(">H", data[2:4])[0]
            do_type = data[48]  # do_Type is at offset 48 (after 44-byte Gadget)

            # Gadget structure starts at offset 4
            # Gadget.Width at offset 4+8=12, Gadget.Height at offset 4+10=14
            gd_width = struct.unpack(">h", data[12:14])[0]
            gd_height = struct.unpack(">h", data[14:16])[0]

            # Gadget.UserData is at offset 4+40=44 (last 4 bytes of 44-byte Gadget)
            # Used to detect WB version: 0 = WB 1.x, non-zero = WB 2.x+
            user_data = struct.unpack(">I", data[44:48])[0]
            is_wb2 = user_data != 0

            if self._debug:
                print(f"[icon_parser] Traditional: version={version}, type={do_type}, "
                      f"gadget={gd_width}x{gd_height}, UserData={user_data} ({'WB2+' if is_wb2 else 'WB1.x'})",
                      flush=True)

            if gd_width <= 0 or gd_height <= 0 or gd_width > 640 or gd_height > 512:
                if self._debug:
                    print(f"[icon_parser] Traditional: invalid gadget dims {gd_width}x{gd_height}")
                return None

            # Check for DrawerData - it's present if do_DrawerData pointer is non-zero
            # do_DrawerData is at offset 66-69 in DiskObject
            # Note: pointer is non-zero for WBDISK, WBDRAWER, WBGARBAGE types that have drawer data
            do_drawer_data = struct.unpack(">I", data[66:70])[0]
            is_drawer_type = do_type in (WBDISK, WBDRAWER, WBGARBAGE)
            # Use pointer to determine if DrawerData is present
            has_drawer = do_drawer_data != 0
            pos = 78
            if has_drawer:
                pos += 56
                if self._debug:
                    print(f"[icon_parser] Traditional: DrawerData present "
                          f"(ptr=0x{do_drawer_data:08x}, type={do_type}), pos now {pos}", flush=True)
            else:
                if self._debug:
                    print(f"[icon_parser] Traditional: No DrawerData "
                          f"(ptr=0x{do_drawer_data:08x}, type={do_type}), pos={pos}", flush=True)

            # First image header at pos
            if pos + 20 > len(data):
                if self._debug:
                    print(f"[icon_parser] Traditional: no room for image header at pos {pos}")
                return None

            if self._debug:
                # Show raw bytes at image header position
                img_header_hex = data[pos:pos + 20].hex(' ')
                print(f"[icon_parser] Traditional: Image header at {pos}: {img_header_hex}", flush=True)

            # Image structure
            img_left = struct.unpack(">h", data[pos:pos + 2])[0]
            img_top = struct.unpack(">h", data[pos + 2:pos + 4])[0]
            img_width = struct.unpack(">h", data[pos + 4:pos + 6])[0]
            img_height = struct.unpack(">h", data[pos + 6:pos + 8])[0]
            img_depth = struct.unpack(">h", data[pos + 8:pos + 10])[0]
            # img_data_ptr at pos + 10 (4 bytes) - not used for file format
            plane_pick = data[pos + 14]
            plane_on_off = data[pos + 15]

            if self._debug:
                print(f"[icon_parser] Traditional: image at pos {pos}: "
                      f"{img_width}x{img_height}x{img_depth}, pick={plane_pick:#x}, "
                      f"onoff={plane_on_off:#x}", flush=True)

            if img_width <= 0 or img_height <= 0 or img_depth <= 0:
                if self._debug:
                    print(f"[icon_parser] Traditional: invalid image dims or depth")
                return None
            if img_width > 640 or img_height > 512 or img_depth > 8:
                if self._debug:
                    print(f"[icon_parser] Traditional: image dims/depth out of range")
                return None

            # Calculate image data size
            row_words = (img_width + 15) // 16
            row_bytes = row_words * 2
            plane_size = row_bytes * img_height
            total_size = img_depth * plane_size

            if self._debug:
                print(f"[icon_parser] Traditional: row_bytes={row_bytes}, "
                      f"plane_size={plane_size}, total_size={total_size}", flush=True)

            pos += 20  # Skip image header

            if pos + total_size > len(data):
                if self._debug:
                    print(f"[icon_parser] Traditional: not enough data: need {pos + total_size}, "
                          f"have {len(data)}")
                return None

            image_data = data[pos:pos + total_size]

            # Convert planar to chunky
            pixels = self._planar_to_chunky(
                image_data, img_width, img_height, img_depth,
                plane_pick, plane_on_off
            )

            # Choose palette based on Workbench version (from UserData field)
            # and extend to 16 colors for deep icons
            if img_depth > 3:
                palette = PALETTE_16
            elif is_wb2:
                palette = WB20_PALETTE
            else:
                palette = WB13_PALETTE

            if self._debug:
                print(f"[icon_parser] Traditional: using {'WB2.0' if is_wb2 else 'WB1.3'} palette "
                      f"({len(palette)} colors)", flush=True)

            # Find which color 0 pixels are on the outer edge (connected to image border)
            # These will be transparent; interior color 0 pixels stay opaque gray
            edge_transparent = self._find_edge_background(pixels, img_width, img_height)

            # Convert to RGBA
            rgba = bytearray(img_width * img_height * 4)
            for i, idx in enumerate(pixels):
                if idx < len(palette):
                    r, g, b = palette[idx]
                else:
                    r, g, b = 0, 0, 0

                # Color 0 on outer edge is transparent; interior color 0 stays gray
                if idx == 0 and edge_transparent[i]:
                    a = 0
                else:
                    a = 255

                rgba[i * 4] = r
                rgba[i * 4 + 1] = g
                rgba[i * 4 + 2] = b
                rgba[i * 4 + 3] = a

            if self._debug:
                print(f"[icon_parser] Traditional: SUCCESS! {img_width}x{img_height}x{img_depth}",
                      flush=True)

            return {
                "width": img_width,
                "height": img_height,
                "rgba": bytes(rgba),
                "format": "traditional",
                "aspect_ratio": 2.0,  # 2:1 aspect correction - icons were designed for 640x200
            }

        except Exception as e:
            if self._debug:
                print(f"[icon_parser] Traditional parse error: {e}")
                traceback.print_exc()
            return None

    def _find_edge_background(self, pixels: List[int], width: int, height: int) -> List[bool]:
        """Find color 0 pixels connected to the image edge (flood fill).

        Returns a list of booleans - True for pixels that should be transparent
        (color 0 and connected to the edge), False otherwise.
        """
        # Result: True = this pixel should be transparent
        is_edge = [False] * len(pixels)

        # Use a queue for flood fill from all edge pixels that are color 0
        from collections import deque
        queue = deque()

        # Add all edge pixels that are color 0 to the queue
        for x in range(width):
            # Top edge
            idx = x
            if pixels[idx] == 0 and not is_edge[idx]:
                is_edge[idx] = True
                queue.append((x, 0))
            # Bottom edge
            idx = (height - 1) * width + x
            if pixels[idx] == 0 and not is_edge[idx]:
                is_edge[idx] = True
                queue.append((x, height - 1))

        for y in range(height):
            # Left edge
            idx = y * width
            if pixels[idx] == 0 and not is_edge[idx]:
                is_edge[idx] = True
                queue.append((0, y))
            # Right edge
            idx = y * width + (width - 1)
            if pixels[idx] == 0 and not is_edge[idx]:
                is_edge[idx] = True
                queue.append((width - 1, y))

        # Flood fill to find all connected color 0 pixels
        while queue:
            x, y = queue.popleft()

            # Check 4 neighbors
            for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height:
                    nidx = ny * width + nx
                    if pixels[nidx] == 0 and not is_edge[nidx]:
                        is_edge[nidx] = True
                        queue.append((nx, ny))

        return is_edge

    def _planar_to_chunky(
        self,
        data: bytes,
        width: int,
        height: int,
        depth: int,
        plane_pick: int,
        plane_on_off: int
    ) -> List[int]:
        """Convert planar Amiga image data to chunky pixel indices.

        Planar format: All rows of plane 0, then all rows of plane 1, etc.
        Each row is padded to word (16-bit) boundary.
        """
        row_words = (width + 15) // 16
        row_bytes = row_words * 2
        plane_size = row_bytes * height

        pixels = []
        for y in range(height):
            for x in range(width):
                byte_offset = (x // 8) + (y * row_bytes)
                bit_offset = 7 - (x % 8)

                pixel = 0
                for plane in range(depth):
                    # Check if this plane is in plane_pick
                    if plane_pick & (1 << plane):
                        plane_offset = plane * plane_size
                        if plane_offset + byte_offset < len(data):
                            if data[plane_offset + byte_offset] & (1 << bit_offset):
                                pixel |= (1 << plane)
                    else:
                        # Use plane_on_off for this plane
                        if plane_on_off & (1 << plane):
                            pixel |= (1 << plane)

                pixels.append(pixel)

        return pixels


def create_icns(rgba: bytes, width: int, height: int, debug: bool = False,
                aspect_ratio: float = 2.0) -> bytes:
    """Convert RGBA pixel data to ICNS format.

    Creates an ICNS file with multiple size variants.
    macOS Finder requires at least ic07 (128x128) for custom icons to display.

    Args:
        rgba: RGBA pixel data
        width: Source image width
        height: Source image height
        debug: Enable debug output
        aspect_ratio: Pixel aspect ratio (height/width). Amiga typically uses 2.0
                      meaning pixels are displayed twice as tall as wide.
                      Set to 1.0 for square pixels.
    """
    entries = []

    # First, correct the aspect ratio
    # Amiga pixels on PAL/NTSC were displayed taller than wide (roughly 2:1)
    # On square-pixel displays, icons look too short/squashed
    # To fix this, we stretch the HEIGHT to match the visual appearance
    if aspect_ratio != 1.0 and aspect_ratio > 0:
        # Correct aspect ratio by scaling height
        corrected_width = width
        corrected_height = int(height * aspect_ratio)
        corrected_rgba = scale_image(rgba, width, height, corrected_width, corrected_height)
        if debug:
            print(f"[ICNS] Aspect ratio correction: {width}x{height} -> {corrected_width}x{corrected_height}", flush=True)
    else:
        corrected_width = width
        corrected_height = height
        corrected_rgba = rgba

    max_dim = max(corrected_width, corrected_height)

    # Generate different sizes
    # macOS Finder uses various sizes: 16, 32, 48, 64, 128, 256, 512
    # We need 64x64 for proper 48x48 and 64x64 display (Finder default is 64x64)
    # Icon sizes with their type codes
    # Some sizes have multiple type codes (non-Retina and Retina variants)
    size_configs = [
        (16,  [b'icp4']),           # 16x16 - small icon views (non-Retina)
        (32,  [b'icp5', b'ic11']),  # 32x32 - standard small + 16x16@2x Retina
        (64,  [b'icp6', b'ic12']),  # 64x64 - Finder default + 32x32@2x Retina
        (128, [b'ic07']),           # 128x128 - REQUIRED for custom icons
        (256, [b'ic08', b'ic13']),  # 256x256 - high-res + 128x128@2x Retina
        (512, [b'ic09', b'ic14']),  # 512x512 - extra large + 256x256@2x Retina
    ]

    for size, type_codes in size_configs:
        # Skip 512x512 if we'd be upscaling more than 8x
        if size > 256 and size > max_dim * 8:
            continue

        # Scale image maintaining aspect ratio, then center in square
        scaled_rgba = scale_image_fit(corrected_rgba, corrected_width, corrected_height, size, size)

        # Use PNG for all sizes (modern macOS supports PNG in all ICNS slots)
        png_data = encode_png(scaled_rgba, size, size)

        # Add entry for each type code at this size
        for type_code in type_codes:
            entries.append((type_code, png_data))

        if debug:
            codes_str = ", ".join(tc.decode() for tc in type_codes)
            print(f"[ICNS] Added {codes_str}: {size}x{size} ({len(png_data)} bytes)", flush=True)

    # Build ICNS container
    result = build_icns(entries)
    if debug:
        print(f"[ICNS] Total ICNS size: {len(result)} bytes with {len(entries)} entries", flush=True)
    return result


def scale_image(rgba: bytes, src_w: int, src_h: int, dst_w: int, dst_h: int) -> bytes:
    """Scale RGBA image using nearest-neighbor (best for pixel art)."""
    result = bytearray(dst_w * dst_h * 4)

    x_ratio = src_w / dst_w
    y_ratio = src_h / dst_h

    for y in range(dst_h):
        for x in range(dst_w):
            src_x = int(x * x_ratio)
            src_y = int(y * y_ratio)
            src_x = min(src_x, src_w - 1)
            src_y = min(src_y, src_h - 1)

            src_idx = (src_y * src_w + src_x) * 4
            dst_idx = (y * dst_w + x) * 4

            result[dst_idx:dst_idx + 4] = rgba[src_idx:src_idx + 4]

    return bytes(result)


def scale_image_fit(rgba: bytes, src_w: int, src_h: int, dst_w: int, dst_h: int) -> bytes:
    """Scale RGBA image to fit within destination, maintaining aspect ratio.

    The image is scaled to fit within dst_w x dst_h while maintaining its
    aspect ratio, then centered in the destination with transparent padding.
    """
    # Calculate scale factor to fit within destination
    scale_x = dst_w / src_w
    scale_y = dst_h / src_h
    scale = min(scale_x, scale_y)

    # Calculate scaled dimensions
    scaled_w = int(src_w * scale)
    scaled_h = int(src_h * scale)

    # Ensure at least 1 pixel
    scaled_w = max(1, scaled_w)
    scaled_h = max(1, scaled_h)

    # Scale the source image
    scaled = scale_image(rgba, src_w, src_h, scaled_w, scaled_h)

    # Create transparent destination
    result = bytearray(dst_w * dst_h * 4)  # All zeros = transparent

    # Calculate offset to center the image
    offset_x = (dst_w - scaled_w) // 2
    offset_y = (dst_h - scaled_h) // 2

    # Copy scaled image into center of result
    for y in range(scaled_h):
        for x in range(scaled_w):
            src_idx = (y * scaled_w + x) * 4
            dst_x = offset_x + x
            dst_y = offset_y + y
            if 0 <= dst_x < dst_w and 0 <= dst_y < dst_h:
                dst_idx = (dst_y * dst_w + dst_x) * 4
                result[dst_idx:dst_idx + 4] = scaled[src_idx:src_idx + 4]

    return bytes(result)


def encode_png(rgba: bytes, width: int, height: int) -> bytes:
    """Encode RGBA data as PNG."""
    def write_chunk(chunk_type: bytes, data: bytes) -> bytes:
        chunk = chunk_type + data
        crc = zlib.crc32(chunk) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk + struct.pack(">I", crc)

    # PNG signature
    result = b'\x89PNG\r\n\x1a\n'

    # IHDR chunk
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    result += write_chunk(b'IHDR', ihdr_data)

    # IDAT chunk (compressed image data)
    raw_data = bytearray()
    for y in range(height):
        raw_data.append(0)  # Filter type: None
        for x in range(width):
            idx = (y * width + x) * 4
            raw_data.extend(rgba[idx:idx + 4])

    compressed = zlib.compress(bytes(raw_data), 9)
    result += write_chunk(b'IDAT', compressed)

    # IEND chunk
    result += write_chunk(b'IEND', b'')

    return result


def build_icns(entries: List[Tuple[bytes, bytes]]) -> bytes:
    """Build ICNS container from list of (type, data) entries."""
    # Calculate total size
    data_size = sum(8 + len(data) for _, data in entries)
    total_size = 8 + data_size  # 'icns' + size + entries

    result = bytearray()
    result.extend(b'icns')
    result.extend(struct.pack(">I", total_size))

    for type_code, data in entries:
        result.extend(type_code)
        result.extend(struct.pack(">I", 8 + len(data)))
        result.extend(data)

    return bytes(result)
