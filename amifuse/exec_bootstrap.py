"""
Helpers to poke ExecBase/Task structures directly in vamos memory to create a
minimal task context and MsgPort for the filesystem handler. This is hacky but
avoids modifying amitools internals.
"""

from amitools.vamos.astructs.access import AccessStruct  # type: ignore
from amitools.vamos.libstructs.exec_ import TaskStruct, NodeType, MsgPortStruct, ListStruct  # type: ignore
from amitools.vamos.schedule.stack import Stack  # type: ignore
from amitools.vamos.schedule.task import Task  # type: ignore
from amitools.vamos.libstructs.dos import DosPacketStruct, MessageStruct  # type: ignore
from amitools.vamos.machine.regs import REG_A0, REG_A6, REG_A7  # type: ignore


def create_task(vh, stack_size=8192, name="handler_task"):
    alloc = vh.alloc
    mem = alloc.get_mem()
    tsk_mem = alloc.alloc_memory(TaskStruct.get_size(), label=name)
    tsk = AccessStruct(mem, TaskStruct, tsk_mem.addr)
    tsk.w_s("tc_Node.ln_Type", NodeType.NT_TASK)
    stack = Stack.alloc(alloc, stack_size, name=name + "_stack")
    tsk.w_s("tc_SPUpper", stack.get_upper())
    tsk.w_s("tc_SPLower", stack.get_lower())
    tsk.w_s("tc_SPReg", stack.get_initial_sp())
    return tsk_mem.addr, stack


def set_this_task(vh, task_bptr):
    mem = vh.alloc.get_mem()
    exec_base_addr = mem.r32(4)
    # ExecBase at addr; ThisTask at offset 276 (tc_CurrentTask) in exec V33+, here we hardcode offset for simplicity
    # For simplicity, assume ExecBase struct at least has ThisTask at offset 276 (0x114) as in 2.0+
    this_task_off = 0x114
    mem.w32(exec_base_addr + this_task_off, task_bptr)


def create_msgport(vh, task_bptr):
    alloc = vh.alloc
    mem = alloc.get_mem()
    mp_mem = alloc.alloc_memory(MsgPortStruct.get_size(), label="MsgPort")
    mp = AccessStruct(mem, MsgPortStruct, mp_mem.addr)
    mp.w_s("mp_Node.ln_Type", NodeType.NT_MSGPORT)
    mp.w_s("mp_Flags", 0)
    mp.w_s("mp_SigBit", 0)
    mp.w_s("mp_SigTask", task_bptr << 2)
    # init list
    lst = AccessStruct(mem, ListStruct, mp_mem.addr + 20)
    lst.w_s("lh_Head", 0)
    lst.w_s("lh_Tail", 0)
    lst.w_s("lh_TailPred", 0)
    lst.w_s("lh_Type", NodeType.NT_MESSAGE)
    return mp_mem.addr


def build_packet(mem, alloc, msg_port_bptr, pkt_type, args):
    pkt_mem = alloc.alloc_memory(DosPacketStruct.get_size(), label="DosPacket")
    msg_mem = alloc.alloc_memory(MessageStruct.get_size(), label="PacketMsg")
    pkt = AccessStruct(mem, DosPacketStruct, pkt_mem.addr)
    msg = AccessStruct(mem, MessageStruct, msg_mem.addr)
    pkt.w_s("dp_Link", msg_mem.addr)
    pkt.w_s("dp_Port", msg_port_bptr << 2)
    pkt.w_s("dp_Type", pkt_type)
    # fill args
    for i, val in enumerate(args[:7], start=1):
        pkt.w_s(f"dp_Arg{i}", val)
    # message
    msg.w_s("mn_Node.ln_Type", NodeType.NT_MESSAGE)
    msg.w_s("mn_ReplyPort", msg_port_bptr << 2)
    msg.w_s("mn_Length", MessageStruct.get_size())
    # link msg name to packet
    msg.w_s("mn_Node.ln_Name", pkt_mem.addr)
    return pkt_mem.addr, msg_mem.addr


def start_handler(vh, fssm_bptr, entry_addr, stack_size=8192):
    task_addr, stack = create_task(vh, stack_size=stack_size, name="handler_task")
    set_this_task(vh, task_addr >> 2)
    # set exec StackSwap bounds for this task
    if hasattr(vh.slm, "exec_impl"):
        vh.slm.exec_impl.stk_lower = stack.get_lower()
        vh.slm.exec_impl.stk_upper = stack.get_upper()
    mem = vh.alloc.get_mem()
    exec_base_addr = mem.r32(4)
    start_regs = {REG_A0: fssm_bptr << 2, REG_A6: exec_base_addr, REG_A7: stack.get_initial_sp()}
    task = Task("handler_task", entry_addr, stack, start_regs=start_regs)
    vh.scheduler.add_task(task)
    return task_addr
