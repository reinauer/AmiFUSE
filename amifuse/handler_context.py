"""
Utilities to derive handler entry points and allocate basic DOS/Exec structs
inside the vamos machine. Execution is not wired yet; this sets the stage for
calling into the handler.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from amitools.vamos.astructs.access import AccessStruct  # type: ignore
from amitools.vamos.libstructs.dos import DosPacketStruct, MessageStruct  # type: ignore
from amitools.vamos.libstructs.exec_ import MsgPortStruct  # type: ignore
from amitools.vamos.libstructs.dos import FileSysStartupMsgStruct  # type: ignore

from .vamos_runner import VamosHandlerRuntime


@dataclass
class HandlerEntry:
    seglist_baddr: int
    entry_addr: int


class HandlerContext:
    def __init__(self, vh: VamosHandlerRuntime):
        self.vh = vh
        self.mem = vh.alloc.get_mem() if vh.alloc else None

    def get_handler_entry(self) -> HandlerEntry:
        if self.vh.seglist_baddr is None:
            raise RuntimeError("Handler not loaded")
        seglist = self.vh.slm.seg_loader.infos[self.vh.seglist_baddr].seglist
        seg = seglist.get_segment()
        return HandlerEntry(seglist.get_baddr(), seg.get_addr())

    def alloc_packet_and_msg(self):
        """Allocate a DosPacket + Message pair and return their addresses."""
        if self.mem is None or self.vh.alloc is None:
            raise RuntimeError("Vamos runtime not initialized")
        pkt_mem = self.vh.alloc.alloc_memory(DosPacketStruct.get_size(), label="pkt")
        msg_mem = self.vh.alloc.alloc_memory(MessageStruct.get_size(), label="msg")
        pkt = AccessStruct(self.mem, DosPacketStruct, pkt_mem.addr)
        msg = AccessStruct(self.mem, MessageStruct, msg_mem.addr)
        # link message to packet (mn_Node.ln_Name is commonly used to carry pkt ptr)
        msg.w_s("mn_Node.ln_Name", pkt_mem.addr)
        return pkt_mem.addr, msg_mem.addr

    def alloc_reply_port(self):
        if self.mem is None or self.vh.alloc is None:
            raise RuntimeError("Vamos runtime not initialized")
        port_mem = self.vh.alloc.alloc_memory(
            MsgPortStruct.get_size(), label="reply_port"
        )
        # The AccessStruct wrapper doesn't expose .addr, so return the allocated address.
        AccessStruct(self.mem, MsgPortStruct, port_mem.addr)  # materialize for now
        return port_mem.addr

    def alloc_fssm(self, packet_bptr: int) -> int:
        """Allocate a FileSysStartupMsg and point its startup packet to the given BPTR."""
        if self.mem is None or self.vh.alloc is None:
            raise RuntimeError("Vamos runtime not initialized")
        fssm_mem = self.vh.alloc.alloc_memory(
            FileSysStartupMsgStruct.get_size(), label="fssm"
        )
        fssm = AccessStruct(self.mem, FileSysStartupMsgStruct, fssm_mem.addr)
        fssm.w_s("fssm_StartupMsg.sm_Message.mn_Node.ln_Name", packet_bptr)
        return fssm_mem.addr
