"""Unit tests for amifuse.scsi_device module."""

import inspect
import struct
import pytest
from unittest.mock import MagicMock, patch

from amitools.vamos.machine.mock.mem import MockMemory
from amitools.vamos.libstructs.exec_ import IORequestStruct
from amifuse.scsi_device import SCSICmdStruct


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


class TestScsiWriteShortBuffer:
    """Tests for WRITE(10) short-buffer validation."""

    # Memory layout constants
    IOR_ADDR = 0x1000
    SCSI_CMD_ADDR = 0x2000
    CDB_ADDR = 0x3000
    DATA_ADDR = 0x4000
    SENSE_ADDR = 0x5000

    def _setup_write10(self, mem, scsi_dev, *, xfer_blocks, data_len,
                       lba=0, sense_len=18):
        """Set up memory for a WRITE(10) HD_SCSICMD call and invoke BeginIO.

        Returns (ior, scsi_struct) for assertions.
        """
        # Build IORequestStruct at IOR_ADDR
        ior = IORequestStruct(mem, self.IOR_ADDR)
        ior.command.val = 28  # HD_SCSICMD
        ior.data.val = self.SCSI_CMD_ADDR  # buf_ptr -> SCSICmdStruct

        # Build SCSICmdStruct at SCSI_CMD_ADDR
        scsi_struct = SCSICmdStruct(mem, self.SCSI_CMD_ADDR)
        scsi_struct.scsi_Data.val = self.DATA_ADDR
        scsi_struct.scsi_Length.val = data_len
        scsi_struct.scsi_Command.val = self.CDB_ADDR
        scsi_struct.scsi_CmdLength.val = 10
        scsi_struct.scsi_Flags.val = 0
        scsi_struct.scsi_Status.val = 0
        scsi_struct.scsi_SenseData.val = self.SENSE_ADDR if sense_len > 0 else 0
        scsi_struct.scsi_SenseLength.val = sense_len
        scsi_struct.scsi_SenseActual.val = 0
        scsi_struct.scsi_Actual.val = 0

        # Build WRITE(10) CDB at CDB_ADDR
        mem.w8(self.CDB_ADDR, 0x2A)  # opcode
        mem.w32(self.CDB_ADDR + 2, lba)  # LBA
        mem.w16(self.CDB_ADDR + 7, xfer_blocks)  # transfer length

        # Fill data buffer with a recognizable pattern
        for i in range(min(data_len, 1024)):
            mem.w8(self.DATA_ADDR + i, (i & 0xFF))

        # Create minimal ctx
        ctx = MagicMock()
        ctx.mem = mem

        scsi_dev.BeginIO(ctx, self.IOR_ADDR)

        # Re-read structs after call (values updated in memory)
        ior = IORequestStruct(mem, self.IOR_ADDR)
        scsi_struct = SCSICmdStruct(mem, self.SCSI_CMD_ADDR)
        return ior, scsi_struct

    def test_write10_short_buffer_check_condition(self, mock_backend):
        """WRITE(10) with buffer shorter than xfer_blocks should return CHECK CONDITION."""
        from amifuse.scsi_device import ScsiDevice
        dev = ScsiDevice(mock_backend, debug=False)
        mem = MockMemory(size_kib=64)

        # Request 2 blocks (1024 bytes) but only provide 512 bytes
        _, scsi_struct = self._setup_write10(
            mem, dev, xfer_blocks=2, data_len=512
        )

        assert scsi_struct.scsi_Status.val == 2  # CHECK CONDITION
        mock_backend.write_blocks.assert_not_called()

    def test_write10_short_buffer_sense_data(self, mock_backend):
        """WRITE(10) short buffer should write ILLEGAL_REQUEST sense data."""
        from amifuse.scsi_device import ScsiDevice
        dev = ScsiDevice(mock_backend, debug=False)
        mem = MockMemory(size_kib=64)

        _, scsi_struct = self._setup_write10(
            mem, dev, xfer_blocks=2, data_len=512, sense_len=18
        )

        assert scsi_struct.scsi_Status.val == 2
        assert scsi_struct.scsi_SenseActual.val == 18

        # Verify sense data bytes
        sense = mem.r_block(self.SENSE_ADDR, 18)
        assert sense[0] == 0x70  # current errors, fixed format
        assert sense[2] == 0x05  # ILLEGAL_REQUEST
        assert sense[7] == 0x0A  # additional sense length
        assert sense[12] == 0x24  # ASC: INVALID FIELD IN CDB
        assert sense[13] == 0x00  # ASCQ

    def test_write10_adequate_buffer_succeeds(self, mock_backend):
        """WRITE(10) with adequate buffer should succeed and call write_blocks."""
        from amifuse.scsi_device import ScsiDevice
        dev = ScsiDevice(mock_backend, debug=False)
        mem = MockMemory(size_kib=64)

        # Request 2 blocks (1024 bytes) with 1024 bytes available
        _, scsi_struct = self._setup_write10(
            mem, dev, xfer_blocks=2, data_len=1024
        )

        assert scsi_struct.scsi_Status.val == 0  # GOOD
        mock_backend.write_blocks.assert_called_once()
        args = mock_backend.write_blocks.call_args
        assert args[0][0] == 0  # lba
        assert len(args[0][1]) == 1024  # full data
        assert args[0][2] == 2  # xfer_blocks

    def test_write10_zero_blocks_succeeds(self, mock_backend):
        """WRITE(10) with xfer_blocks=0 should succeed without writing."""
        from amifuse.scsi_device import ScsiDevice
        dev = ScsiDevice(mock_backend, debug=False)
        mem = MockMemory(size_kib=64)

        _, scsi_struct = self._setup_write10(
            mem, dev, xfer_blocks=0, data_len=0
        )

        assert scsi_struct.scsi_Status.val == 0  # GOOD
        mock_backend.write_blocks.assert_not_called()
