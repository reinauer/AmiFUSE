"""Exercise the actual Windows process query with non-OEM characters."""
import subprocess
import sys

import pytest

from amifuse import platform as plat


@pytest.mark.skipif(sys.platform != "win32", reason="Windows CIM query")
def test_native_cim_preserves_unicode_arguments():
    image = "C:/José-Ω-ā-漢字.hdf"
    mountpoint = "C:/mounts/ā-漢字"
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                "amifuse", "mount", image, "--mountpoint", mountpoint])
    try:
        mounts = plat._find_amifuse_mounts_windows()
        mount = next(m for m in mounts if m["pid"] == process.pid)
        assert mount["image"] == image
        assert mount["mountpoint"] == mountpoint
    finally:
        process.terminate()
        process.wait(timeout=10)
