"""Fixture path resolution for AmiFUSE test suite.

Resolves external fixture paths through a cascade:
  1. AMIFUSE_FIXTURE_ROOT env var
  2. ../AmiFUSE-testing sibling directory (relative to repo root)
  3. ~/AmigaOS/AmiFuse (default local path)
  4. None (tests skip gracefully)

This module does NOT import from tools/fixture_paths.py.
Tests and tools share the env var name but are independent.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_fixture_root() -> Path | None:
    """Find the fixture root directory, or None if unavailable."""
    # 1. Env var (CI and explicit override)
    env_root = os.environ.get("AMIFUSE_FIXTURE_ROOT")
    if env_root:
        p = Path(env_root)
        if p.is_dir():
            return p

    # 2. Sibling AmiFUSE-testing checkout
    sibling = REPO_ROOT.parent / "AmiFUSE-testing"
    if sibling.is_dir() and (sibling / "drivers").is_dir():
        return sibling

    # 3. Default local path
    default = Path.home() / "AmigaOS" / "AmiFuse"
    if default.is_dir() and (default / "drivers").is_dir():
        return default

    return None


FIXTURE_ROOT = _resolve_fixture_root()

# Derived paths (None-safe -- callers must check FIXTURE_ROOT first)
DRIVERS_DIR = FIXTURE_ROOT / "drivers" if FIXTURE_ROOT else None
READONLY_DIR = FIXTURE_ROOT / "fixtures" / "readonly" if FIXTURE_ROOT else None

# Specific fixture files used by integration tests
PFS3AIO = DRIVERS_DIR / "pfs3aio" if DRIVERS_DIR else None
PFS3_HDF = READONLY_DIR / "pfs.hdf" if READONLY_DIR else None
OFS_ADF = READONLY_DIR / "ofs.adf" if READONLY_DIR else None
FFS_DRIVER = DRIVERS_DIR / "FastFileSystem" if DRIVERS_DIR else None
