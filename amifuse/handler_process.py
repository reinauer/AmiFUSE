"""The handler's process: a vamos DosProcess with what dos.library asks of one.

vamos' dos.library serves its calls through the current process object,
which it expects to be a full lib.dos.Process: the one vamos builds when
it loads and runs a program, with a CLI, arguments and a shell. A handler
is no such program: it is started from a DeviceNode with a startup
packet. So AmiFUSE gives it a plain task DosProcess and adds the
accessors a handler's DOS calls reach, with the same contracts as
lib.dos.Process, reading the Process structure in memory. It has no CLI
and no arguments, and its standard handles are the empty ones the
launcher gives it.

What it relies on in amitools:
- ExecLibCtx and DosLibCtx accept any DosProcess as the current process.
- DosLibrary reads the process through this_task (the Process structure),
  get_current_dir()/get_home_dir() and their _lock variants (through
  ctx.dos_lib.lock_mgr), set_current_dir(), get_input()/get_output()
  (through ctx.dos_lib.file_mgr), get_cli_struct(), get_local_vars(),
  is_native_shell(), get_program_name() and get_arg_str_ptr().
"""

from amitools.vamos.libstructs.exec_ import MinListStruct  # type: ignore
from amitools.vamos.task import DosProcess  # type: ignore


class HandlerProcess(DosProcess):
    """A DosProcess whose DOS accessors read the Process structure."""

    def __init__(self, dos_ctx, machine, alloc, name, **kw_args):
        # the dos.library context, for its lock and file managers
        self.ctx = dos_ctx
        self.name = name
        super().__init__(machine, alloc, name, **kw_args)

    @property
    def this_task(self):
        return self.proc

    # ----- directories -----

    def get_current_dir(self) -> int:
        return self.proc.pr_CurrentDir.aptr

    def set_current_dir(self, lock: int):
        self.proc.pr_CurrentDir.aptr = lock

    def get_home_dir(self) -> int:
        return self.proc.pr_HomeDir.aptr

    def set_home_dir(self, lock_addr: int):
        self.proc.pr_HomeDir.aptr = lock_addr

    def get_current_dir_lock(self):
        return self.ctx.dos_lib.lock_mgr.get_by_b_addr(self.get_current_dir() >> 2)

    def get_home_dir_lock(self):
        return self.ctx.dos_lib.lock_mgr.get_by_b_addr(self.get_home_dir() >> 2)

    # ----- standard handles -----

    def get_input(self):
        return self.ctx.dos_lib.file_mgr.get_by_b_addr(self.proc.pr_CIS.aptr >> 2)

    def set_input(self, input_fh):
        self.proc.pr_CIS.aptr = 0 if input_fh is None else input_fh.b_addr << 2

    def get_output(self):
        return self.ctx.dos_lib.file_mgr.get_by_b_addr(self.proc.pr_COS.aptr >> 2)

    def set_output(self, output_fh):
        self.proc.pr_COS.aptr = 0 if output_fh is None else output_fh.b_addr << 2

    # ----- CLI and shell: a handler has none -----

    def get_cli_struct(self) -> int:
        return 0

    def is_native_shell(self) -> bool:
        return False

    def get_program_name(self) -> str:
        return self.name

    def get_arg_str_ptr(self) -> int:
        return self.proc.pr_Arguments.aptr

    def get_local_vars(self):
        return MinListStruct(self.ctx.mem, self.proc.pr_LocalVars.addr)
