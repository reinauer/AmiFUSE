"""
Build a tiny stub that sets A0 to the FSSM BPTR and jumps to the handler entry.
Used to force correct startup context when invoking the handler.
"""

from amitools.vamos.machine.regs import REG_PC  # type: ignore


def build_entry_stub(mem, alloc, fssm_bptr, handler_entry_addr):
    """
    Allocate code: move.l #fssm_bptr*4, A0; jmp handler_entry_addr
    Returns stub PC.
    """
    # 68000: move.l #imm32, A0 -> 0x203c imm16 imm16 ; jmp abs.l -> 0x4ef9 imm16 imm16
    code = bytearray()
    code += b"\x20\x3c"  # move.l #imm32, A0
    code += ((fssm_bptr << 2) >> 16).to_bytes(2, "big")
    code += ((fssm_bptr << 2) & 0xFFFF).to_bytes(2, "big")
    code += b"\x4e\xf9"  # jmp absolute long
    code += (handler_entry_addr >> 16).to_bytes(2, "big")
    code += (handler_entry_addr & 0xFFFF).to_bytes(2, "big")
    mem_obj = alloc.alloc_memory(len(code), label="handler_stub")
    mem.w_block(mem_obj.addr, code)
    return mem_obj.addr
