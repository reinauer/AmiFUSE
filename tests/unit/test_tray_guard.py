"""Cross-platform guard test for amifuse.tray.

Mirrors ``test_launcher_guard.py``: monkeypatches ``sys.platform`` to a
non-Windows value and asserts ``main()`` fails cleanly instead of raising an
uncaught ``AttributeError`` from the Windows-only ctypes mutex call.
"""

from __future__ import annotations

import sys

import pytest

import amifuse.tray


def test_main_exits_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SystemExit, match="only supported on Windows"):
        amifuse.tray.main()
