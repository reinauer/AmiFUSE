"""
Sets up a minimal vamos environment for loading a filesystem handler.
This stops short of actually running the handler, but it constructs the
machine, memory map, path manager, scheduler, and lib manager, and loads
the handler into memory so we can later jump into it.
"""

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from amitools.vamos.cfg import VamosMainParser
from amitools.vamos.log import log_machine, log_setup
from amitools.vamos.machine import Machine, MemoryMap, Runtime
from amitools.vamos.trace import TraceManager


from amitools.vamos.path import VamosPathManager
from amitools.vamos.schedule import Scheduler, SchedulerConfig
from amitools.vamos.libmgr import SetupLibManager
from amitools.vamos.disk import DiskSession

_LOG_SETUP_DONE = False


def _ensure_vamos_logging(levels):
    global _LOG_SETUP_DONE
    if _LOG_SETUP_DONE or log_machine.handlers:
        _LOG_SETUP_DONE = True
        return
    cfg = SimpleNamespace(
        file=None,
        timestamps=False,
        quiet=False,
        verbose=False,
        levels=levels,
    )
    log_setup(cfg)
    _LOG_SETUP_DONE = True


def end_task(scheduler, task):
    """End a task that the scheduler still has.

    rem_task() throws TaskStop into the task's greenlet, which unwinds its
    wait and the library call it waits in, and the task takes itself out of
    the scheduler. Only a greenlet that has started and not ended can take
    that: for any other, greenlet raises TaskStop straight back here, so the
    task just leaves the scheduler's lists.
    """
    if task.glet:
        scheduler.rem_task(task)
    else:
        scheduler.terminate_task(task)


class VamosHandlerRuntime:
    # Emulated cycles a task runs before the scheduler looks for another
    # ready task.
    SLICE_CYCLES = 50_000

    def __init__(self):
        self.machine = None
        self.mem_map = None
        self.alloc = None
        self.trace_mgr = None
        self.path_mgr = None
        self.scheduler = None
        self.runtime = None
        self.slm: Optional[SetupLibManager] = None
        self.seglist_baddr: Optional[int] = None
        self._temp_dir: Optional[tempfile.TemporaryDirectory] = None
        self.disk_session = DiskSession(
            acknowledge_read_only_writes=True,
            acknowledge_unsupported_commands=True,
        )
        self.scsi_backend = None
        self.scsi_debug = False
        self.timer_device = None

    def setup(self, cpu: Optional[str] = None):
        self.disk_session = DiskSession(
            acknowledge_read_only_writes=True,
            acknowledge_unsupported_commands=True,
        )
        # Use default vamos configs (bin argument required, so pass a dummy).
        mp = VamosMainParser()
        mp.parse(paths=None, args=["dummy"], cfg_dict=None)

        # machine + mem map
        machine_cfg = mp.get_machine_dict().machine
        mem_map_cfg = mp.get_machine_dict().memmap
        # give us enough RAM headroom for relocated handlers at 0x100000+
        if hasattr(machine_cfg, "ram_size") and machine_cfg.ram_size < 8192:
            machine_cfg.ram_size = 8192
        if cpu:
            machine_cfg.cpu = cpu
        trace_cfg = mp.get_trace_dict().trace
        path_cfg = mp.get_path_dict()
        # Create a temp directory for vamos path manager to avoid permission
        # issues when amitools tries to create volume directories in vols_base_dir.
        # Using "/" would try to create /system etc. which requires root.
        self._temp_dir = tempfile.TemporaryDirectory(prefix="amifuse_")
        temp_path = self._temp_dir.name
        # Avoid touching ~/.vamos/volumes; keep auto volumes/assigns disabled here.
        path_cfg["path"]["auto_volumes"] = []
        path_cfg["path"]["auto_assigns"] = []
        path_cfg["path"]["vols_base_dir"] = temp_path
        # Use "root:" (volume root) for cwd/command rather than embedding
        # the host path.  On Windows the host temp path contains a drive
        # letter colon (e.g. C:\…) which is rejected by AmiPath validation
        # (only one colon allowed).  The volume definition below maps root:
        # to the actual temp directory, so "root:" resolves correctly.
        path_cfg["path"]["cwd"] = "root:"
        path_cfg["path"]["command"] = ["root:"]
        # Provide a minimal root: volume pointing to temp dir so cwd resolves.
        path_cfg["volumes"] = [f"root:{temp_path}"]
        path_cfg["assigns"] = {}
        libs_cfg = mp.get_libs_dict()

        try:
            self.machine = Machine.from_cfg(machine_cfg, use_labels=False)
        except ImportError as e:
            raise RuntimeError(
                "machine68k dependency missing. Install amitools extras "
                "(pip install .[full]) or add machine68k to your environment."
            ) from e
        self.mem_map = MemoryMap(self.machine)
        if not self.mem_map.parse_config(mem_map_cfg):
            raise RuntimeError("Failed to parse memory map config")
        self.alloc = self.mem_map.get_alloc()

        # trace manager (mostly disabled by default)
        self.trace_mgr = TraceManager(self.machine)
        self.trace_mgr.parse_config(trace_cfg)

        # path manager
        self.path_mgr = VamosPathManager()
        if not self.path_mgr.parse_config(path_cfg):
            raise RuntimeError("Failed to parse path manager config")
        if not self.path_mgr.setup():
            raise RuntimeError("Failed to setup path manager")

        # The scheduler runs the handler and the processes it creates.
        # A slice is how long a task runs before the scheduler may switch
        # to another ready one; the bridge's cycle budgets are its multiples.
        self.scheduler = Scheduler(
            self.machine, SchedulerConfig(slice_cycles=self.SLICE_CYCLES)
        )

        # Create a runtime for m68k code execution
        self.runtime = Runtime(self.machine, self.machine.scratch_end)

        # runner function for library code execution
        def runner(code, name=None):
            task = self.scheduler.get_cur_task()
            if task:
                return task.sub_run(code, name=name)
            else:
                return self.runtime.run(code, name=name)

        # libs
        self.slm = SetupLibManager(
            self.machine, self.mem_map, runner, self.scheduler, self.path_mgr
        )
        if not self.slm.parse_config(libs_cfg):
            raise RuntimeError("Failed to parse lib manager config")
        self.slm.setup()
        from amifuse.clock_libraries import AmiFuseDosLibrary, AmiFuseIntuitionLibrary
        from amifuse.exec_library import AmiFuseExecLibrary
        # Before exec.library and dos.library open: a stub binds the methods
        # then. The clock reads tell local time (clock_libraries).
        self.slm.lib_mgr.add_impl_cls("exec.library", AmiFuseExecLibrary)

        # One timer device for the runtime, so that pending requests survive
        # the device being expunged and opened again, and the bridge can
        # reach it. dos.library's Delay() waits on it too. Made when first
        # needed: it takes exec.library, which opens before dos.library.
        def make_dos_library():
            dos_lib = AmiFuseDosLibrary()
            dos_lib.delay_timer = self._get_timer_device()
            return dos_lib

        self.slm.lib_mgr.add_impl_cls("dos.library", make_dos_library)
        self.slm.lib_mgr.add_impl_cls("intuition.library", AmiFuseIntuitionLibrary)
        self.slm.open_base_libs()
        # Register the shared vamos disk device. The backend is supplied by
        # HandlerBridge after it has applied AmiFUSE's image detection.
        self.disk_session.install_devices(self.slm)
        from amifuse.null_device import NullDevice  # lazy import to avoid cycles
        self.slm.lib_mgr.add_impl_cls("keyboard.device", NullDevice)
        self.slm.lib_mgr.add_impl_cls("timer.device", self._get_timer_device)
        self.slm.lib_mgr.add_impl_cls("console.device", NullDevice)


    def _get_timer_device(self):
        """The runtime's AmiFuseTimerDevice, made on first use."""
        if self.timer_device is None:
            from amifuse.timer_device import AmiFuseTimerDevice

            self.timer_device = AmiFuseTimerDevice(self.slm.exec_impl)
        return self.timer_device

    def set_scsi_backend(self, backend, debug=False):
        self.scsi_backend = backend
        self.scsi_debug = debug
        self.disk_session.debug = debug
        self.disk_session.set_backend(0, backend, owned=False)

    def enable_trace(self, show_regs: bool = False):
        if not self.machine:
            return
        _ensure_vamos_logging({"machine": "info"})
        log_machine.setLevel(logging.INFO)
        self.trace_mgr.setup_cpu_instr_trace(show_regs)

    def load_handler(self, handler_path: Path,
                     driver_name: Optional[str] = None) -> int:
        """Load the handler and return its seglist.

        Errors name the handler by its path, or by driver_name if given: a
        driver extracted from the RDB is at a temporary path, gone by the
        time the error is shown.
        """
        if not self.slm:
            raise RuntimeError("setup() not called")
        name = driver_name or f"handler {handler_path}"

        def raise_os_error(exc: OSError):
            # The file could not be read: a reported refusal. An error that
            # names the file, by the path the user gave, stands as it is.
            if exc.filename is not None and driver_name is None:
                raise exc
            text = f"Failed to load {name}: {exc.strerror or exc}"
            raise (OSError(text) if exc.errno is None
                   else OSError(exc.errno, text)) from exc

        # A bad handler file is a user error: raise OSError or ValueError,
        # which mount and format report in one line. The loader returns 0
        # both for a missing file and for one in no format it knows.
        try:
            os.stat(handler_path)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Failed to load {name}: no such file") from None
        except OSError as exc:
            # E.g. an unreadable folder.
            raise_os_error(exc)
        if not os.path.isfile(handler_path):
            raise ValueError(
                f"Failed to load {name}: not a regular file")
        try:
            seg_baddr = self.slm.seg_loader.load_sys_seglist(str(handler_path))
        except OSError as exc:
            raise_os_error(exc)
        except Exception as exc:
            # The file is the loader's only input. Its parsers fail on a
            # damaged file with assorted errors (HunkParseError, IndexError,
            # struct.error, ...), and so can vamos itself, e.g. when a
            # segment does not fit in memory. --debug still shows the
            # original.
            raise ValueError(
                f"Failed to load {name}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if seg_baddr == 0:
            raise ValueError(
                f"Failed to load {name}: not a loadable "
                f"Amiga executable (HUNK or ELF)"
            )
        self.seglist_baddr = seg_baddr
        return seg_baddr

    def shutdown(self):
        first_error = None
        # The handler, and any child it created, is still suspended in a
        # wait. A greenlet suspended in a frame that refers back to it is
        # never collected, so every task is ended first; the scheduler, its
        # tasks and what they reference can then go.
        scheduler, self.scheduler = self.scheduler, None
        if scheduler is not None:
            try:
                self._stop_tasks(scheduler)
            except BaseException as exc:
                first_error = exc
        # Handlers may leave internal library/device state in a partially
        # torn-down state by the time AmiFuse unmounts. Calling
        # close_base_libs() here can end up touching stale Library structs
        # during open-count updates, which crashes inside machine68k. For this
        # short-lived runtime we let the lib manager's shutdown path decide
        # what can still be expunged safely and then tear down the machine.
        for attr, method in (
            ("disk_session", "close"),
            ("slm", "cleanup"),
            ("path_mgr", "shutdown"),
            ("mem_map", "cleanup"),
            ("machine", "cleanup"),
            ("_temp_dir", "cleanup"),
        ):
            resource = getattr(self, attr)
            setattr(self, attr, None)
            try:
                if resource is not None:
                    getattr(resource, method)()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        self.disk_session = DiskSession(
            acknowledge_read_only_writes=True,
            acknowledge_unsupported_commands=True,
        )
        self.scsi_backend = None
        self.timer_device = None
        if first_error is not None:
            raise first_error

    @staticmethod
    def _stop_tasks(scheduler):
        """End every task the scheduler still has, the current one included."""
        from amitools.vamos.schedule import TaskState  # type: ignore

        tasks = list(scheduler.ready_tasks) + list(scheduler.waiting_tasks)
        current = scheduler.get_cur_task()
        if current is not None and current not in tasks:
            tasks.append(current)
        for task in tasks:
            if task.get_state() != TaskState.TS_REMOVED:
                end_task(scheduler, task)
