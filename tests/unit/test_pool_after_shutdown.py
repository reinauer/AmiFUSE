"""A handler's exec Pool may be freed after the runtime has shut down.

The handler's pools stay in exec.library's table, as the base libraries are
not closed at shutdown, and the garbage collector frees them whenever it
gets to them. Freeing returns the memory to the allocator, which must not
then write into the machine's memory, freed by then: it would crash.
"""

import subprocess
import sys

SCRIPT = """
from amifuse.vamos_runner import VamosHandlerRuntime
from amitools.vamos.lib.lexec.Pool import Pool

runtime = VamosHandlerRuntime()
runtime.setup()
exec_impl = runtime.slm.exec_impl
# What CreatePool() and AllocPooled() leave in exec.library's table.
pool = Pool(exec_impl.mem, exec_impl.alloc, 0, 4096, 2048, 1)
exec_impl._pools[1] = pool
assert pool.AllocPooled(None, "x", 64)
runtime.shutdown()
del exec_impl, runtime
pool.__del__()  # as the garbage collector does
print("freed")
"""


def test_a_pool_freed_after_shutdown_leaves_the_machine_memory_alone():
    # In a child process: the failure is a crash, which would end pytest.
    result = subprocess.run([sys.executable, "-c", SCRIPT],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "freed"
