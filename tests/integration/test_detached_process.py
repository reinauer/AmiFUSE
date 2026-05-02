"""Integration tests for detached process lifecycle."""

import os
import subprocess
import sys
import time

import pytest

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only"),
    pytest.mark.windows,
]

DETACHED_FLAGS = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW


class TestDetachedProcess:
    """Real OS-level detached process tests."""

    def test_detached_process_detected_by_pid_exists(self):
        """A detached process should be visible to _pid_exists()."""
        from amifuse.platform import _pid_exists

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=DETACHED_FLAGS,
        )
        try:
            assert _pid_exists(proc.pid)
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_kill_pids_terminates_detached_process(self):
        """kill_pids() should terminate a detached process (graceful then force)."""
        from amifuse.platform import _pid_exists, kill_pids

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            creationflags=DETACHED_FLAGS,
        )
        pid = proc.pid
        try:
            assert _pid_exists(pid)
            killed = kill_pids([pid], timeout=3.0)
            assert pid in killed
            # Verify process actually exited
            retcode = proc.wait(timeout=5)
            assert retcode is not None, "Process did not exit after kill_pids"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_pid_exists_false_for_dead_process(self):
        """_pid_exists() should return False for a terminated process."""
        from amifuse.platform import _pid_exists

        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            creationflags=DETACHED_FLAGS,
        )
        proc.wait(timeout=5)
        pid = proc.pid
        # Release the Popen handle so Windows can fully reclaim the PID
        del proc

        time.sleep(0.5)

        assert not _pid_exists(pid)
