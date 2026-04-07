"""Shared fixture path constants for all test layers.

This is a simple constants module (not a pytest conftest). Import path
constants from here in both unit and integration tests.
"""
from pathlib import Path

FIXTURES_ROOT = Path(__file__).parent
HANDLERS_DIR = FIXTURES_ROOT / "handlers"
IMAGES_DIR = FIXTURES_ROOT / "images"

PFS3AIO = HANDLERS_DIR / "pfs3aio"
PFS3_TEST_HDF = IMAGES_DIR / "pfs3_test.hdf"
PFS3_8MB_HDF = IMAGES_DIR / "pfs3_8mb.hdf"
BLANK_ADF = IMAGES_DIR / "blank.adf"
