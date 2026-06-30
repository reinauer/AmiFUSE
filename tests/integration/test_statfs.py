"""Integration tests for statfs (disk space reporting).

Verifies that mounted images report correct filesystem geometry via os.statvfs
(Unix) or GetDiskFreeSpaceExW (Windows).
"""
import os
import sys

import pytest

pytestmark = pytest.mark.fuse


def test_statfs_reports_nonzero_blocks(pfs3_mount):
    """Mounted PFS3 image reports non-zero block count via statvfs."""
    _proc, mountpoint = pfs3_mount
    mp = str(mountpoint)

    if sys.platform.startswith("win"):
        import ctypes

        free_bytes = ctypes.c_ulonglong(0)
        total_bytes = ctypes.c_ulonglong(0)
        total_free_bytes = ctypes.c_ulonglong(0)
        result = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            mp + "\\",
            ctypes.byref(free_bytes),
            ctypes.byref(total_bytes),
            ctypes.byref(total_free_bytes),
        )
        assert result != 0, "GetDiskFreeSpaceExW failed"
        assert total_bytes.value > 0, "Total bytes should be non-zero"
    else:
        st = os.statvfs(mp)
        assert st.f_blocks > 0, f"f_blocks should be non-zero, got {st.f_blocks}"
        assert st.f_bsize > 0, f"f_bsize should be non-zero, got {st.f_bsize}"


def test_statfs_free_less_than_or_equal_total(pfs3_mount):
    """Free space should not exceed total space."""
    _proc, mountpoint = pfs3_mount
    mp = str(mountpoint)

    if sys.platform.startswith("win"):
        import ctypes

        free_bytes = ctypes.c_ulonglong(0)
        total_bytes = ctypes.c_ulonglong(0)
        total_free_bytes = ctypes.c_ulonglong(0)
        result = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            mp + "\\",
            ctypes.byref(free_bytes),
            ctypes.byref(total_bytes),
            ctypes.byref(total_free_bytes),
        )
        assert result != 0, "GetDiskFreeSpaceExW failed"
        assert total_free_bytes.value <= total_bytes.value, (
            f"Free ({total_free_bytes.value}) exceeds total ({total_bytes.value})"
        )
    else:
        st = os.statvfs(mp)
        assert st.f_bfree <= st.f_blocks, (
            f"f_bfree ({st.f_bfree}) exceeds f_blocks ({st.f_blocks})"
        )


def test_statfs_block_size_is_power_of_two(pfs3_mount):
    """Block size should be a power of two (512, 1024, 2048, etc.)."""
    _proc, mountpoint = pfs3_mount
    mp = str(mountpoint)

    if sys.platform.startswith("win"):
        import ctypes

        sectors_per_cluster = ctypes.c_ulong(0)
        bytes_per_sector = ctypes.c_ulong(0)
        free_clusters = ctypes.c_ulong(0)
        total_clusters = ctypes.c_ulong(0)
        result = ctypes.windll.kernel32.GetDiskFreeSpaceW(
            mp + "\\",
            ctypes.byref(sectors_per_cluster),
            ctypes.byref(bytes_per_sector),
            ctypes.byref(free_clusters),
            ctypes.byref(total_clusters),
        )
        if result == 0:
            pytest.skip("GetDiskFreeSpaceW not supported for this mount")
        bsize = bytes_per_sector.value
    else:
        st = os.statvfs(mp)
        bsize = st.f_bsize

    assert bsize > 0, f"Block size should be positive, got {bsize}"
    assert (bsize & (bsize - 1)) == 0, (
        f"Block size {bsize} is not a power of two"
    )
