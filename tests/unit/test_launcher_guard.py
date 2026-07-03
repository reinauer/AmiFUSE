"""Cross-platform guard test for amifuse.launcher.

Separate from ``test_launcher.py`` (which is Windows-only via ``pytestmark``)
so this runs on every CI platform. Monkeypatches ``sys.platform`` to a
non-Windows value and asserts ``main()`` fails cleanly instead of hanging or
raising an uncaught ``AttributeError`` from Windows-only ctypes calls.
"""

from __future__ import annotations

import sys

import pytest

import amifuse.launcher


def test_main_exits_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    # ["open", "x.hdf"] would otherwise reach argparse dispatch and the
    # Windows-only mount/ctypes path; the guard must fire first.
    with pytest.raises(SystemExit, match="only supported on Windows"):
        amifuse.launcher.main(["open", "x.hdf"])
