"""
Filesystem driver lookup for FFS/OFS volumes that carry no driver.

An ADF never carries a filesystem driver, and an RDB may have none for a
partition's DOS type. For DOS0-DOS7 volumes this module picks one, in order:

1. FastFileSystem in the user's drivers folder (DOS0-DOS7), so a user's own
   FFS, e.g. from AmigaOS 3.2, always wins;
2. the FastFileSystem bundled with AmiFUSE, for the DOS types its build
   covers (recorded in its provenance file);
3. the dev fallback, a sibling AmiFUSE-testing checkout (DOS0-DOS7).

It reads no disk image: fuse_fs.resolve_driver() puts --driver and the
driver stored in the RDB in front of this lookup.
"""

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple

BUNDLED_DIR = Path(__file__).parent / "drivers"
# Kept under its release asset name, so the release's digest applies to it.
BUNDLED_DRIVER = BUNDLED_DIR / "FastFileSystem.OS3"
BUNDLED_PROVENANCE = BUNDLED_DIR / "FastFileSystem.OS3.json"
BUNDLED_LICENSE = BUNDLED_DIR / "FastFileSystem.OS3.LICENSE"

# The name of the user's own copy, as in L:FastFileSystem on AmigaOS.
USER_DRIVER_NAME = "FastFileSystem"
# Names the drivers folder in messages when there is no home folder for it.
NO_USER_DRIVER_DIR = "your drivers folder (no home folder found)"

# Development checkouts and CI have AmiFUSE-testing beside AmiFUSE.
DEV_FALLBACK_DRIVER = (
    Path(__file__).parent.parent.parent / "AmiFUSE-testing" / "drivers" / USER_DRIVER_NAME
)

DOS_BASE = 0x444F5300  # "DOS\0"
_LONG_NAME_TAGS = ("DOS6", "DOS7")
_DOS_TAG_RE = re.compile(r"DOS[0-7]\Z")
_VER_RE = re.compile(rb"\$VER:\s*([\x20-\x7e]{1,120})")


@dataclass
class ResolvedDriver:
    """A filesystem driver and where it came from.

    source is one of "explicit" (--driver), "rdb", "user", "bundled" or
    "dev_fallback". description follows "Filesystem driver:" in banners.
    version is the driver's $VER text; tag is the bundled release tag.
    Only a temporary driver (an RDB extraction) is the caller's to delete.
    """

    path: Path
    source: str
    description: str
    temporary: bool = False
    version: Optional[str] = None
    sha256: Optional[str] = None
    tag: Optional[str] = None

    def to_json(self) -> dict:
        """The driver object reported by inspect --json."""
        return {
            "source": self.source,
            "path": str(self.path),
            "version": self.version,
            "tag": self.tag,
            "sha256": self.sha256,
        }


class DriverNotFound(Exception):
    """No driver serves the volume; the message says what to do instead."""


def is_dos_type(dos_type: Optional[int]) -> bool:
    """Return True for the FFS/OFS DOS types DOS0-DOS7."""
    return dos_type is not None and DOS_BASE <= dos_type <= DOS_BASE + 7


def dos_tag(dos_type: int) -> str:
    """Return "DOS0".."DOS7" for an FFS/OFS DOS type."""
    return f"DOS{dos_type - DOS_BASE}"


def dos_range(tags: Iterable[str]) -> str:
    """Name DOS types compactly, e.g. "DOS0-DOS5" or "DOS0-DOS3, DOS5"."""
    runs = []
    for n in sorted({int(tag[3:]) for tag in tags}):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(
        f"DOS{first}" if first == last else f"DOS{first}-DOS{last}"
        for first, last in runs
    )


def user_driver_dir() -> Optional[Path]:
    """The drivers folder searched for the user's own FastFileSystem, or
    None when no home folder can be found to hold it."""
    from . import platform as plat

    try:
        return plat.get_primary_driver_dir()
    except RuntimeError:  # Path.home() found no home folder
        return None


def _user_driver() -> Optional[Path]:
    user_dir = user_driver_dir()
    return None if user_dir is None else user_dir / USER_DRIVER_NAME


def _usable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def bundled_present() -> bool:
    """Return True when the bundled FastFileSystem is installed and non-empty."""
    return _usable(BUNDLED_DRIVER)


def _cannot_read(what: str, path: Path, exc: OSError) -> str:
    return f"Cannot read {what}: {path} ({exc.strerror or exc})"


def _bundled_missing() -> str:
    return f"The bundled FastFileSystem is missing or damaged: {BUNDLED_DRIVER}"


def _bundled_mismatch(expected: str) -> str:
    return (
        f"The bundled FastFileSystem does not match the sha256 recorded in "
        f"{BUNDLED_PROVENANCE.name} ({expected}): {BUNDLED_DRIVER}"
    )


def bundled_provenance() -> Tuple[Optional[dict], Optional[str]]:
    """Return the bundled build's provenance record and None, or None and
    what is wrong with the record."""
    what = "The bundled FastFileSystem's provenance record"
    try:
        with open(BUNDLED_PROVENANCE, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as exc:
        return None, _cannot_read(
            "the bundled FastFileSystem's provenance record", BUNDLED_PROVENANCE, exc)
    except ValueError as exc:
        return None, f"{what} is not valid JSON: {BUNDLED_PROVENANCE} ({exc})"
    tags = data.get("dos_types") if isinstance(data, dict) else None
    valid = (
        isinstance(tags, list)
        and all(isinstance(tag, str) and _DOS_TAG_RE.match(tag) for tag in tags)
        and isinstance(data.get("sha256"), str)
    )
    if not valid:
        return None, f"{what} has no valid dos_types or sha256: {BUNDLED_PROVENANCE}"
    return data, None


def bundled_problem() -> Optional[str]:
    """Return what is wrong with the bundled FastFileSystem, or None."""
    provenance, problem = bundled_provenance()
    if problem is not None:
        return problem
    if not bundled_present():
        return _bundled_missing()
    try:
        data = BUNDLED_DRIVER.read_bytes()
    except OSError as exc:
        return _cannot_read("the bundled FastFileSystem", BUNDLED_DRIVER, exc)
    if hashlib.sha256(data).hexdigest() != provenance["sha256"]:
        return _bundled_mismatch(provenance["sha256"])
    return None


def user_copy_problem() -> Optional[str]:
    """Return why the user's FastFileSystem cannot be read, or None."""
    user = _user_driver()
    if user is None or not _usable(user):
        return None
    try:
        user.read_bytes()
    except OSError as exc:
        return _cannot_read("your FastFileSystem", user, exc)
    return None


def find_version(data: bytes) -> Optional[str]:
    """Return the text after the first "$VER:" in a driver binary, or None."""
    match = _VER_RE.search(data)
    if match is None:
        return None
    return match.group(1).decode("ascii").strip() or None


def _identity(path: Path, what: str, subject: str, remedy: str) -> Tuple[Optional[str], str]:
    """Return a driver's $VER text (or None) and its sha256.

    Raises DriverNotFound naming the file when it cannot be read.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DriverNotFound(
            f"{subject}\n{_cannot_read(what, path, exc)}\n{remedy}") from None
    return find_version(data), hashlib.sha256(data).hexdigest()


def _bundled(provenance: dict, subject: str, remedy: str, quiet: bool) -> ResolvedDriver:
    version, digest = _identity(BUNDLED_DRIVER, "the bundled FastFileSystem", subject, remedy)
    tag = provenance.get("tag")
    if digest != provenance["sha256"] and not quiet:
        # Whoever can change this file can change AmiFUSE's code too, so
        # refusing it would protect nothing; say so and use it.
        print(f"Warning: {_bundled_mismatch(provenance['sha256'])}; using it anyway.",
              file=sys.stderr)
    # A $VER string, once the handler has one, replaces the release tag.
    label = version or f"FastFileSystem {tag or 'unknown'}"
    return ResolvedDriver(
        BUNDLED_DRIVER, "bundled",
        f"{label} (bundled, sha256 {digest[:12]}) {BUNDLED_DRIVER}",
        version=version, sha256=digest, tag=tag,
    )


def find_folder_driver(dos_type: int, subject: str, quiet: bool = False) -> ResolvedDriver:
    """Pick the driver for a DOS0-DOS7 volume that has none of its own.

    subject is the first line of the error raised when nothing serves the
    volume, naming it, e.g. "ADF floppy image detected (DD, DOS7).".
    quiet suppresses the warning for a bundled file that does not match its
    recorded sha256; doctor reports that through bundled_problem().

    Raises DriverNotFound, also when the chosen file cannot be read.
    """
    tag = dos_tag(dos_type)
    user_dir = user_driver_dir() or NO_USER_DRIVER_DIR
    remedy = f"Reinstall AmiFUSE, place a FastFileSystem in {user_dir}, or use --driver."
    user = _user_driver()
    if user is not None and _usable(user):
        version, digest = _identity(
            user, "your FastFileSystem", subject,
            "Make it readable, remove it, or use --driver.")
        label = version or f"sha256 {digest[:12]}"
        return ResolvedDriver(
            user, "user", f"{user} (your drivers folder, {label})",
            version=version, sha256=digest,
        )

    provenance, problem = bundled_provenance()
    covered = provenance is not None and tag in provenance["dos_types"]
    if covered and bundled_present():
        return _bundled(provenance, subject, remedy, quiet)

    if _usable(DEV_FALLBACK_DRIVER):
        version, digest = _identity(
            DEV_FALLBACK_DRIVER, "the dev fallback FastFileSystem", subject, remedy)
        return ResolvedDriver(
            DEV_FALLBACK_DRIVER, "dev_fallback", f"{DEV_FALLBACK_DRIVER} (dev fallback)",
            version=version, sha256=digest,
        )

    if provenance is not None and not covered:
        long_names = " (long file names)" if tag in _LONG_NAME_TAGS else ""
        raise DriverNotFound(
            f"{subject}\n"
            f"The bundled FastFileSystem supports {dos_range(provenance['dos_types'])} "
            f"only; {tag}{long_names} needs another FFS.\n"
            f"Place a FastFileSystem that supports {tag} (e.g. from AmigaOS 3.2) "
            f"in {user_dir}, or use --driver."
        )
    raise DriverNotFound(f"{subject}\n{problem or _bundled_missing()}\n{remedy}")
