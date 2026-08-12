"""Canonical AmigaDOS structs used by AmiFUSE bootstrap code.

The Amiga ``DeviceNode`` used to start a filesystem handler is the
``DLT_DEVICE`` variant of ``DosList``.  Keep the historical local name as an
alias while sharing the actual ABI definition with standalone vamos.
"""

from amitools.vamos.libstructs import (
    DosEnvecStruct,
    DosListDeviceStruct,
    FileSysStartupMsgStruct,
)


DeviceNodeStruct = DosListDeviceStruct

__all__ = ["DeviceNodeStruct", "DosEnvecStruct", "FileSysStartupMsgStruct"]
