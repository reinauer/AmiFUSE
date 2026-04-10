"""Integration test conftest -- machine68k probe + fixture providers."""
import subprocess
import sys

import pytest

from tests.fixtures.paths import FIXTURE_ROOT, PFS3AIO, PFS3_HDF, OFS_ADF


def _machine68k_works() -> bool:
    """Subprocess probe -- safe against segfaults from C extension."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import machine68k; machine68k.CPU(1)"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


_m68k_checked = False
_m68k_available = False


def pytest_collection_modifyitems(config, items):
    """Skip integration tests when machine68k or fixtures are unavailable.

    Skip hierarchy: no fixtures -> skip all integration, then
    no machine68k -> skip all integration. Tests that don't need
    either (help, doctor) should use tests/unit/ instead.
    """
    global _m68k_checked, _m68k_available

    has_integration = any(
        item.get_closest_marker("integration") is not None for item in items
    )
    if not has_integration:
        return

    if FIXTURE_ROOT is None:
        skip = pytest.mark.skip(
            reason="No fixture root found (set AMIFUSE_FIXTURE_ROOT or place fixtures in ~/AmigaOS/AmiFuse)"
        )
        for item in items:
            if item.get_closest_marker("integration") is not None:
                item.add_marker(skip)
        return

    if not _m68k_checked:
        _m68k_available = _machine68k_works()
        _m68k_checked = True

    if not _m68k_available:
        skip = pytest.mark.skip(reason="machine68k CPU not functional")
        for item in items:
            if item.get_closest_marker("integration") is not None:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def fixture_root():
    if FIXTURE_ROOT is None:
        pytest.skip("No fixture root found")
    return FIXTURE_ROOT


@pytest.fixture(scope="session")
def pfs3_driver():
    if PFS3AIO is None or not PFS3AIO.exists():
        pytest.skip("PFS3 handler not found")
    return PFS3AIO


@pytest.fixture(scope="session")
def pfs3_image():
    if PFS3_HDF is None or not PFS3_HDF.exists():
        pytest.skip("PFS3 test image not found")
    return PFS3_HDF


@pytest.fixture(scope="session")
def ofs_adf_image():
    if OFS_ADF is None or not OFS_ADF.exists():
        pytest.skip("OFS ADF image not found")
    return OFS_ADF
