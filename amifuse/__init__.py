"""Helper utilities for running Amiga filesystem drivers on the host."""

from pathlib import Path
import sys

# Single source of truth for the version string. Kept dependency-free so
# modules that must work without amitools installed (e.g. doctor) can use it.
try:
    from importlib.metadata import version as _pkg_version
    __version__ = f"v{_pkg_version('amifuse')}"
except Exception:
    # Fallback when package metadata is unavailable (running from a source
    # tree without an installed distribution).
    __version__ = "v0.5.0"


def _ensure_local_amitools_on_path() -> None:
    """Prefer the vendored amitools checkout when running from the repo."""
    repo_root = Path(__file__).resolve().parents[1]
    amitools_path = repo_root / "amitools"

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    if amitools_path.is_dir() and str(amitools_path) not in sys.path:
        sys.path.insert(0, str(amitools_path))


_ensure_local_amitools_on_path()
