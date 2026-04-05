"""Unit tests for amifuse.scsi_device module."""

import inspect
import struct
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_backend():
    """Create a mock BlockDeviceBackend with 1000 blocks of 512 bytes."""
    backend = MagicMock()
    backend.total_blocks = 1000
    backend.block_size = 512
    backend.cyls = 10
    backend.heads = 10
    backend.secs = 10
    backend.read_only = False
    backend.read_blocks.return_value = b"\x00" * 512
    return backend


@pytest.fixture
def scsi(mock_backend):
    """Create a ScsiDevice with a mock backend."""
    from amifuse.scsi_device import ScsiDevice
    return ScsiDevice(mock_backend, debug=True)


class TestBlockBoundsChecking:
    """Tests for _check_block_bounds() helper."""

    def test_check_bounds_valid_range(self, scsi):
        assert scsi._check_block_bounds(0, 100) is True

    def test_check_bounds_exact_end(self, scsi):
        assert scsi._check_block_bounds(999, 1) is True

    def test_check_bounds_past_end(self, scsi):
        assert scsi._check_block_bounds(999, 2) is False

    def test_check_bounds_way_past_end(self, scsi):
        assert scsi._check_block_bounds(2000, 1) is False

    def test_check_bounds_negative_block(self, scsi):
        assert scsi._check_block_bounds(-1, 1) is False

    def test_check_bounds_negative_count(self, scsi):
        assert scsi._check_block_bounds(0, -1) is False

    def test_check_bounds_zero_count(self, scsi):
        assert scsi._check_block_bounds(0, 0) is True

    def test_check_bounds_overflow(self, scsi):
        assert scsi._check_block_bounds(500, 501) is False

    def test_begin_io_has_bounds_checking(self):
        """Verify BeginIO dispatches through _check_block_bounds."""
        from amifuse.scsi_device import ScsiDevice
        source = inspect.getsource(ScsiDevice.BeginIO)
        assert "_check_block_bounds" in source


class TestReadCapacityOverflow:
    """Tests for READ CAPACITY(10) overflow handling."""

    def test_read_capacity_normal_image(self):
        """Verify READ CAPACITY(10) returns correct last_lba for small images."""
        total_blocks = 1000
        last_lba = total_blocks - 1
        if last_lba > 0xFFFFFFFF:
            last_lba = 0xFFFFFFFF
        assert last_lba == 999
        packed = last_lba.to_bytes(4, "big")
        assert packed == b"\x00\x00\x03\xe7"

    def test_read_capacity_2tb_overflow(self):
        """Verify READ CAPACITY(10) caps last_lba at 0xFFFFFFFF for >2TB."""
        total_blocks = 2**32 + 100
        last_lba = total_blocks - 1
        if last_lba > 0xFFFFFFFF:
            last_lba = 0xFFFFFFFF
        assert last_lba == 0xFFFFFFFF
        packed = last_lba.to_bytes(4, "big")
        assert packed == b"\xff\xff\xff\xff"

    def test_read_capacity_exactly_2tb(self):
        """Verify READ CAPACITY(10) handles exactly 2^32 blocks (boundary)."""
        total_blocks = 2**32
        last_lba = total_blocks - 1
        if last_lba > 0xFFFFFFFF:
            last_lba = 0xFFFFFFFF
        assert last_lba == 0xFFFFFFFF
        packed = last_lba.to_bytes(4, "big")
        assert packed == b"\xff\xff\xff\xff"

    def test_read_capacity_source_has_cap(self):
        """Verify READ CAPACITY(10) has the 0xFFFFFFFF cap in source."""
        from amifuse.scsi_device import ScsiDevice
        source = inspect.getsource(ScsiDevice.BeginIO)
        assert "0xFFFFFFFF" in source
        assert "last_lba > 0xFFFFFFFF" in source
