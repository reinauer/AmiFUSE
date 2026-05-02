"""Windows shell integration: context menu verbs and file type icons."""

from __future__ import annotations

import logging
import os
import struct
import sys
import zlib
from pathlib import Path

logger = logging.getLogger(__name__)

EXTENSIONS = {".hdf": "AmiFUSE.DiskImage", ".adf": "AmiFUSE.FloppyImage"}

PROGID_DESCRIPTIONS = {
    "AmiFUSE.DiskImage": "Amiga Disk Image",
    "AmiFUSE.FloppyImage": "Amiga Floppy Image",
}

ICON_DIR = Path(os.environ.get("APPDATA", "")) / "AmiFUSE" / "icons"
_AMIFUSE_DIR = Path(os.environ.get("APPDATA", "")) / "AmiFUSE"
_LAUNCH_VBS = _AMIFUSE_DIR / "launch.vbs"

# VBScript launcher: runs a command with hidden window and no wait,
# avoiding the cmd.exe flash that ``cmd /c start`` causes.
_LAUNCH_VBS_CONTENT = '''\
Set a = WScript.Arguments
cmd = ""
For i = 0 To a.Count - 1
    If i > 0 Then cmd = cmd & " "
    cmd = cmd & """" & a(i) & """"
Next
CreateObject("WScript.Shell").Run cmd, 0, False
'''


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register(extensions: list[str] | None = None) -> None:
    """Register file associations and context menu verbs for AmiFUSE."""
    if not sys.platform.startswith("win"):
        raise SystemExit("Shell registration is only supported on Windows.")

    import winreg

    exts = _resolve_extensions(extensions)
    launcher = _get_launcher_path()

    for ext in exts:
        progid = EXTENSIONS[ext]
        _register_extension(winreg, ext, progid)
        logger.info("Registered %s -> %s", ext, progid)

    registered_progids: set[str] = set()
    for ext in exts:
        progid = EXTENSIONS[ext]
        if progid not in registered_progids:
            _register_progid(winreg, progid, launcher)
            registered_progids.add(progid)

    _install_icons()
    _notify_shell_change()

    for ext in exts:
        print(f"Registered {ext} with AmiFUSE context menu.")


def unregister(extensions: list[str] | None = None) -> None:
    """Remove AmiFUSE file associations and context menu verbs."""
    if not sys.platform.startswith("win"):
        raise SystemExit("Shell registration is only supported on Windows.")

    import winreg

    exts = _resolve_extensions(extensions)

    removed_progids: set[str] = set()
    for ext in exts:
        progid = EXTENSIONS[ext]
        _unregister_extension(winreg, ext, progid)
        if progid not in removed_progids:
            _delete_key_recursive(
                winreg.HKEY_CURRENT_USER, rf"Software\Classes\{progid}"
            )
            removed_progids.add(progid)
        logger.info("Unregistered %s", ext)

    _remove_icons()
    _notify_shell_change()

    for ext in exts:
        print(f"Unregistered {ext} from AmiFUSE.")


def is_registered() -> bool:
    """Return True if AmiFUSE is registered for .hdf files."""
    if not sys.platform.startswith("win"):
        return False
    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Classes\.hdf\OpenWithProgids",
        )
        try:
            winreg.QueryValueEx(key, "AmiFUSE.DiskImage")
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_extensions(extensions: list[str] | None) -> list[str]:
    if extensions is None:
        return list(EXTENSIONS)
    for ext in extensions:
        if ext not in EXTENSIONS:
            raise ValueError(f"Unknown extension: {ext}")
    return extensions


def _get_launcher_path() -> str:
    return str(Path(sys.executable).parent / "amifuse-launcher.exe")


def _register_extension(winreg, ext: str, progid: str) -> None:
    """Register a single extension under HKCU\\Software\\Classes."""
    ext_path = rf"Software\Classes\{ext}"

    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, ext_path)
    try:
        existing = ""
        try:
            existing, _ = winreg.QueryValueEx(key, "")
        except FileNotFoundError:
            pass

        if existing and existing != progid:
            logger.warning(
                "%s already claimed by %s; adding to OpenWithProgids only",
                ext,
                existing,
            )
            print(
                f"Warning: {ext} is already associated with {existing}. "
                f"Adding AmiFUSE to Open With list without overriding."
            )
        else:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, progid)
    finally:
        winreg.CloseKey(key)

    # Always add to OpenWithProgids
    owp_key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{ext_path}\OpenWithProgids")
    try:
        winreg.SetValueEx(owp_key, progid, 0, winreg.REG_SZ, "")
    finally:
        winreg.CloseKey(owp_key)


def _register_progid(winreg, progid: str, launcher: str) -> None:
    """Create ProgID with flat verb entries and icon."""
    base = rf"Software\Classes\{progid}"
    description = PROGID_DESCRIPTIONS[progid]

    # (Default) = description
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, base)
    try:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, description)
    finally:
        winreg.CloseKey(key)

    # Flat verb: mount — wscript runs VBS launcher invisibly (no cmd.exe flash)
    vbs = str(_LAUNCH_VBS)
    _set_verb(
        winreg,
        base,
        "mount",
        "Mount with AmiFUSE",
        f'wscript.exe //nologo //b "{vbs}" "{launcher}" mount "%1"',
    )

    # Flat verb: mountrw
    _set_verb(
        winreg,
        base,
        "mountrw",
        "Mount Read-Write with AmiFUSE",
        f'wscript.exe //nologo //b "{vbs}" "{launcher}" mount --write "%1"',
    )

    # DefaultIcon
    icon_name = progid.split(".")[-1].lower() + ".ico"
    icon_path = str(ICON_DIR / icon_name)
    icon_key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{base}\DefaultIcon")
    try:
        winreg.SetValueEx(icon_key, "", 0, winreg.REG_SZ, icon_path)
    finally:
        winreg.CloseKey(icon_key)


def _set_verb(winreg, base: str, verb: str, label: str, command: str) -> None:
    verb_key = winreg.CreateKey(
        winreg.HKEY_CURRENT_USER, rf"{base}\shell\{verb}"
    )
    try:
        winreg.SetValueEx(verb_key, "", 0, winreg.REG_SZ, label)
    finally:
        winreg.CloseKey(verb_key)

    cmd_key = winreg.CreateKey(
        winreg.HKEY_CURRENT_USER, rf"{base}\shell\{verb}\command"
    )
    try:
        winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, command)
    finally:
        winreg.CloseKey(cmd_key)


def _unregister_extension(winreg, ext: str, progid: str) -> None:
    """Remove AmiFUSE entries from a single extension key."""
    ext_path = rf"Software\Classes\{ext}"

    # Remove from OpenWithProgids
    try:
        owp_key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            rf"{ext_path}\OpenWithProgids",
            0,
            winreg.KEY_SET_VALUE | winreg.KEY_READ,
        )
        try:
            try:
                winreg.DeleteValue(owp_key, progid)
            except FileNotFoundError:
                pass

            # If no other ProgIDs remain, delete the key
            try:
                winreg.EnumValue(owp_key, 0)
            except OSError:
                winreg.CloseKey(owp_key)
                owp_key = None
                try:
                    winreg.DeleteKey(
                        winreg.HKEY_CURRENT_USER,
                        rf"{ext_path}\OpenWithProgids",
                    )
                except OSError:
                    pass
        finally:
            if owp_key is not None:
                winreg.CloseKey(owp_key)
    except OSError:
        pass

    # Only clear (Default) if it's ours
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, ext_path, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        )
        try:
            current, _ = winreg.QueryValueEx(key, "")
            if current == progid:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "")
        except FileNotFoundError:
            pass
        finally:
            winreg.CloseKey(key)
    except OSError:
        pass

    # Remove the extension key entirely if it's now empty (no subkeys, no default)
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, ext_path, 0, winreg.KEY_READ)
        try:
            has_subkeys = True
            try:
                winreg.EnumKey(key, 0)
            except OSError:
                has_subkeys = False

            default_empty = True
            try:
                val, _ = winreg.QueryValueEx(key, "")
                if val:
                    default_empty = False
            except OSError:
                pass  # No default value — counts as empty

            if not has_subkeys and default_empty:
                winreg.CloseKey(key)
                key = None
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, ext_path)
        finally:
            if key is not None:
                winreg.CloseKey(key)
    except OSError:
        pass


def _delete_key_recursive(hkey, sub_key: str) -> None:
    """Recursively delete a registry key and all its children."""
    import winreg

    try:
        key = winreg.OpenKey(hkey, sub_key, 0, winreg.KEY_READ)
    except FileNotFoundError:
        return
    except OSError:
        return

    children = []
    try:
        i = 0
        while True:
            try:
                children.append(winreg.EnumKey(key, i))
                i += 1
            except OSError:
                break
    finally:
        winreg.CloseKey(key)

    for child in children:
        _delete_key_recursive(hkey, rf"{sub_key}\{child}")

    try:
        winreg.DeleteKey(hkey, sub_key)
    except OSError as exc:
        logger.warning("Failed to delete registry key %s: %s", sub_key, exc)


# ---------------------------------------------------------------------------
# Icons — pretty-file-icons style page icons for HDF and ADF, plus tray icon
# ---------------------------------------------------------------------------

# Shared page template colors
_PAGE_FILL = (233, 233, 224, 255)       # warm off-white (#E9E9E0)
_DOG_EAR_FILL = (217, 215, 202, 255)   # slightly darker warm gray (#D9D7CA)
_PAGE_OUTLINE = (180, 178, 168, 255)    # medium gray for definition
_BANNER_TEXT = (255, 255, 255, 255)     # white

# Banner colors per type
_ADF_BANNER = (60, 179, 113, 255)       # medium sea green (#3CB371)
_HDF_BANNER = (212, 160, 23, 255)       # amber/gold (#D4A017)

# Tray icon color
_TRAY_COLOR = (212, 160, 23, 255)       # amber/gold

T = tuple[int, int, int, int]  # RGBA pixel type alias


def _new_canvas(size: int) -> list[list[T]]:
    """Create a size x size transparent canvas (RGBA)."""
    return [[(0, 0, 0, 0)] * size for _ in range(size)]


def _fill_rect(canvas: list[list[T]], x0: int, y0: int, x1: int, y1: int, color: T) -> None:
    """Fill a rectangle [x0,x1) x [y0,y1) with color, alpha-blending over existing."""
    h = len(canvas)
    w = len(canvas[0]) if h else 0
    for y in range(max(0, y0), min(h, y1)):
        for x in range(max(0, x0), min(w, x1)):
            canvas[y][x] = _blend(canvas[y][x], color)


def _set_pixel(canvas: list[list[T]], x: int, y: int, color: T) -> None:
    h = len(canvas)
    w = len(canvas[0]) if h else 0
    if 0 <= x < w and 0 <= y < h:
        canvas[y][x] = _blend(canvas[y][x], color)


def _blend(bg: T, fg: T) -> T:
    """Alpha-blend fg over bg (both RGBA tuples)."""
    fa = fg[3] / 255.0
    ba = bg[3] / 255.0
    oa = fa + ba * (1 - fa)
    if oa == 0:
        return (0, 0, 0, 0)
    r = int((fg[0] * fa + bg[0] * ba * (1 - fa)) / oa)
    g = int((fg[1] * fa + bg[1] * ba * (1 - fa)) / oa)
    b = int((fg[2] * fa + bg[2] * ba * (1 - fa)) / oa)
    return (r, g, b, int(oa * 255))


def _draw_outline_rect(canvas: list[list[T]], x0: int, y0: int, x1: int, y1: int, color: T) -> None:
    """Draw a 1px outline rectangle."""
    for x in range(x0, x1):
        _set_pixel(canvas, x, y0, color)
        _set_pixel(canvas, x, y1 - 1, color)
    for y in range(y0, y1):
        _set_pixel(canvas, x0, y, color)
        _set_pixel(canvas, x1 - 1, y, color)


def _draw_filled_circle(canvas: list[list[T]], cx: int, cy: int, r: int, color: T) -> None:
    """Draw a filled circle."""
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                _set_pixel(canvas, cx + dx, cy + dy, color)


# ---------------------------------------------------------------------------
# Bitmap fonts for banner text
# ---------------------------------------------------------------------------

# 3x5 font for 16px icons (tiny, adds texture)
_FONT_3x5: dict[str, list[list[int]]] = {
    'A': [[0,1,0],[1,0,1],[1,1,1],[1,0,1],[1,0,1]],
    'D': [[1,1,0],[1,0,1],[1,0,1],[1,0,1],[1,1,0]],
    'F': [[1,1,1],[1,0,0],[1,1,0],[1,0,0],[1,0,0]],
    'H': [[1,0,1],[1,0,1],[1,1,1],[1,0,1],[1,0,1]],
}

# 4x6 font for 32px icons (readable)
_FONT_4x6: dict[str, list[list[int]]] = {
    'A': [[0,1,1,0],[1,0,0,1],[1,0,0,1],[1,1,1,1],[1,0,0,1],[1,0,0,1]],
    'D': [[1,1,1,0],[1,0,0,1],[1,0,0,1],[1,0,0,1],[1,0,0,1],[1,1,1,0]],
    'F': [[1,1,1,1],[1,0,0,0],[1,1,1,0],[1,0,0,0],[1,0,0,0],[1,0,0,0]],
    'H': [[1,0,0,1],[1,0,0,1],[1,1,1,1],[1,0,0,1],[1,0,0,1],[1,0,0,1]],
}

# 6x8 font for 48px icons (clearly readable)
_FONT_6x8: dict[str, list[list[int]]] = {
    'A': [[0,0,1,1,0,0],[0,1,0,0,1,0],[1,0,0,0,0,1],[1,0,0,0,0,1],[1,1,1,1,1,1],[1,0,0,0,0,1],[1,0,0,0,0,1],[1,0,0,0,0,1]],
    'D': [[1,1,1,1,0,0],[1,0,0,0,1,0],[1,0,0,0,0,1],[1,0,0,0,0,1],[1,0,0,0,0,1],[1,0,0,0,0,1],[1,0,0,0,1,0],[1,1,1,1,0,0]],
    'F': [[1,1,1,1,1,1],[1,0,0,0,0,0],[1,0,0,0,0,0],[1,1,1,1,0,0],[1,0,0,0,0,0],[1,0,0,0,0,0],[1,0,0,0,0,0],[1,0,0,0,0,0]],
    'H': [[1,0,0,0,0,1],[1,0,0,0,0,1],[1,0,0,0,0,1],[1,1,1,1,1,1],[1,0,0,0,0,1],[1,0,0,0,0,1],[1,0,0,0,0,1],[1,0,0,0,0,1]],
}


def _render_text(canvas: list[list[T]], text: str, x: int, y: int,
                 font: dict[str, list[list[int]]], color: T, spacing: int = 1) -> None:
    """Render text on canvas using a bitmap font."""
    cx = x
    for ch in text:
        glyph = font.get(ch)
        if glyph is None:
            continue
        for row_i, row in enumerate(glyph):
            for col_i, bit in enumerate(row):
                if bit:
                    _set_pixel(canvas, cx + col_i, y + row_i, color)
        cx += len(glyph[0]) + spacing


def _text_width(text: str, font: dict[str, list[list[int]]], spacing: int = 1) -> int:
    """Compute pixel width of rendered text."""
    w = 0
    for i, ch in enumerate(text):
        glyph = font.get(ch)
        if glyph is None:
            continue
        w += len(glyph[0])
        if i < len(text) - 1:
            w += spacing
    return w


# ---------------------------------------------------------------------------
# Page icon drawing — shared template
# ---------------------------------------------------------------------------


def _draw_page_icon_16(banner_color: T, text: str) -> list[list[T]]:
    """16x16 pretty-file-icons page icon."""
    canvas = _new_canvas(16)
    # Page: x=[3,13), y=[1,15) -> 10px wide, 14px tall
    px0, py0, px1, py1 = 3, 1, 13, 15
    ear = 2

    # Page fill (excluding dog-ear)
    _fill_rect(canvas, px0, py0, px1 - ear, py1, _PAGE_FILL)
    _fill_rect(canvas, px0, py0 + ear, px1, py1, _PAGE_FILL)

    # Dog-ear fill
    for i in range(ear):
        _fill_rect(canvas, px1 - ear + i + 1, py0 + i, px1, py0 + i + 1, _DOG_EAR_FILL)

    # Page outline
    for y in range(py0, py1):
        _set_pixel(canvas, px0, y, _PAGE_OUTLINE)          # left
    for x in range(px0, px1):
        _set_pixel(canvas, x, py1 - 1, _PAGE_OUTLINE)      # bottom
    for y in range(py0 + ear, py1):
        _set_pixel(canvas, px1 - 1, y, _PAGE_OUTLINE)      # right
    for x in range(px0, px1 - ear):
        _set_pixel(canvas, x, py0, _PAGE_OUTLINE)           # top
    # Dog-ear diagonal and crease
    for i in range(ear + 1):
        _set_pixel(canvas, px1 - ear + i, py0 + i, _PAGE_OUTLINE)
    for x in range(px1 - ear, px1):
        _set_pixel(canvas, x, py0 + ear, _PAGE_OUTLINE)

    # Banner: bottom 4 rows of page
    bx0, by0, bx1, by1 = px0 + 1, py1 - 5, px1 - 1, py1 - 1
    _fill_rect(canvas, bx0, by0, bx1, by1, banner_color)

    # Text (3x5 font, centered in banner)
    font = _FONT_3x5
    tw = _text_width(text, font)
    tx = bx0 + (bx1 - bx0 - tw) // 2
    ty = by0 + (by1 - by0 - 5) // 2
    _render_text(canvas, text, tx, ty, font, _BANNER_TEXT)

    return canvas


def _draw_page_icon_32(banner_color: T, text: str) -> list[list[T]]:
    """32x32 pretty-file-icons page icon."""
    canvas = _new_canvas(32)
    # Page: x=[6,26), y=[2,30) -> 20px wide, 28px tall
    px0, py0, px1, py1 = 6, 2, 26, 30
    ear = 4

    # Page fill
    _fill_rect(canvas, px0, py0, px1 - ear, py1, _PAGE_FILL)
    _fill_rect(canvas, px0, py0 + ear, px1, py1, _PAGE_FILL)

    # Dog-ear fill
    for i in range(ear):
        _fill_rect(canvas, px1 - ear + i + 1, py0 + i, px1, py0 + i + 1, _DOG_EAR_FILL)

    # Outline
    for y in range(py0, py1):
        _set_pixel(canvas, px0, y, _PAGE_OUTLINE)
    for x in range(px0, px1):
        _set_pixel(canvas, x, py1 - 1, _PAGE_OUTLINE)
    for y in range(py0 + ear, py1):
        _set_pixel(canvas, px1 - 1, y, _PAGE_OUTLINE)
    for x in range(px0, px1 - ear):
        _set_pixel(canvas, x, py0, _PAGE_OUTLINE)
    for i in range(ear + 1):
        _set_pixel(canvas, px1 - ear + i, py0 + i, _PAGE_OUTLINE)
    for x in range(px1 - ear, px1):
        _set_pixel(canvas, x, py0 + ear, _PAGE_OUTLINE)

    # Banner: bottom 8 rows
    bx0, by0, bx1, by1 = px0 + 1, py1 - 9, px1 - 1, py1 - 1
    _fill_rect(canvas, bx0, by0, bx1, by1, banner_color)

    # Text (4x6 font, centered)
    font = _FONT_4x6
    tw = _text_width(text, font)
    tx = bx0 + (bx1 - bx0 - tw) // 2
    ty = by0 + (by1 - by0 - 6) // 2
    _render_text(canvas, text, tx, ty, font, _BANNER_TEXT)

    return canvas


def _draw_page_icon_48(banner_color: T, text: str) -> list[list[T]]:
    """48x48 pretty-file-icons page icon."""
    canvas = _new_canvas(48)
    # Page: x=[9,39), y=[3,45) -> 30px wide, 42px tall
    px0, py0, px1, py1 = 9, 3, 39, 45
    ear = 7

    # Page fill
    _fill_rect(canvas, px0, py0, px1 - ear, py1, _PAGE_FILL)
    _fill_rect(canvas, px0, py0 + ear, px1, py1, _PAGE_FILL)

    # Dog-ear fill
    for i in range(ear):
        _fill_rect(canvas, px1 - ear + i + 1, py0 + i, px1, py0 + i + 1, _DOG_EAR_FILL)

    # Outline
    for y in range(py0, py1):
        _set_pixel(canvas, px0, y, _PAGE_OUTLINE)
    for x in range(px0, px1):
        _set_pixel(canvas, x, py1 - 1, _PAGE_OUTLINE)
    for y in range(py0 + ear, py1):
        _set_pixel(canvas, px1 - 1, y, _PAGE_OUTLINE)
    for x in range(px0, px1 - ear):
        _set_pixel(canvas, x, py0, _PAGE_OUTLINE)
    for i in range(ear + 1):
        _set_pixel(canvas, px1 - ear + i, py0 + i, _PAGE_OUTLINE)
    for x in range(px1 - ear, px1):
        _set_pixel(canvas, x, py0 + ear, _PAGE_OUTLINE)

    # Banner: bottom 12 rows
    bx0, by0, bx1, by1 = px0 + 1, py1 - 13, px1 - 1, py1 - 1
    _fill_rect(canvas, bx0, by0, bx1, by1, banner_color)

    # Text (6x8 font, centered)
    font = _FONT_6x8
    tw = _text_width(text, font)
    tx = bx0 + (bx1 - bx0 - tw) // 2
    ty = by0 + (by1 - by0 - 8) // 2
    _render_text(canvas, text, tx, ty, font, _BANNER_TEXT)

    return canvas


def _draw_page_icon_256(banner_color: T, text: str, content_color: T | None = None) -> list[list[T]]:
    """256x256 pretty-file-icons page icon with optional content illustration."""
    canvas = _new_canvas(256)
    # Page: x=[43,213), y=[15,240) -> 170px wide, 225px tall
    px0, py0, px1, py1 = 43, 15, 213, 240
    ear = 35

    # Rounded-corner page fill
    _fill_rect(canvas, px0 + 4, py0, px1 - ear, py0 + 4, _PAGE_FILL)  # top strip
    _fill_rect(canvas, px0, py0 + 4, px1 - ear, py1 - 4, _PAGE_FILL)  # main body left
    _fill_rect(canvas, px0, py0 + ear, px1, py1 - 4, _PAGE_FILL)      # main body full
    _fill_rect(canvas, px0 + 4, py1 - 4, px1 - 4, py1, _PAGE_FILL)   # bottom strip

    # Dog-ear fill
    for i in range(ear):
        _fill_rect(canvas, px1 - ear + i + 1, py0 + i, px1, py0 + i + 1, _DOG_EAR_FILL)

    # Outline — left
    for y in range(py0 + 4, py1 - 4):
        _set_pixel(canvas, px0, y, _PAGE_OUTLINE)
    # Outline — bottom
    for x in range(px0 + 4, px1 - 4):
        _set_pixel(canvas, x, py1 - 1, _PAGE_OUTLINE)
    # Outline — right (below ear)
    for y in range(py0 + ear, py1 - 4):
        _set_pixel(canvas, px1 - 1, y, _PAGE_OUTLINE)
    # Outline — top (before ear)
    for x in range(px0 + 4, px1 - ear):
        _set_pixel(canvas, x, py0, _PAGE_OUTLINE)
    # Dog-ear diagonal
    for i in range(ear + 1):
        _set_pixel(canvas, px1 - ear + i, py0 + i, _PAGE_OUTLINE)
    # Dog-ear horizontal crease
    for x in range(px1 - ear, px1):
        _set_pixel(canvas, x, py0 + ear, _PAGE_OUTLINE)
    # Rounded corners (small arcs)
    for dx, dy in [(1,3),(2,2),(3,1)]:
        _set_pixel(canvas, px0 + dx, py0 + dy, _PAGE_OUTLINE)   # top-left
        _set_pixel(canvas, px0 + dx, py1 - 1 - dy, _PAGE_OUTLINE)  # bottom-left
        _set_pixel(canvas, px1 - 1 - dx, py1 - 1 - dy, _PAGE_OUTLINE)  # bottom-right

    # Content illustration lines (above banner, in lighter banner color)
    if content_color:
        for ly in range(py0 + 55, py0 + 130, 16):
            _fill_rect(canvas, px0 + 25, ly, px1 - 30, ly + 3, content_color)

    # Banner: bottom 65 rows
    bx0, by0, bx1, by1 = px0 + 1, py1 - 66, px1 - 1, py1 - 1
    _fill_rect(canvas, bx0, by0, bx1, by1, banner_color)

    # Text — scale up the 6x8 font by 4x for crisp large rendering
    font = _FONT_6x8
    scale = 4
    tw = _text_width(text, font, spacing=1) * scale
    tx = bx0 + (bx1 - bx0 - tw) // 2
    ty = by0 + (by1 - by0 - 8 * scale) // 2
    for i, ch in enumerate(text):
        glyph = font.get(ch)
        if glyph is None:
            continue
        for row_i, row in enumerate(glyph):
            for col_i, bit in enumerate(row):
                if bit:
                    _fill_rect(canvas, tx + col_i * scale, ty + row_i * scale,
                               tx + col_i * scale + scale, ty + row_i * scale + scale,
                               _BANNER_TEXT)
        tx += (len(glyph[0]) + 1) * scale

    return canvas


# ---------------------------------------------------------------------------
# Tray icon — amber eject symbol (triangle + bar)
# ---------------------------------------------------------------------------


def _draw_tray_16() -> list[list[T]]:
    """16x16 tray icon: 'AF' text in a rounded box."""
    canvas = _new_canvas(16)
    bg = (245, 245, 245, 255)
    outline = (60, 60, 60, 220)
    text_color = (180, 130, 10, 255)

    # Fill entire canvas with background
    _fill_rect(canvas, 0, 0, 16, 16, bg)

    # Clip 4 corner pixels (1px rounded corners)
    transparent = (0, 0, 0, 0)
    for cx, cy in [(0, 0), (15, 0), (0, 15), (15, 15)]:
        canvas[cy][cx] = transparent

    # 1px outline
    _draw_outline_rect(canvas, 0, 0, 16, 16, outline)
    # Re-clear corners over outline
    for cx, cy in [(0, 0), (15, 0), (0, 15), (15, 15)]:
        canvas[cy][cx] = transparent

    # Bold "A" glyph 6x10 (2px strokes)
    _A_6x10 = [
        [0,0,1,1,0,0],
        [0,1,1,1,1,0],
        [1,1,0,0,1,1],
        [1,1,0,0,1,1],
        [1,1,0,0,1,1],
        [1,1,1,1,1,1],
        [1,1,0,0,1,1],
        [1,1,0,0,1,1],
        [1,1,0,0,1,1],
        [1,1,0,0,1,1],
    ]
    # Bold "F" glyph 6x10 (2px strokes)
    _F_6x10 = [
        [1,1,1,1,1,1],
        [1,1,1,1,1,1],
        [1,1,0,0,0,0],
        [1,1,0,0,0,0],
        [1,1,1,1,1,0],
        [1,1,1,1,1,0],
        [1,1,0,0,0,0],
        [1,1,0,0,0,0],
        [1,1,0,0,0,0],
        [1,1,0,0,0,0],
    ]

    # Position: 13px wide (6+1+6), centered in 14px interior (1px border)
    # x_start = 1 + (14 - 13) // 2 = 1, y_start = 1 + (14 - 10) // 2 = 3
    x0, y0 = 1, 3
    for row_i, row in enumerate(_A_6x10):
        for col_i, bit in enumerate(row):
            if bit:
                _set_pixel(canvas, x0 + col_i, y0 + row_i, text_color)
    x_f = x0 + 6 + 1  # 1px spacing
    for row_i, row in enumerate(_F_6x10):
        for col_i, bit in enumerate(row):
            if bit:
                _set_pixel(canvas, x_f + col_i, y0 + row_i, text_color)

    return canvas


def _draw_tray_32() -> list[list[T]]:
    """32x32 tray icon: 'AF' text in a rounded box."""
    canvas = _new_canvas(32)
    bg = (245, 245, 245, 255)
    outline = (60, 60, 60, 220)
    text_color = (180, 130, 10, 255)

    # Fill entire canvas with background
    _fill_rect(canvas, 0, 0, 32, 32, bg)

    # Clip 2px rounded corners
    transparent = (0, 0, 0, 0)
    corners = [(0, 0), (1, 0), (0, 1),
               (30, 0), (31, 0), (31, 1),
               (0, 30), (0, 31), (1, 31),
               (30, 31), (31, 31), (31, 30)]
    for cx, cy in corners:
        canvas[cy][cx] = transparent

    # 1px outline
    _draw_outline_rect(canvas, 0, 0, 32, 32, outline)
    # Re-clear corners over outline
    for cx, cy in corners:
        canvas[cy][cx] = transparent

    # Scale 6x10 glyphs by 2x -> 12x20 per letter, 2px spacing = 26px wide
    # Centered in 30px interior: (30-26)//2 = 2, so x_start = 1 + 2 = 3
    # Vertically: (30-20)//2 = 5, so y_start = 1 + 5 = 6
    _A_6x10 = [
        [0,0,1,1,0,0],
        [0,1,1,1,1,0],
        [1,1,0,0,1,1],
        [1,1,0,0,1,1],
        [1,1,0,0,1,1],
        [1,1,1,1,1,1],
        [1,1,0,0,1,1],
        [1,1,0,0,1,1],
        [1,1,0,0,1,1],
        [1,1,0,0,1,1],
    ]
    _F_6x10 = [
        [1,1,1,1,1,1],
        [1,1,1,1,1,1],
        [1,1,0,0,0,0],
        [1,1,0,0,0,0],
        [1,1,1,1,1,0],
        [1,1,1,1,1,0],
        [1,1,0,0,0,0],
        [1,1,0,0,0,0],
        [1,1,0,0,0,0],
        [1,1,0,0,0,0],
    ]

    scale = 2
    x0, y0 = 3, 6
    for glyph, gx in [(_A_6x10, x0), (_F_6x10, x0 + 12 + 2)]:
        for row_i, row in enumerate(glyph):
            for col_i, bit in enumerate(row):
                if bit:
                    _fill_rect(canvas, gx + col_i * scale, y0 + row_i * scale,
                               gx + col_i * scale + scale, y0 + row_i * scale + scale,
                               text_color)

    return canvas


def _canvas_to_bgra(canvas: list[list[T]]) -> bytes:
    """Convert RGBA canvas to BGRA bytes (bottom-up for BMP)."""
    size = len(canvas)
    rows = []
    for y in range(size - 1, -1, -1):  # bottom-up
        for x in range(size):
            r, g, b, a = canvas[y][x]
            rows.append(struct.pack("BBBB", b, g, r, a))
    return b"".join(rows)


def _canvas_to_rgba_topdown(canvas: list[list[T]]) -> bytes:
    """Convert RGBA canvas to RGBA bytes (top-down for PNG)."""
    parts = []
    for row in canvas:
        for r, g, b, a in row:
            parts.append(struct.pack("BBBB", r, g, b, a))
    return b"".join(parts)


def _make_png(canvas: list[list[T]]) -> bytes:
    """Create a PNG file from an RGBA canvas."""
    size = len(canvas)
    # Build raw scanlines: filter byte 0 + RGBA row data
    raw = bytearray()
    for row in canvas:
        raw.append(0)  # filter: None
        for r, g, b, a in row:
            raw.extend((r, g, b, a))
    compressed = zlib.compress(bytes(raw))

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr_data = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    # 8=bit depth, 6=RGBA, 0=compression, 0=filter, 0=interlace

    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr_data)
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )


def _make_bmp_entry(canvas: list[list[T]]) -> bytes:
    """Create a BMP DIB for an ICO entry from an RGBA canvas."""
    size = len(canvas)
    bgra = _canvas_to_bgra(canvas)
    bih = struct.pack(
        "<IiiHHIIiiII",
        40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0,
    )
    mask_row_bytes = (size + 31) // 32 * 4
    mask = b"\x00" * mask_row_bytes * size
    return bih + bgra + mask


def _make_ico(icon_type: str) -> bytes:
    """Generate a multi-resolution ICO file for the given type."""
    if icon_type == "hdf":
        banner, text = _HDF_BANNER, "HDF"
        content_color = (232, 195, 90, 80)  # light amber, semi-transparent
        canvases = {
            16: _draw_page_icon_16(banner, text),
            32: _draw_page_icon_32(banner, text),
            48: _draw_page_icon_48(banner, text),
            256: _draw_page_icon_256(banner, text, content_color),
        }
    elif icon_type == "adf":
        banner, text = _ADF_BANNER, "ADF"
        content_color = (100, 200, 150, 80)  # light green, semi-transparent
        canvases = {
            16: _draw_page_icon_16(banner, text),
            32: _draw_page_icon_32(banner, text),
            48: _draw_page_icon_48(banner, text),
            256: _draw_page_icon_256(banner, text, content_color),
        }
    elif icon_type == "tray":
        canvases = {
            16: _draw_tray_16(),
            32: _draw_tray_32(),
        }
    else:
        raise ValueError(f"Unknown icon type: {icon_type}")

    entries: list[tuple[int, bytes]] = []
    for size in sorted(canvases):
        if size == 256:
            entries.append((size, _make_png(canvases[size])))
        else:
            entries.append((size, _make_bmp_entry(canvases[size])))

    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    directory = b""
    image_data = b""

    for size, data in entries:
        w = size if size < 256 else 0
        h = size if size < 256 else 0
        directory += struct.pack(
            "<BBBBHHII",
            w, h, 0, 0, 1, 32, len(data), offset,
        )
        image_data += data
        offset += len(data)

    return header + directory + image_data


_ICON_SPECS = {
    "diskimage.ico": "hdf",
    "floppyimage.ico": "adf",
    "tray.ico": "tray",
}


def _install_icons() -> None:
    """Write icon files and launcher script to APPDATA."""
    import tempfile

    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for name, icon_type in _ICON_SPECS.items():
        path = ICON_DIR / name
        data = _make_ico(icon_type)
        try:
            path.write_bytes(data)
        except PermissionError:
            # File may be locked by Explorer; write to temp and atomic-rename
            try:
                fd, tmp = tempfile.mkstemp(dir=ICON_DIR, suffix=".ico")
                os.write(fd, data)
                os.close(fd)
                os.replace(tmp, path)
            except OSError as exc:
                logger.warning("Could not update icon %s (locked): %s", path, exc)
                # Clean up temp file if rename failed
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                continue
        logger.info("Installed icon %s", path)
    # VBS launcher for invisible context-menu invocation
    _LAUNCH_VBS.write_text(_LAUNCH_VBS_CONTENT, encoding="utf-8")
    logger.info("Installed launcher %s", _LAUNCH_VBS)


def _remove_icons() -> None:
    """Delete icon files, launcher script, and clean up empty directories."""
    for name in _ICON_SPECS:
        path = ICON_DIR / name
        try:
            path.unlink()
            logger.info("Removed icon %s", path)
        except FileNotFoundError:
            pass
        except (PermissionError, OSError) as exc:
            logger.warning("Could not remove icon %s (locked): %s", path, exc)

    try:
        _LAUNCH_VBS.unlink()
        logger.info("Removed launcher %s", _LAUNCH_VBS)
    except FileNotFoundError:
        pass
    except (PermissionError, OSError) as exc:
        logger.warning("Could not remove launcher %s: %s", _LAUNCH_VBS, exc)

    # Clean up empty dirs (may fail if files remain due to locks)
    for d in (ICON_DIR, ICON_DIR.parent):
        try:
            d.rmdir()
        except OSError:
            break


# ---------------------------------------------------------------------------
# Shell notification
# ---------------------------------------------------------------------------


def _notify_shell_change() -> None:
    """Tell Explorer that file associations have changed."""
    import ctypes

    SHCNE_ASSOCCHANGED = 0x08000000
    SHCNF_IDLIST = 0x0000
    ctypes.windll.shell32.SHChangeNotify(
        SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None
    )
