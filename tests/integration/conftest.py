"""Integration test configuration.

Integration tests use real HandlerBridge instances against committed
fixture images. They require amitools with machine68k to be functional.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.paths import PFS3AIO, PFS3_TEST_HDF, PFS3_8MB_HDF, BLANK_ADF

REPO_ROOT = Path(__file__).resolve().parents[2]


def _machine68k_works() -> bool:
    """Check if machine68k CPU can be instantiated without crashing.

    Runs the probe in a subprocess because machine68k may segfault due to
    C-extension bugs (e.g. opcode table over-read), which cannot be caught
    by Python exception handling.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import machine68k; machine68k.CPU(1)"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _has_external_fixtures() -> bool:
    """Check if external fixture directory exists with required files."""
    try:
        tools_dir = str(REPO_ROOT / "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        from fixture_paths import READONLY_DIR, DRIVERS_DIR  # type: ignore
        return (
            READONLY_DIR.exists()
            and DRIVERS_DIR.exists()
            and (DRIVERS_DIR / "pfs3aio").exists()
            and (READONLY_DIR / "pfs.hdf").exists()
        )
    except ImportError:
        return False


# Lazy evaluation: only probe machine68k when integration tests are collected.
# This avoids calling machine68k.CPU(1) during unit-only test runs, which
# prevents a potential segfault from the C extension killing the entire
# pytest process.
_machine68k_checked = False
_machine68k_available = False


def pytest_collection_modifyitems(config, items):
    """Skip integration tests when machine68k is unavailable."""
    global _machine68k_checked, _machine68k_available

    has_integration = any(
        item.get_closest_marker("integration") is not None for item in items
    )
    if not has_integration:
        return

    if not _machine68k_checked:
        _machine68k_available = _machine68k_works()
        _machine68k_checked = True

    if not _machine68k_available:
        skip = pytest.mark.skip(reason="machine68k CPU not functional")
        for item in items:
            if item.get_closest_marker("integration") is not None:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def pfs3_driver() -> Path:
    """Path to the PFS3 handler binary."""
    assert PFS3AIO.exists(), f"PFS3 handler not found: {PFS3AIO}"
    return PFS3AIO


@pytest.fixture(scope="session")
def pfs3_test_image() -> Path:
    """Path to the 1MB PFS3 test HDF (unformatted partition)."""
    assert PFS3_TEST_HDF.exists(), f"PFS3 test image not found: {PFS3_TEST_HDF}"
    return PFS3_TEST_HDF


@pytest.fixture(scope="session")
def pfs3_8mb_image() -> Path:
    """Path to the 8MB PFS3 test HDF (formatted with content)."""
    assert PFS3_8MB_HDF.exists(), f"PFS3 8MB image not found: {PFS3_8MB_HDF}"
    return PFS3_8MB_HDF


@pytest.fixture(scope="session")
def blank_adf() -> Path:
    """Path to blank ADF floppy image."""
    assert BLANK_ADF.exists(), f"Blank ADF not found: {BLANK_ADF}"
    return BLANK_ADF


@pytest.fixture(scope="session")
def external_fixtures_available() -> bool:
    """Check if external fixture directory exists with required files."""
    return _has_external_fixtures()
