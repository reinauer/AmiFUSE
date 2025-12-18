"""
Minimal AmigaDOS structs for filesystem bootstrap.

Layouts taken from AmigaOS 3.x headers (dos/filehandler.h):
FileSysStartupMsg:
  ULONG fssm_Unit;
  BSTR  fssm_Device;   (BPTR to BSTR)
  BPTR  fssm_Environ;  (DosEnvec)
  ULONG fssm_Flags;

DeviceNode (DLT_DEVICE variant):
  BPTR  dn_Next;
  ULONG dn_Type;
  APTR  dn_Task;
  BPTR  dn_Lock;
  BPTR  dn_Handler;
  ULONG dn_StackSize;
  LONG  dn_Priority;
  BPTR  dn_Startup;
  BPTR  dn_SegList;
  LONG  dn_GlobalVec;
  BPTR  dn_Name;       (BSTR)
"""

from amitools.vamos.astructs import AmigaStructDef, AmigaStruct
from amitools.vamos.astructs.scalar import ULONG, LONG


@AmigaStructDef
class FileSysStartupMsgStruct(AmigaStruct):
    _format = [
        (ULONG, "fssm_Unit"),
        (ULONG, "fssm_Device"),   # BPTR to BSTR
        (ULONG, "fssm_Environ"),  # BPTR to DosEnvec
        (ULONG, "fssm_Flags"),
    ]


@AmigaStructDef
class DeviceNodeStruct(AmigaStruct):
    _format = [
        (ULONG, "dn_Next"),       # BPTR
        (ULONG, "dn_Type"),
        (ULONG, "dn_Task"),       # APTR
        (ULONG, "dn_Lock"),       # BPTR
        (ULONG, "dn_Handler"),    # BPTR BSTR filename
        (ULONG, "dn_StackSize"),
        (LONG,  "dn_Priority"),
        (ULONG, "dn_Startup"),    # BPTR FileSysStartupMsg
        (ULONG, "dn_SegList"),    # BPTR seglist
        (LONG,  "dn_GlobalVec"),
        (ULONG, "dn_Name"),       # BPTR BSTR
    ]


# DosEnvec stays the same; reimport from old file
from amitools.vamos.astructs import AmigaStructDef as _AmigaStructDef
from amitools.vamos.astructs.scalar import UBYTE, UWORD


@_AmigaStructDef
class DosEnvecStruct(AmigaStruct):
    _format = [
        (ULONG, "de_TableSize"),   # number of entries (longs)
        (ULONG, "de_SizeBlock"),   # in longs
        (ULONG, "de_SecOrg"),
        (ULONG, "de_Surfaces"),
        (ULONG, "de_SectorPerBlock"),
        (ULONG, "de_BlocksPerTrack"),
        (ULONG, "de_Reserved"),
        (ULONG, "de_PreAlloc"),
        (ULONG, "de_Interleave"),
        (ULONG, "de_LowCyl"),
        (ULONG, "de_HighCyl"),
        (ULONG, "de_NumBuffers"),
        (ULONG, "de_BufMemType"),
        (ULONG, "de_MaxTransfer"),
        (ULONG, "de_Mask"),
        (LONG, "de_BootPri"),
        (ULONG, "de_DosType"),
        (ULONG, "de_Baud"),
        (ULONG, "de_Control"),
        (ULONG, "de_BootBlocks"),
    ]
