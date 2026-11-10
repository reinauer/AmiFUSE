"""How write_empty_reply() reads a handler's answer to a write of no bytes.

BFFS refuses with DOSFALSE, 0 bytes and an error code in dp_Res2, while
FastFileSystem 47.4 writes the 0 bytes asked for and sets dp_Res2 to -1,
which is no error code (#426).
"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from amifuse.fuse_fs import HandlerBridge


@pytest.mark.parametrize("reply, expected", [
    ((0, 0), (0, 0)),
    ((0, -1), (0, 0)),       # FastFileSystem 47.4
    ((0, 214), (-1, 214)),   # DOSFALSE, ERROR_DISK_WRITE_PROTECTED
    ((0, 223), (-1, 223)),   # DOSFALSE, ERROR_WRITE_PROTECTED
    ((-1, 221), (-1, 221)),  # ERROR_DISK_FULL
    (None, (-1, 0)),         # no reply
])
def test_write_empty_reply(reply, expected):
    bridge = object.__new__(HandlerBridge)
    bridge._lock = threading.RLock()
    bridge._unwritten_outputs = {0x100}
    bridge.state = MagicMock()
    bridge.launcher = MagicMock()
    bridge.launcher.send_write_handle.return_value = (0x500, None)
    bridge._alloc_empty_buf = MagicMock(return_value=SimpleNamespace(addr=0x2000))
    bridge._run_until_reply = MagicMock(return_value=reply)

    assert bridge.write_empty_reply(0x100) == expected
    bridge.launcher.send_write_handle.assert_called_once_with(
        bridge.state, 0x100, 0x2000, 0)
    assert bridge._unwritten_outputs == set()
