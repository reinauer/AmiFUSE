"""
Helper to allocate minimal AmigaDOS structs (DosEnvec, FileSysStartupMsg,
DeviceNode) in vamos memory using partition info.
"""

from pathlib import Path
from typing import Optional

from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice  # type: ignore

from .amiga_structs import DosEnvecStruct, FileSysStartupMsgStruct, DeviceNodeStruct
from amitools.vamos.libstructs.exec_ import MsgPortStruct, ListStruct, NodeType  # type: ignore
from .rdb_inspect import detect_adf, ADFInfo, ISOInfo


def _scalar_field(struct, name: str):
    field = struct.sfields.get_field_by_name(name)
    if field is None:
        raise AttributeError(f"{type(struct).__name__} has no field {name!r}")
    return field


# PFS/PDS family share an identical on-disk layout — the dostype only steers
# pfs3aio's I/O access mode at mount (see PDS3_MOUNT_BUG.md).
_PFS_FAMILY_PREFIX = 0x50465300  # 'PFS\0'
_PDS_FAMILY_PREFIX = 0x50445300  # 'PDS\0'
_PFS_PDS_VERSIONS = (0x01, 0x03)  # \1 and \3 — versions known to share format


def _remap_pds_to_pfs(dostype: int) -> int:
    """Return ``dostype`` with the family prefix rewritten from ``PDS`` to
    ``PFS`` for known-compatible versions (\\1 and \\3).

    Other dostypes — including unknown PDS\\? versions, AFS, OFS, SFS — are
    returned unchanged.
    """
    if (dostype & 0xFFFFFF00) != _PDS_FAMILY_PREFIX:
        return dostype
    version = dostype & 0xFF
    if version not in _PFS_PDS_VERSIONS:
        return dostype
    return _PFS_FAMILY_PREFIX | version


def is_remapped_dostype(dostype: int) -> bool:
    """True iff ``_remap_pds_to_pfs`` would change ``dostype``."""
    return _remap_pds_to_pfs(dostype) != dostype


class SyntheticDosEnv:
    """Synthetic DosEnvec-like object for ADF (floppy) images."""
    def __init__(self, adf_info: ADFInfo):
        self.size = 16  # de_TableSize
        self.block_size = 128  # de_SizeBlock in longwords (512 bytes / 4)
        self.sec_org = 0
        self.surfaces = adf_info.heads
        self.sec_per_blk = 1
        self.blk_per_trk = adf_info.sectors_per_track
        self.reserved = 2  # Boot blocks
        self.pre_alloc = 0
        self.interleave = 0
        self.low_cyl = 0
        self.high_cyl = adf_info.cylinders - 1
        self.num_buffer = 5
        self.buf_mem_type = 0
        self.max_transfer = 0x7FFFFFFF
        self.mask = 0xFFFFFFFF
        self.boot_pri = 0
        self.dos_type = adf_info.dos_type
        self.baud = 0
        self.control = 0
        self.boot_blocks = 2


class SyntheticPartition:
    """Synthetic partition info for ADF images."""
    def __init__(self, adf_info: ADFInfo):
        self.num = 0
        self.adf_info = adf_info

    def get_num_blocks(self):
        return self.adf_info.total_blocks


class SyntheticIsoDosEnv:
    """Synthetic DosEnvec-like object for ISO 9660 images."""
    def __init__(self, iso_info: ISOInfo):
        self.size = 16  # de_TableSize
        self.block_size = iso_info.block_size // 4  # de_SizeBlock in longwords
        self.sec_org = 0
        self.surfaces = iso_info.heads
        self.sec_per_blk = 1
        self.blk_per_trk = iso_info.sectors_per_track
        self.reserved = 0
        self.pre_alloc = 0
        self.interleave = 0
        self.low_cyl = 0
        self.high_cyl = iso_info.cylinders - 1
        self.num_buffer = 5
        self.buf_mem_type = 0
        self.max_transfer = 0x7FFFFFFF
        self.mask = 0xFFFFFFFF
        self.boot_pri = 0
        self.dos_type = 0
        self.baud = 0
        self.control = 0
        self.boot_blocks = 0


class SyntheticIsoPartition:
    """Synthetic partition info for ISO images."""
    def __init__(self, iso_info: ISOInfo):
        self.num = 0
        self.iso_info = iso_info

    def get_num_blocks(self):
        return self.iso_info.total_blocks


class BootstrapAllocator:
    def __init__(self, vh, image_path: Path, block_size=512, partition=None,
                 adf_info: Optional[ADFInfo] = None, iso_info: Optional[ISOInfo] = None,
                 mbr_partition_index=None, strict_dostype: bool = False):
        self.vh = vh
        self.alloc = vh.alloc
        self.mem = vh.alloc.get_mem()
        self.image_path = image_path
        self.block_size = block_size
        self.partition = partition  # name, index, or None for first
        self.adf_info = adf_info  # Pre-detected ADF info, if any
        self.iso_info = iso_info  # Pre-detected ISO info, if any
        self.mbr_partition_index = mbr_partition_index  # For MBR disks with multiple 0x76 partitions
        self.strict_dostype = strict_dostype  # If False, PDS\1/\3 dostypes are remapped to PFS\1/\3
        self.remapped_dostype: Optional[tuple] = None  # (original, remapped) iff remap fired

    def _read_partition_env(self):
        from .rdb_inspect import open_rdisk

        blk, rd, mbr_ctx = open_rdisk(
            self.image_path, block_size=self.block_size,
            mbr_partition_index=self.mbr_partition_index,
        )
        if self.partition is None:
            part = rd.get_partition(0)
        else:
            part = rd.find_partition_by_string(str(self.partition))
            if part is None:
                rd.close()
                blk.close()
                raise ValueError(f"Partition '{self.partition}' not found")
        de = part.part_blk.dos_env
        return de, blk, rd, part

    def _read_adf_env(self):
        """Create synthetic partition info for ADF images."""
        blk = RawBlockDevice(str(self.image_path), read_only=True, block_bytes=self.block_size)
        blk.open()
        de = SyntheticDosEnv(self.adf_info)
        part = SyntheticPartition(self.adf_info)
        return de, blk, None, part  # rd is None for ADF

    def _read_iso_env(self):
        """Create synthetic partition info for ISO images."""
        blk = RawBlockDevice(str(self.image_path), read_only=True,
                             block_bytes=self.iso_info.block_size)
        blk.open()
        de = SyntheticIsoDosEnv(self.iso_info)
        part = SyntheticIsoPartition(self.iso_info)
        return de, blk, None, part  # rd is None for ISO

    def alloc_all(self, handler_seglist_baddr, handler_seglist_bptr, handler_name="PFS0:"):
        # Use ADF/ISO synthetic partition if detected, otherwise read from RDB
        if self.adf_info is not None:
            de, blk, rd, part = self._read_adf_env()
        elif self.iso_info is not None:
            de, blk, rd, part = self._read_iso_env()
        else:
            de, blk, rd, part = self._read_partition_env()
        # DosEnvec
        env_mem = self.alloc.alloc_memory(DosEnvecStruct.get_size(), label="DosEnvec")
        env = DosEnvecStruct(self.mem, env_mem.addr)
        _scalar_field(env, "de_TableSize").val = de.size if getattr(de, "size", 0) else 16
        _scalar_field(env, "de_SizeBlock").val = de.block_size
        _scalar_field(env, "de_SecOrg").val = de.sec_org
        _scalar_field(env, "de_Surfaces").val = de.surfaces
        _scalar_field(env, "de_SectorPerBlock").val = de.sec_per_blk
        _scalar_field(env, "de_BlocksPerTrack").val = de.blk_per_trk
        _scalar_field(env, "de_Reserved").val = de.reserved
        _scalar_field(env, "de_PreAlloc").val = de.pre_alloc
        _scalar_field(env, "de_Interleave").val = de.interleave
        _scalar_field(env, "de_LowCyl").val = de.low_cyl
        _scalar_field(env, "de_HighCyl").val = de.high_cyl
        _scalar_field(env, "de_NumBuffers").val = de.num_buffer
        _scalar_field(env, "de_BufMemType").val = de.buf_mem_type
        _scalar_field(env, "de_MaxTransfer").val = de.max_transfer
        # Relax mask: allow any address to avoid handler memorymask complaints
        _scalar_field(env, "de_Mask").val = 0xFFFFFFFF
        _scalar_field(env, "de_BootPri").val = de.boot_pri
        effective_dostype = de.dos_type if self.strict_dostype else _remap_pds_to_pfs(de.dos_type)
        if effective_dostype != de.dos_type:
            self.remapped_dostype = (de.dos_type, effective_dostype)
        _scalar_field(env, "de_DosType").val = effective_dostype
        _scalar_field(env, "de_Baud").val = de.baud
        _scalar_field(env, "de_Control").val = de.control
        _scalar_field(env, "de_BootBlocks").val = de.boot_blocks

        # FSSM
        fssm_mem = self.alloc.alloc_memory(FileSysStartupMsgStruct.get_size(), label="FSSM")
        fssm = FileSysStartupMsgStruct(self.mem, fssm_mem.addr)
        dev_bstr = b"\x0b" + b"scsi.device"
        dev_mem = self.alloc.alloc_memory(len(dev_bstr), label="dev_bstr")
        self.mem.w_block(dev_mem.addr, dev_bstr)
        _scalar_field(fssm, "fssm_Unit").val = 0
        _scalar_field(fssm, "fssm_Device").val = dev_mem.addr >> 2
        _scalar_field(fssm, "fssm_Environ").val = env_mem.addr >> 2
        _scalar_field(fssm, "fssm_Flags").val = 0

        # DeviceNode
        dn_mem = self.alloc.alloc_memory(DeviceNodeStruct.get_size(), label="DeviceNode")
        dn = DeviceNodeStruct(self.mem, dn_mem.addr)
        name_bstr = bytes([len(handler_name)]) + handler_name.encode("ascii")
        name_mem = self.alloc.alloc_memory(len(name_bstr), label="dn_name")
        self.mem.w_block(name_mem.addr, name_bstr)
        _scalar_field(dn, "dn_Next").val = 0
        _scalar_field(dn, "dn_Type").val = 0
        _scalar_field(dn, "dn_Task").val = 0
        _scalar_field(dn, "dn_Lock").val = 0
        _scalar_field(dn, "dn_Handler").val = handler_seglist_bptr
        _scalar_field(dn, "dn_StackSize").val = 0
        _scalar_field(dn, "dn_Priority").val = 0
        _scalar_field(dn, "dn_Startup").val = fssm_mem.addr >> 2
        _scalar_field(dn, "dn_SegList").val = handler_seglist_bptr
        _scalar_field(dn, "dn_GlobalVec").val = -1
        _scalar_field(dn, "dn_Name").val = name_mem.addr >> 2

        return {
            "env_addr": env_mem.addr,
            "fssm_addr": fssm_mem.addr,
            "device_bstr": dev_mem.addr,
            "dn_addr": dn_mem.addr,
            "dn_name_addr": name_mem.addr,
            "blk": blk,
            "rd": rd,
            "part": part,
        }

    def alloc_msgport(self):
        """Allocate and minimally init a MsgPort.

        Initializes mp_MsgList as a proper empty Exec list with correct
        sentinel pointers (lh_Head -> &lh_Tail, lh_Tail = 0,
        lh_TailPred -> &lh_Head). This matches the logic/semantics of
        HandlerLauncher._init_msgport() (proper sentinel pointers) but
        uses inline offset computation rather than the cached offsets
        that _init_msgport() uses.
        """
        mp_mem = self.alloc.alloc_memory(MsgPortStruct.get_size(), label="MsgPort")
        mp = MsgPortStruct(self.mem, mp_mem.addr)
        mp.node.type.val = NodeType.NT_MSGPORT
        # Init message list as a proper empty Exec list
        list_offset = MsgPortStruct.sdef.find_field_def_by_name("mp_MsgList").offset
        list_addr = mp_mem.addr + list_offset
        lst = ListStruct(self.mem, list_addr)
        lh_head_addr = list_addr + ListStruct.sdef.find_field_def_by_name("lh_Head").offset
        lh_tail_addr = list_addr + ListStruct.sdef.find_field_def_by_name("lh_Tail").offset
        # Empty list: Head points to Tail address, TailPred points to Head address
        lst.head.aptr = lh_tail_addr
        lst.tail.aptr = 0
        lst.tail_pred.aptr = lh_head_addr
        lst.type.val = NodeType.NT_MESSAGE
        mp.flags.val = 0
        mp.sig_bit.val = 0
        mp.sig_task.aptr = 0
        return mp_mem.addr
