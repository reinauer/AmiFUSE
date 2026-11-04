"""A run on an ISO shows its volume ID escaped, whoever prints it (#361).

The debug geometry comes from HandlerBridge once a real handler has its
bootstrap, and ODFileSystem logs the ID itself, so this runs ODFileSystem.
The image holds no directories, as neither needs one to print.
"""

import pytest

from amifuse import fuse_fs, rdb_inspect
from tests.fixtures.paths import DRIVERS_DIR

pytestmark = pytest.mark.timeout(60)


@pytest.fixture(autouse=True)
def needs_cpu():
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")


def test_debug_geometry_escapes_the_volume_id(tmp_path, capfd):
    driver = DRIVERS_DIR / "ODFileSystem" if DRIVERS_DIR else None
    if driver is None or not driver.exists():
        pytest.skip("ODFileSystem not found")
    pvd = bytearray(b" " * 2048)
    pvd[:6] = b"\x01CD001"
    pvd[40:47] = b"CD\x1b[2J "
    image = tmp_path / "esc.iso"
    image.write_bytes(bytes(16 * 2048) + bytes(pvd) + bytes(2 * 2048))

    try:
        bridge = fuse_fs.HandlerBridge(
            image, driver, debug=True, iso_info=rdb_inspect.detect_iso(image))
    except (OSError, SystemExit, ValueError):
        pass  # no root directory to mount
    else:
        bridge.close()

    captured = capfd.readouterr()
    assert "[amifuse]   Volume: CD\\x1b[2J" in captured.out
    assert "\x1b[2J" not in captured.out
    assert 'volume: "CD\\x1b[2J' in captured.err
    assert "\x1b[2J" not in captured.err
