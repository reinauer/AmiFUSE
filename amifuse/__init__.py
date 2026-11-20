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
    __version__ = "v1.0.0"


def _ensure_repo_on_path() -> None:
    """Let the package's modules import each other when run from the repo."""
    repo_root = Path(__file__).resolve().parents[1]

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


_ensure_repo_on_path()
