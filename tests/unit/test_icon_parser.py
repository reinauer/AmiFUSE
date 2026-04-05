"""Unit tests for amifuse.icon_parser module.

Tests cover IconParser class methods and module-level utility functions.
All binary payloads are constructed inline using struct.pack() -- no fixture files.
"""

import struct
from typing import Optional

import pytest

from amifuse.icon_parser import (
    IconParser,
    WB_DISKMAGIC,
    WB13_PALETTE,
    WB20_PALETTE,
    build_icns,
    create_icns,
    encode_png,
    scale_image,
    scale_image_fit,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_diskobject_header(
    *,
    magic: int = WB_DISKMAGIC,
    version: int = 1,
    gd_width: int = 16,
    gd_height: int = 16,
    gd_flags: int = 0,
    user_data: int = 0,
    do_type: int = 3,       # WBTOOL
    default_tool_ptr: int = 0,
    tooltypes_ptr: int = 0,
    drawer_data_ptr: int = 0,
) -> bytes:
    """Build a 78-byte DiskObject header.

    Layout (all big-endian):
      0-1:   do_Magic
      2-3:   do_Version
      4-7:   gg_NextGadget (ptr, 0)
      8-9:   gg_LeftEdge
      10-11: gg_TopEdge
      12-13: gg_Width
      14-15: gg_Height
      16-17: gg_Flags
      18-19: gg_Activation
      20-21: gg_GadgetType
      22-25: gg_GadgetRender (ptr)
      26-29: gg_SelectRender (ptr)
      30-33: gg_GadgetText (ptr)
      34-37: gg_MutualExclude
      38-41: gg_SpecialInfo (ptr)
      42-43: gg_GadgetID
      44-47: gg_UserData          <-- controls WB13 vs WB20 palette
      48:    do_Type
      49:    do_Pad
      50-53: do_DefaultTool (ptr)
      54-57: do_ToolTypes (ptr)
      58-61: do_CurrentX
      62-65: do_CurrentY
      66-69: do_DrawerData (ptr)
      70-73: do_ToolWindow (ptr)
      74-77: do_StackSize
    """
    hdr = bytearray(78)
    struct.pack_into(">H", hdr, 0, magic)
    struct.pack_into(">H", hdr, 2, version)
    # Gadget fields
    struct.pack_into(">h", hdr, 12, gd_width)
    struct.pack_into(">h", hdr, 14, gd_height)
    struct.pack_into(">H", hdr, 16, gd_flags)
    struct.pack_into(">I", hdr, 44, user_data)
    # DiskObject fields after Gadget
    hdr[48] = do_type
    struct.pack_into(">I", hdr, 50, default_tool_ptr)
    struct.pack_into(">I", hdr, 54, tooltypes_ptr)
    struct.pack_into(">I", hdr, 66, drawer_data_ptr)
    return bytes(hdr)


def _make_image_header(
    *,
    width: int = 1,
    height: int = 1,
    depth: int = 1,
    plane_pick: int = 0xFF,
    plane_on_off: int = 0,
) -> bytes:
    """Build a 20-byte Image header.

    Layout (big-endian):
      0-1:   ig_LeftEdge
      2-3:   ig_TopEdge
      4-5:   ig_Width
      6-7:   ig_Height
      8-9:   ig_Depth
      10-13: ig_ImageData (ptr)
      14:    ig_PlanePick
      15:    ig_PlaneOnOff
      16-19: ig_NextImage (ptr)
    """
    ihdr = bytearray(20)
    struct.pack_into(">h", ihdr, 4, width)
    struct.pack_into(">h", ihdr, 6, height)
    struct.pack_into(">h", ihdr, 8, depth)
    ihdr[14] = plane_pick
    ihdr[15] = plane_on_off
    return bytes(ihdr)


def _planar_data_size(width: int, height: int, depth: int) -> int:
    """Calculate the byte size of planar image data."""
    row_words = (width + 15) // 16
    row_bytes = row_words * 2
    return depth * row_bytes * height


def _build_traditional_icon(
    *,
    img_width: int = 1,
    img_height: int = 1,
    img_depth: int = 1,
    plane_pick: int = 0xFF,
    plane_on_off: int = 0,
    plane_data: Optional[bytes] = None,
    user_data: int = 0,
    gd_flags: int = 0,
) -> bytes:
    """Build a complete Traditional icon (DiskObject + Image header + plane data).

    The data is constructed so it does NOT trigger GlowIcons (no FORM chunk)
    or NewIcons (no ToolTypes with im1=/im2=) parsing.
    """
    do_hdr = _make_diskobject_header(
        gd_width=img_width,
        gd_height=img_height,
        user_data=user_data,
        gd_flags=gd_flags,
        # no default tool, no tooltypes, no drawer data
    )
    img_hdr = _make_image_header(
        width=img_width,
        height=img_height,
        depth=img_depth,
        plane_pick=plane_pick,
        plane_on_off=plane_on_off,
    )
    if plane_data is None:
        size = _planar_data_size(img_width, img_height, img_depth)
        plane_data = bytes(size)

    return do_hdr + img_hdr + plane_data


# ---------------------------------------------------------------------------
# A. Magic number and basic validation (3 tests)
# ---------------------------------------------------------------------------


class TestMagicAndValidation:
    """Tests for basic parsing validation."""

    def test_parse_rejects_short_data(self):
        """Data shorter than 78 bytes returns None."""
        parser = IconParser(debug=False)
        assert parser.parse(b"\x00" * 77) is None
        assert parser.parse(b"") is None

    def test_parse_rejects_bad_magic(self):
        """Wrong magic (not 0xE310) returns None."""
        parser = IconParser(debug=False)
        data = bytearray(78)
        struct.pack_into(">H", data, 0, 0xDEAD)
        assert parser.parse(bytes(data)) is None

    def test_parse_returns_dict_with_required_keys(self):
        """Valid Traditional icon returns dict with required keys."""
        parser = IconParser(debug=False)
        data = _build_traditional_icon(img_width=1, img_height=1, img_depth=1)
        result = parser.parse(data)
        assert result is not None
        assert set(result.keys()) >= {"width", "height", "rgba", "format", "aspect_ratio"}


# ---------------------------------------------------------------------------
# B. Traditional icon parsing (5 tests)
# ---------------------------------------------------------------------------


class TestTraditionalParsing:
    """Tests for Traditional Amiga icon parsing."""

    def test_traditional_minimal_1x1(self):
        """Minimal 1-plane 1x1 Traditional icon parses correctly.

        A 1x1 icon with 1 bitplane: row is padded to 2 bytes (word boundary).
        All bits zero -> pixel index 0 -> color 0 from WB13_PALETTE (blue).
        Since the single pixel is an edge pixel and color 0,
        _find_edge_background makes it transparent (alpha=0).
        """
        parser = IconParser(debug=False)
        # 1 plane, 1x1: row_words=1, row_bytes=2, plane_size=2
        plane_data = b"\x00\x00"  # pixel index 0
        data = _build_traditional_icon(
            img_width=1, img_height=1, img_depth=1,
            plane_data=plane_data, user_data=0,
        )
        result = parser.parse(data)
        assert result is not None
        assert result["format"] == "traditional"
        assert result["width"] == 1
        assert result["height"] == 1
        # Pixel is color 0 at edge -> transparent
        r, g, b, a = result["rgba"][0], result["rgba"][1], result["rgba"][2], result["rgba"][3]
        assert a == 0  # edge color-0 pixel is transparent

    def test_traditional_2plane_2x2(self):
        """2-plane 2x2 icon verifies planar-to-chunky conversion.

        Plane 0 data (2 bytes per row, 2 rows):
          row 0: 0b10000000 0b00000000  -> bit7=1, bit6=0 for pixels (0,0) and (1,0)
          row 1: 0b00000000 0b00000000  -> bit7=0, bit6=0
        Plane 1 data (2 bytes per row, 2 rows):
          row 0: 0b11000000 0b00000000  -> bit7=1, bit6=1
          row 1: 0b01000000 0b00000000  -> bit7=0, bit6=1

        Pixel indices (plane1_bit << 1 | plane0_bit):
          (0,0): plane0=1, plane1=1 -> 0b11 = 3
          (1,0): plane0=0, plane1=1 -> 0b10 = 2
          (0,1): plane0=0, plane1=0 -> 0b00 = 0
          (1,1): plane0=0, plane1=1 -> 0b10 = 2
        """
        parser = IconParser(debug=False)
        # 2x2, 2 planes: row_words=1, row_bytes=2, plane_size=4
        plane0 = bytes([0b10000000, 0x00, 0b00000000, 0x00])
        plane1 = bytes([0b11000000, 0x00, 0b01000000, 0x00])
        plane_data = plane0 + plane1

        data = _build_traditional_icon(
            img_width=2, img_height=2, img_depth=2,
            plane_data=plane_data, user_data=0,
        )
        result = parser.parse(data)
        assert result is not None
        assert result["format"] == "traditional"
        rgba = result["rgba"]

        # Pixel (0,0): index 3 -> WB13 orange (0xFF, 0x88, 0x00), alpha=255
        assert (rgba[0], rgba[1], rgba[2], rgba[3]) == (0xFF, 0x88, 0x00, 255)
        # Pixel (1,0): index 2 -> WB13 black (0x00, 0x00, 0x22), alpha=255
        assert (rgba[4], rgba[5], rgba[6], rgba[7]) == (0x00, 0x00, 0x22, 255)
        # Pixel (0,1): index 0 -> edge color-0 -> transparent
        assert rgba[11] == 0  # alpha
        # Pixel (1,1): index 2 -> WB13 black (0x00, 0x00, 0x22), alpha=255
        assert (rgba[12], rgba[13], rgba[14], rgba[15]) == (0x00, 0x00, 0x22, 255)

    def test_traditional_wb13_palette(self):
        """UserData=0 selects WB13_PALETTE; color 1 = white (0xFF, 0xFF, 0xFF)."""
        parser = IconParser(debug=False)
        # 1x1, 1 plane, pixel bit set -> index 1
        plane_data = bytes([0b10000000, 0x00])
        data = _build_traditional_icon(
            img_width=1, img_height=1, img_depth=1,
            plane_data=plane_data, user_data=0,
        )
        result = parser.parse(data)
        assert result is not None
        rgba = result["rgba"]
        # Index 1 = WB13 white
        assert (rgba[0], rgba[1], rgba[2]) == (0xFF, 0xFF, 0xFF)
        assert rgba[3] == 255

    def test_traditional_wb20_palette(self):
        """UserData!=0 selects WB20_PALETTE.

        Color 1 = black (0x00, 0x00, 0x00) in WB20.
        Use a pixel with index 1 to verify palette selection without
        edge-transparency complications.
        """
        parser = IconParser(debug=False)
        # 1x1, 1 plane, pixel bit set -> index 1
        plane_data = bytes([0b10000000, 0x00])
        data = _build_traditional_icon(
            img_width=1, img_height=1, img_depth=1,
            plane_data=plane_data, user_data=1,  # non-zero -> WB20
        )
        result = parser.parse(data)
        assert result is not None
        rgba = result["rgba"]
        # Index 1 = WB20 black
        assert (rgba[0], rgba[1], rgba[2]) == (0x00, 0x00, 0x00)
        assert rgba[3] == 255

    def test_traditional_aspect_ratio(self):
        """Traditional icons return aspect_ratio=2.0."""
        parser = IconParser(debug=False)
        data = _build_traditional_icon()
        result = parser.parse(data)
        assert result is not None
        assert result["aspect_ratio"] == 2.0


# ---------------------------------------------------------------------------
# C. Planar-to-chunky conversion (2 tests)
# ---------------------------------------------------------------------------


class TestPlanarToChunky:
    """Tests for _planar_to_chunky internal method."""

    def test_planar_to_chunky_single_plane(self):
        """Single bitplane produces pixel indices 0 or 1."""
        parser = IconParser(debug=False)
        # 8x1, 1 plane: row_words=1, row_bytes=2
        # Byte pattern: 0b10101010 -> pixels [1,0,1,0,1,0,1,0]
        plane_data = bytes([0b10101010, 0x00])
        pixels = parser._planar_to_chunky(
            plane_data, width=8, height=1, depth=1,
            plane_pick=0xFF, plane_on_off=0,
        )
        assert pixels == [1, 0, 1, 0, 1, 0, 1, 0]
        assert all(p in (0, 1) for p in pixels)

    def test_planar_to_chunky_plane_pick_on_off(self):
        """plane_pick and plane_on_off interaction.

        2 planes, but plane_pick=0b01 (only plane 0 from data).
        plane_on_off=0b10 (force plane 1 always ON).

        Plane 0 data: 0b10000000 0x00 -> pixel 0 has bit0=1
        Plane 1 data: present but not picked.

        For pixel 0: plane0 from data=1, plane1 forced on=1 -> index=0b11=3
        For pixel 1: plane0 from data=0, plane1 forced on=1 -> index=0b10=2
        """
        parser = IconParser(debug=False)
        # 2x1, 2 planes: row_bytes=2, plane_size=2
        plane0 = bytes([0b10000000, 0x00])
        plane1 = bytes([0b00000000, 0x00])  # data ignored since not picked
        plane_data = plane0 + plane1

        pixels = parser._planar_to_chunky(
            plane_data, width=2, height=1, depth=2,
            plane_pick=0b01,   # only pick plane 0
            plane_on_off=0b10,  # force plane 1 on
        )
        assert pixels[0] == 3  # plane0=1 (from data) | plane1=1 (forced) = 0b11
        assert pixels[1] == 2  # plane0=0 (from data) | plane1=1 (forced) = 0b10


# ---------------------------------------------------------------------------
# D. RLE decompression (3 tests)
# ---------------------------------------------------------------------------


class TestRLEDecompression:
    """Tests for _unpack_rle_8bit internal method."""

    def test_unpack_rle_8bit_literal(self):
        """Literal run: control byte n < 128 copies n+1 bytes.

        n=2 -> copy 3 bytes.
        """
        parser = IconParser(debug=False)
        data = bytes([2, 0xAA, 0xBB, 0xCC])
        result = parser._unpack_rle_8bit(data, expected_size=3)
        assert result == bytes([0xAA, 0xBB, 0xCC])

    def test_unpack_rle_8bit_replicate(self):
        """Replicate run: control byte n >= 128, repeat next byte (256-n)+1 times.

        n=0xFE (254) -> (256-254)+1 = 3 repeats.
        """
        parser = IconParser(debug=False)
        data = bytes([0xFE, 0x42])
        result = parser._unpack_rle_8bit(data, expected_size=3)
        assert result == bytes([0x42, 0x42, 0x42])

    def test_unpack_rle_8bit_mixed(self):
        """Mixed literal + replicate runs.

        Literal: n=1 -> copy 2 bytes [0x11, 0x22]
        Replicate: n=0xFD (253) -> (256-253)+1 = 4 repeats of 0x33
        Total: [0x11, 0x22, 0x33, 0x33, 0x33, 0x33]
        """
        parser = IconParser(debug=False)
        data = bytes([1, 0x11, 0x22, 0xFD, 0x33])
        result = parser._unpack_rle_8bit(data, expected_size=6)
        assert result == bytes([0x11, 0x22, 0x33, 0x33, 0x33, 0x33])


# ---------------------------------------------------------------------------
# E. _find_edge_background isolation (1 test)
# ---------------------------------------------------------------------------


class TestFindEdgeBackground:
    """Tests for _find_edge_background flood-fill logic."""

    def test_find_edge_background_flood_fill(self):
        """Verify edge-connected vs interior color-0 pixel transparency.

        5x5 grid:
          0 0 0 0 0
          0 1 1 1 0
          0 1 0 1 0
          0 1 1 1 0
          0 0 0 0 0

        All border color-0 pixels (row 0, row 4, col 0, col 4) should be
        marked transparent. The interior color-0 pixel at (2,2) is
        fully surrounded by color-1 and NOT connected to the edge,
        so it should NOT be marked transparent.
        """
        parser = IconParser(debug=False)
        pixels = [
            0, 0, 0, 0, 0,
            0, 1, 1, 1, 0,
            0, 1, 0, 1, 0,
            0, 1, 1, 1, 0,
            0, 0, 0, 0, 0,
        ]
        result = parser._find_edge_background(pixels, width=5, height=5)

        # All edge-connected color-0 pixels should be True
        edge_positions = [
            0, 1, 2, 3, 4,   # top row
            5, 9,             # left/right of row 1
            10, 14,           # left/right of row 2
            15, 19,           # left/right of row 3
            20, 21, 22, 23, 24,  # bottom row
        ]
        for pos in edge_positions:
            assert result[pos] is True, f"pixel {pos} should be edge-transparent"

        # Interior color-0 pixel at (2,2) -> index 12
        assert result[12] is False, "interior color-0 pixel should NOT be transparent"

        # Non-zero pixels should never be marked
        non_zero_positions = [6, 7, 8, 11, 13, 16, 17, 18]
        for pos in non_zero_positions:
            assert result[pos] is False, f"non-zero pixel {pos} should not be marked"


# ---------------------------------------------------------------------------
# F. GlowIcons parsing (3 tests)
# ---------------------------------------------------------------------------


def _make_glowicons_data(
    *,
    face_width: int = 2,
    face_height: int = 2,
    extra_form_before: bytes = b"",
) -> bytes:
    """Build a minimal GlowIcons icon (DiskObject + FORM ICON with FACE + IMAG).

    The IMAG chunk contains uncompressed image data and an uncompressed
    colormap (2 colors: black + white).
    """
    # --- Build FACE chunk (4 bytes minimum) ---
    face_data = bytes([
        face_width - 1,   # width - 1
        face_height - 1,  # height - 1
        0x01,             # flags: bit0 = transparency
        0x00,             # aspect
    ])
    face_chunk = b"FACE" + struct.pack(">I", len(face_data)) + face_data

    # --- Build IMAG chunk ---
    num_pixels = face_width * face_height
    num_colors = 2
    # Image data: uncompressed, all pixels index 0
    image_data = bytes([0] * num_pixels)
    # Colormap: 2 colors, uncompressed (black, white)
    colormap_data = bytes([0, 0, 0, 255, 255, 255])

    imag_header = bytes([
        0,              # transparent_color
        num_colors - 1,  # num_colors - 1
        0x01,           # flags: bit0 = transparency
        0,              # image_compressed = False
        0,              # colormap_compressed = False
        8,              # depth (8 bits per pixel)
    ])
    imag_header += struct.pack(">H", len(image_data) - 1)    # image_size - 1
    imag_header += struct.pack(">H", len(colormap_data) - 1)  # colormap_size - 1

    imag_data = imag_header + image_data + colormap_data
    imag_chunk = b"IMAG" + struct.pack(">I", len(imag_data)) + imag_data

    # --- Build FORM ICON container ---
    form_body = face_chunk + imag_chunk
    form_chunk = b"FORM" + struct.pack(">I", len(form_body) + 4) + b"ICON" + form_body

    # --- DiskObject header (78 bytes) ---
    do_hdr = _make_diskobject_header(
        gd_width=face_width, gd_height=face_height,
    )

    # Construct a minimal image header + plane data so the DiskObject is
    # "valid" enough for the parser to scan past it. We need the first image
    # header after the 78-byte header so _parse_tooltypes can skip it (though
    # for GlowIcons this is reached via _try_glowicons scanning for FORM).
    img_hdr = _make_image_header(width=face_width, height=face_height, depth=1)
    plane_size = _planar_data_size(face_width, face_height, 1)
    plane_data = bytes(plane_size)

    return do_hdr + img_hdr + plane_data + extra_form_before + form_chunk


class TestGlowIconsParsing:
    """Tests for GlowIcons (FORM ICON) parsing."""

    def test_glowicons_form_icon_detection(self):
        """GlowIcons FORM ICON chunk is detected and parsed correctly."""
        parser = IconParser(debug=False)
        data = _make_glowicons_data(face_width=2, face_height=2)
        result = parser.parse(data)
        assert result is not None
        assert result["format"] == "glowicons"
        assert result["width"] == 2
        assert result["height"] == 2

    def test_glowicons_skips_non_icon_form(self):
        """FORM + non-ICON type is skipped; FORM ICON later is found.

        Insert a fake FORM ABCD before the real FORM ICON.
        """
        parser = IconParser(debug=False)
        # A fake FORM chunk with type "ABCD"
        fake_form = b"FORM" + struct.pack(">I", 4) + b"ABCD"
        data = _make_glowicons_data(
            face_width=2, face_height=2,
            extra_form_before=fake_form,
        )
        result = parser.parse(data)
        assert result is not None
        assert result["format"] == "glowicons"

    def test_glowicons_aspect_ratio(self):
        """GlowIcons return aspect_ratio=1.0."""
        parser = IconParser(debug=False)
        data = _make_glowicons_data(face_width=4, face_height=4)
        result = parser.parse(data)
        assert result is not None
        assert result["aspect_ratio"] == 1.0


# ---------------------------------------------------------------------------
# G. ARGB chunk parsing (1 test)
# ---------------------------------------------------------------------------


class TestARGBChunkParsing:
    """Tests for _parse_argb_chunk method."""

    def test_argb_chunk_conversion(self):
        """Verify ARGB-to-RGBA byte reordering.

        Input (ARGB):  A=0x80, R=0x11, G=0x22, B=0x33
        Output (RGBA): R=0x11, G=0x22, B=0x33, A=0x80
        """
        parser = IconParser(debug=False)
        # 1x1 ARGB chunk: 2-byte width + 2-byte height + 4 bytes ARGB
        chunk_data = struct.pack(">HH", 1, 1) + bytes([0x80, 0x11, 0x22, 0x33])
        result = parser._parse_argb_chunk(chunk_data)
        assert result is not None
        assert result["width"] == 1
        assert result["height"] == 1
        rgba = result["rgba"]
        assert (rgba[0], rgba[1], rgba[2], rgba[3]) == (0x11, 0x22, 0x33, 0x80)


# ---------------------------------------------------------------------------
# H. Module-level utility functions (5 tests)
# ---------------------------------------------------------------------------


class TestModuleLevelUtilities:
    """Tests for module-level utility functions."""

    def test_scale_image_identity(self):
        """scale_image with src==dst dimensions returns identical data."""
        # 2x2 RGBA image: red, green, blue, white
        rgba = bytes([
            255, 0, 0, 255,    # red
            0, 255, 0, 255,    # green
            0, 0, 255, 255,    # blue
            255, 255, 255, 255,  # white
        ])
        result = scale_image(rgba, 2, 2, 2, 2)
        assert result == rgba

    def test_scale_image_fit_centers(self):
        """scale_image_fit with non-square source into square destination.

        A 2x1 source scaled into a 4x4 destination should be centered
        with transparent padding on top and bottom.
        """
        # 2x1 source: two opaque red pixels
        rgba = bytes([255, 0, 0, 255, 255, 0, 0, 255])
        result = scale_image_fit(rgba, 2, 1, 4, 4)

        # Result is 4x4 = 64 bytes
        assert len(result) == 4 * 4 * 4

        # The scaled image should be 4x2 (scale factor=2), centered vertically
        # Offset_y = (4 - 2) // 2 = 1, so row 0 and row 3 should be transparent
        # Row 0: all transparent
        for x in range(4):
            idx = x * 4
            assert result[idx + 3] == 0, f"row 0, col {x} should be transparent"
        # Row 3: all transparent
        for x in range(4):
            idx = (3 * 4 + x) * 4
            assert result[idx + 3] == 0, f"row 3, col {x} should be transparent"

        # Middle rows (1-2) should have opaque pixels
        for y in [1, 2]:
            for x in range(4):
                idx = (y * 4 + x) * 4
                assert result[idx + 3] == 255, f"row {y}, col {x} should be opaque"

    def test_encode_png_valid_header(self):
        """encode_png output starts with the PNG signature."""
        # 1x1 red pixel
        rgba = bytes([255, 0, 0, 255])
        result = encode_png(rgba, 1, 1)
        assert result[:8] == b'\x89PNG\r\n\x1a\n'

    def test_build_icns_valid_header(self):
        """build_icns output starts with 'icns' magic and has correct total size."""
        entries = [
            (b'icp4', b'\x00' * 10),
            (b'icp5', b'\x00' * 20),
        ]
        result = build_icns(entries)
        # Header: 'icns' + 4-byte total size
        assert result[:4] == b'icns'
        total_size = struct.unpack(">I", result[4:8])[0]
        assert total_size == len(result)
        # Expected: 8 (header) + (8+10) + (8+20) = 54
        assert total_size == 8 + (8 + 10) + (8 + 20)

    def test_create_icns_smoke(self):
        """create_icns with a small RGBA buffer produces output starting with 'icns'.

        Smoke test exercising the create_icns -> scale_image_fit -> build_icns pipeline.
        """
        # 4x4 opaque red image
        width, height = 4, 4
        rgba = bytes([255, 0, 0, 255] * (width * height))
        result = create_icns(rgba, width, height, debug=False, aspect_ratio=2.0)
        assert result[:4] == b'icns'
        # Should have valid total size
        total_size = struct.unpack(">I", result[4:8])[0]
        assert total_size == len(result)
        assert total_size > 8  # Should contain at least one entry
