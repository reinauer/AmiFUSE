"""Integration test conftest -- machine68k probe + fixture providers."""
import os
import shutil
import string
import subprocess
import sys
import time

import pytest

from tests.fixtures.paths import FIXTURE_ROOT, PFS3AIO, PFS3_HDF, OFS_ADF


_MOUNT_POLL_INTERVAL = 0.5   # seconds between ismount checks
_MOUNT_TIMEOUT = 15.0        # max seconds to wait for mount
_UNMOUNT_TIMEOUT = 10.0      # max seconds to wait for unmount subprocess
_PROCESS_KILL_TIMEOUT = 5.0  # max seconds to wait after kill


def _machine68k_works() -> bool:
    """Subprocess probe -- safe against segfaults from C extension."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import machine68k; machine68k.CPU(1)"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


_m68k_checked = False
_m68k_available = False


def pytest_collection_modifyitems(config, items):
    """Skip integration tests when machine68k or fixtures are unavailable.

    Skip hierarchy: no fixtures -> skip all integration, then
    no machine68k -> skip all integration. Tests that don't need
    either (help, doctor) should use tests/unit/ instead.
    """
    global _m68k_checked, _m68k_available

    has_integration = any(
        item.get_closest_marker("integration") is not None for item in items
    )
    if not has_integration:
        return

    if FIXTURE_ROOT is None:
        skip = pytest.mark.skip(
            reason="No fixture root found (set AMIFUSE_FIXTURE_ROOT or place fixtures in ~/AmigaOS/AmiFuse)"
        )
        for item in items:
            if item.get_closest_marker("integration") is not None:
                item.add_marker(skip)
        return

    if not _m68k_checked:
        _m68k_available = _machine68k_works()
        _m68k_checked = True

    if not _m68k_available:
        skip = pytest.mark.skip(reason="machine68k CPU not functional")
        for item in items:
            if item.get_closest_marker("integration") is not None:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def fixture_root():
    if FIXTURE_ROOT is None:
        pytest.skip("No fixture root found")
    return FIXTURE_ROOT


@pytest.fixture(scope="session")
def pfs3_driver():
    if PFS3AIO is None or not PFS3AIO.exists():
        pytest.skip("PFS3 handler not found")
    return PFS3AIO


@pytest.fixture(scope="session")
def pfs3_image():
    if PFS3_HDF is None or not PFS3_HDF.exists():
        pytest.skip("PFS3 test image not found")
    return PFS3_HDF


@pytest.fixture(scope="session")
def ofs_adf_image():
    if OFS_ADF is None or not OFS_ADF.exists():
        pytest.skip("OFS ADF image not found")
    return OFS_ADF


@pytest.fixture(scope="session")
def fuse_available():
    """Detect FUSE backend availability; skip all fuse-marked tests if absent.

    Detection strategy:
    - Import `fuse` (fusepy). fusepy raises EnvironmentError at import time
      when libfuse/WinFSP is not installed. ImportError means fusepy itself
      is missing.
    - On Linux only: secondary check for fusermount/fusermount3 binary
      (needed for unmount, not strictly for mount, but indicates fuse3 is
      properly installed).

    Does NOT use platform.check_fuse_available() -- that function only
    checks WinFSP on Windows and is a no-op on macOS/Linux.

    Returns the platform name string ('darwin', 'linux', 'win32').
    """
    # Primary check: can fusepy find a FUSE library?
    try:
        from fuse import FUSE  # noqa: F401
    except EnvironmentError as exc:
        # libfuse/WinFSP not installed
        pytest.skip(f"FUSE backend not available: {exc}")
    except ImportError:
        # fusepy itself not installed
        pytest.skip("fusepy not installed (pip install fusepy)")

    # Secondary check on Linux: verify fusermount binary exists
    if sys.platform.startswith("linux"):
        has_fusermount = (
            shutil.which("fusermount") or shutil.which("fusermount3")
        )
        if not has_fusermount:
            pytest.skip("Neither fusermount nor fusermount3 found on PATH")

    return sys.platform


@pytest.fixture
def mount_image(fuse_available, tmp_path):
    """Factory fixture: mount an Amiga image and yield (process, mountpoint).

    Usage in test:
        def test_something(mount_image, pfs3_image, pfs3_driver):
            proc, mp = mount_image(pfs3_image, driver=pfs3_driver)
            assert os.path.ismount(mp)

    The factory handles:
    - Mountpoint creation (tmpdir on Unix, drive letter on Windows)
    - Launching `amifuse mount --interactive <image> --mountpoint <mp>`
      via subprocess.Popen (--interactive is the flag name; --foreground
      is an alias; both set dest="foreground" to True)
    - Polling os.path.ismount() until True or timeout, with os.listdir()
      as a secondary readiness check
    - Teardown: unmount, kill process, clean up mountpoint

    Parameters accepted by the returned callable:
        image: Path       -- path to disk image
        driver: Path      -- path to filesystem handler binary (optional)
        extra_args: list   -- additional CLI args (optional)
        timeout: float    -- mount detection timeout (default 15s)
    """
    # Track all mounts created by this fixture instance for teardown
    _active_mounts = []

    def _find_available_drive_letter():
        """Find first available drive letter (D: through Z:) on Windows."""
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if letter < 'D':
                continue
            if not (bitmask & (1 << i)):
                return f"{letter}:"
        raise RuntimeError("No available drive letters found (D: through Z:)")

    def _mount(image, driver=None, extra_args=None, timeout=_MOUNT_TIMEOUT):
        # Build mountpoint
        if sys.platform.startswith("win"):
            # Windows: use drive-letter mounts for reliable os.path.ismount()
            # os.path.ismount() returns False for directory-based FUSE mounts
            # on Windows, so we use drive letters instead
            mountpoint_str = _find_available_drive_letter()
            mountpoint = mountpoint_str + "\\"
        else:
            # Unix: use tmp_path subdirectory
            mountpoint = tmp_path / f"mnt_{len(_active_mounts)}"
            mountpoint.mkdir(exist_ok=True)
            mountpoint_str = str(mountpoint)

        # Build command -- pass drive letter without trailing backslash on Windows
        mp_arg = mountpoint_str if sys.platform.startswith("win") else str(mountpoint)
        cmd = [
            sys.executable, "-m", "amifuse", "mount",
            str(image),
            "--interactive",  # prevents daemonization; alias for --foreground
            "--mountpoint", mp_arg,
        ]
        if driver is not None:
            cmd.extend(["--driver", str(driver)])
        if extra_args:
            cmd.extend(extra_args)

        # Launch mount process
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # On Windows, CREATE_NEW_PROCESS_GROUP allows clean termination
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                if sys.platform.startswith("win") else 0
            ),
        )

        # Poll until mount is detected or timeout
        deadline = time.monotonic() + timeout
        mounted = False
        while time.monotonic() < deadline:
            # Check if process died early (bad image, missing driver, etc.)
            if proc.poll() is not None:
                stdout = proc.stdout.read().decode(errors="replace")
                stderr = proc.stderr.read().decode(errors="replace")
                raise RuntimeError(
                    f"Mount process exited early (rc={proc.returncode}).\n"
                    f"stdout: {stdout}\nstderr: {stderr}"
                )
            # On Windows, ismount detects the drive letter before the
            # filesystem is fully initialized. Use listdir as the
            # authoritative readiness check on all platforms.
            try:
                os.listdir(str(mountpoint))
                mounted = True
                break
            except OSError:
                pass
            time.sleep(_MOUNT_POLL_INTERVAL)

        if not mounted:
            # Clean up the failed mount attempt
            proc.kill()
            proc.wait(timeout=_PROCESS_KILL_TIMEOUT)
            raise RuntimeError(
                f"Mount not detected at {mountpoint} within {timeout}s. "
                f"Process still running: {proc.poll() is None}"
            )

        _active_mounts.append((proc, mountpoint))
        return proc, mountpoint

    yield _mount

    # === TEARDOWN ===
    # Unmount and kill all mounts created during this test, in reverse order
    for proc, mountpoint in reversed(_active_mounts):
        _teardown_mount(proc, mountpoint)


def _teardown_mount(proc, mountpoint):
    """Unmount and clean up a single mount. Best-effort, never raises."""
    mp_str = str(mountpoint)

    # Step 1: Try `amifuse unmount` (works on all platforms --
    # on Windows it internally does process termination)
    try:
        subprocess.run(
            [sys.executable, "-m", "amifuse", "unmount", mp_str],
            capture_output=True, text=True,
            timeout=_UNMOUNT_TIMEOUT, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Step 2: If process is still alive, escalate
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=_PROCESS_KILL_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError):
            pass

    if proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=_PROCESS_KILL_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Step 3: Drain stdout/stderr to avoid ResourceWarning
    try:
        proc.stdout.close()
        proc.stderr.close()
    except OSError:
        pass

    # Step 4: Clean up mountpoint directory
    # Drive-letter mounts on Windows don't need directory cleanup
    # On Unix, a stale FUSE mount may make the dir inaccessible -- ignore errors
    try:
        if os.path.isdir(mp_str) and not os.path.ismount(mp_str):
            os.rmdir(mp_str)
    except OSError:
        pass


@pytest.fixture
def pfs3_mount(mount_image, pfs3_image, pfs3_driver):
    """Mount the PFS3 test image and yield (process, mountpoint).

    Composes mount_image with the PFS3 fixtures from conftest.
    The pfs3_image and pfs3_driver fixtures already handle skipping
    when fixture files are missing.
    """
    proc, mountpoint = mount_image(pfs3_image, driver=pfs3_driver)
    return proc, mountpoint
