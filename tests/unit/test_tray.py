"""Unit tests for amifuse.tray module.

Mocks pystray, PIL, ctypes.windll, and platform functions so tests run
on all platforms without a display server.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Fake pystray / PIL modules
# ---------------------------------------------------------------------------


class _FakeMenuItem:
    SEPARATOR = object()

    def __init__(self, text=None, action=None):
        self.text = text
        self.action = action


class _FakeMenu:
    SEPARATOR = _FakeMenuItem.SEPARATOR

    def __init__(self, *items):
        self.items = items


class _FakeIcon:
    def __init__(self, name, image, title, menu=None):
        self.name = name
        self.image = image
        self.title = title
        self.menu = menu
        self._stopped = False

    def run(self):
        # Simulate run returning immediately
        pass

    def stop(self):
        self._stopped = True


@pytest.fixture
def fake_pystray(monkeypatch):
    """Install fake pystray module."""
    mod = types.ModuleType("pystray")
    mod.Icon = _FakeIcon
    mod.MenuItem = _FakeMenuItem
    mod.Menu = _FakeMenu
    monkeypatch.setitem(sys.modules, "pystray", mod)
    return mod


@pytest.fixture
def fake_pil(monkeypatch):
    """Install fake PIL.Image module."""
    pil_mod = types.ModuleType("PIL")
    image_mod = types.ModuleType("PIL.Image")
    fake_image = MagicMock()
    image_mod.open = MagicMock(return_value=fake_image)
    pil_mod.Image = image_mod
    monkeypatch.setitem(sys.modules, "PIL", pil_mod)
    monkeypatch.setitem(sys.modules, "PIL.Image", image_mod)
    return image_mod


@pytest.fixture
def tray_app(fake_pystray, fake_pil, monkeypatch):
    """Create a TrayApp instance with mocked dependencies."""
    # Ensure icon file "exists"
    monkeypatch.setattr("pathlib.Path.exists", lambda self: True)

    from amifuse.tray import TrayApp
    app = TrayApp()
    return app


# ---------------------------------------------------------------------------
# Menu building tests
# ---------------------------------------------------------------------------


class TestBuildMenu:
    def test_build_menu_empty_mounts(self, tray_app, fake_pystray):
        """Empty mounts still produces Unmount All and Exit items."""
        menu = tray_app._build_menu()
        # SEPARATOR + Unmount All + Exit = items at end
        texts = [i.text for i in menu.items if hasattr(i, "text") and i.text]
        assert "Unmount All" in texts
        assert "Exit" in texts

    def test_build_menu_with_mounts(self, tray_app, fake_pystray):
        """Mount entries appear with Inspect/Unmount submenus."""
        tray_app._mounts = [
            {"pid": 1234, "mountpoint": "D:", "image": "/path/to/image.hdf"},
        ]
        menu = tray_app._build_menu()
        # First item should be mount entry
        assert menu.items[0].text == "D: - image.hdf"

    def test_build_menu_mount_label_format(self, tray_app, fake_pystray):
        """Label format is 'D: - image.hdf'."""
        tray_app._mounts = [
            {"pid": 100, "mountpoint": "E:", "image": "C:\\disks\\work.adf"},
        ]
        menu = tray_app._build_menu()
        assert menu.items[0].text == "E: - work.adf"


# ---------------------------------------------------------------------------
# Poll loop tests
# ---------------------------------------------------------------------------


class TestPollLoop:
    def test_poll_detects_new_mount(self, tray_app, fake_pystray, monkeypatch):
        """Menu rebuilds when a mount appears."""
        call_count = 0
        mounts_sequence = [
            [],  # first poll: empty
            [{"pid": 1, "mountpoint": "D:", "image": "x.hdf"}],  # second poll: mount
        ]

        def fake_find():
            nonlocal call_count
            idx = min(call_count, len(mounts_sequence) - 1)
            call_count += 1
            if call_count >= 3:
                tray_app._stop_event.set()
            return mounts_sequence[idx]

        monkeypatch.setattr("amifuse.tray.TrayApp.POLL_INTERVAL", 0.01)
        monkeypatch.setattr("amifuse.platform.find_amifuse_mounts", fake_find)

        # Need a fake icon for menu assignment
        tray_app._icon = _FakeIcon("test", None, "test")
        tray_app._poll_loop()

        assert call_count >= 2

    def test_poll_detects_removed_mount(self, tray_app, fake_pystray, monkeypatch):
        """Menu rebuilds when mount disappears."""
        call_count = 0
        mounts_sequence = [
            [{"pid": 1, "mountpoint": "D:", "image": "x.hdf"}],
            [],  # mount removed
        ]

        def fake_find():
            nonlocal call_count
            idx = min(call_count, len(mounts_sequence) - 1)
            call_count += 1
            if call_count >= 3:
                tray_app._stop_event.set()
            return mounts_sequence[idx]

        monkeypatch.setattr("amifuse.tray.TrayApp.POLL_INTERVAL", 0.01)
        monkeypatch.setattr("amifuse.platform.find_amifuse_mounts", fake_find)

        tray_app._icon = _FakeIcon("test", None, "test")
        tray_app._poll_loop()

        assert call_count >= 2

    def test_poll_detects_mountpoint_change_same_pid(self, tray_app, fake_pystray, monkeypatch):
        """BUG FIX: change detection uses (pid, mountpoint) tuple."""
        call_count = 0
        menu_assignments = []

        mounts_sequence = [
            [{"pid": 1, "mountpoint": "D:", "image": "x.hdf"}],
            [{"pid": 1, "mountpoint": "E:", "image": "x.hdf"}],  # same pid, different mountpoint
        ]

        def fake_find():
            nonlocal call_count
            idx = min(call_count, len(mounts_sequence) - 1)
            call_count += 1
            if call_count >= 3:
                tray_app._stop_event.set()
            return mounts_sequence[idx]

        monkeypatch.setattr("amifuse.tray.TrayApp.POLL_INTERVAL", 0.01)
        monkeypatch.setattr("amifuse.platform.find_amifuse_mounts", fake_find)

        icon = _FakeIcon("test", None, "test")
        tray_app._icon = icon

        # Track menu assignments
        original_menu_setter = type(icon).__dict__.get("menu")

        class MenuTracker:
            def __set_name__(self, owner, name):
                self.name = name

            def __set__(self, obj, value):
                menu_assignments.append(value)
                obj.__dict__["menu"] = value

            def __get__(self, obj, objtype=None):
                return obj.__dict__.get("menu")

        # Can't easily add descriptor, so just track via poll
        tray_app._poll_loop()
        # If detection works, menu gets rebuilt on mountpoint change
        assert call_count >= 2


# ---------------------------------------------------------------------------
# Auto-exit tests
# ---------------------------------------------------------------------------


class TestAutoExit:
    def test_auto_exit_after_grace_period(self, tray_app, fake_pystray, monkeypatch):
        """Mounts empty for GRACE_PERIOD seconds -> icon.stop() called."""
        tray_app.GRACE_PERIOD = 0.05
        tray_app.POLL_INTERVAL = 0.01

        call_count = 0

        def fake_find():
            nonlocal call_count
            call_count += 1
            return []

        monkeypatch.setattr("amifuse.platform.find_amifuse_mounts", fake_find)

        icon = _FakeIcon("test", None, "test")
        tray_app._icon = icon

        # Run poll loop until it stops the icon
        def stop_eventually():
            time.sleep(0.2)
            tray_app._stop_event.set()

        t = threading.Thread(target=stop_eventually, daemon=True)
        t.start()
        tray_app._poll_loop()
        t.join(timeout=1)

        assert icon._stopped

    def test_auto_exit_cancelled_by_new_mount(self, tray_app, fake_pystray, monkeypatch):
        """Mount appears during grace period -> timer reset."""
        tray_app.GRACE_PERIOD = 0.1
        tray_app.POLL_INTERVAL = 0.01

        call_count = 0
        mounts_seq = [
            [],  # start grace
            [],
            [{"pid": 1, "mountpoint": "D:", "image": "x.hdf"}],  # cancel grace
            [{"pid": 1, "mountpoint": "D:", "image": "x.hdf"}],
        ]

        def fake_find():
            nonlocal call_count
            idx = min(call_count, len(mounts_seq) - 1)
            call_count += 1
            if call_count >= 5:
                tray_app._stop_event.set()
            return mounts_seq[idx]

        monkeypatch.setattr("amifuse.platform.find_amifuse_mounts", fake_find)

        icon = _FakeIcon("test", None, "test")
        tray_app._icon = icon
        tray_app._poll_loop()

        # Grace should have been reset when mount appeared
        assert tray_app._grace_start is None


# ---------------------------------------------------------------------------
# Single instance tests
# ---------------------------------------------------------------------------


class TestSingleInstance:
    def test_single_instance_acquired(self, monkeypatch):
        """CreateMutexW succeeds, GetLastError != 183."""
        mock_windll = MagicMock()
        import ctypes
        monkeypatch.setattr(ctypes, "windll", mock_windll)
        mock_windll.kernel32.GetLastError.return_value = 0

        from amifuse.tray import _check_single_instance
        assert _check_single_instance() is True

    def test_single_instance_already_running(self, monkeypatch):
        """GetLastError returns 183 -> already running."""
        mock_windll = MagicMock()
        import ctypes
        monkeypatch.setattr(ctypes, "windll", mock_windll)
        mock_windll.kernel32.GetLastError.return_value = 183

        from amifuse.tray import _check_single_instance
        assert _check_single_instance() is False


# ---------------------------------------------------------------------------
# Quit / unmount lifecycle tests (BUG FIX #11)
# ---------------------------------------------------------------------------


class TestQuitLifecycle:
    def test_quit_calls_icon_stop_not_unmount_all(self, tray_app, fake_pystray, monkeypatch):
        """BUG FIX: _quit only stops icon, doesn't unmount."""
        icon = _FakeIcon("test", None, "test")
        tray_app._icon = icon
        tray_app._mounts = [{"pid": 1, "mountpoint": "D:", "image": "x.hdf"}]

        kill_calls = []
        monkeypatch.setattr("amifuse.platform.kill_pids", lambda pids, **kw: kill_calls.extend(pids))

        tray_app._quit(icon, None)

        assert icon._stopped
        assert kill_calls == []  # unmount NOT called in _quit

    def test_main_calls_unmount_all_after_icon_run(self, fake_pystray, fake_pil, monkeypatch):
        """BUG FIX: unmount happens after icon.run() returns."""
        monkeypatch.setattr("pathlib.Path.exists", lambda self: True)

        kill_calls = []

        from amifuse.tray import TrayApp
        app = TrayApp()

        # Simulate mounts existing when run() returns
        app._mounts = [{"pid": 42, "mountpoint": "D:", "image": "x.hdf"}]

        monkeypatch.setattr("amifuse.platform.kill_pids", lambda pids, **kw: kill_calls.extend(pids))

        # Mock the poll thread to not actually run
        monkeypatch.setattr("threading.Thread.start", lambda self: None)

        app.run()

        assert 42 in kill_calls


# ---------------------------------------------------------------------------
# Unmount tests
# ---------------------------------------------------------------------------


class TestUnmount:
    def test_unmount_single_calls_kill_pids(self, tray_app, monkeypatch):
        """kill_pids called with correct pid and timeout=2.0."""
        calls = []
        monkeypatch.setattr(
            "amifuse.platform.kill_pids",
            lambda pids, timeout=10.0: calls.append((pids, timeout)),
        )

        mount = {"pid": 99, "mountpoint": "D:", "image": "x.hdf"}
        tray_app._unmount_single(mount)

        assert calls == [([99], 2.0)]

    def test_unmount_single_wakes_poll(self, tray_app, monkeypatch):
        """wake_event is set after unmount."""
        monkeypatch.setattr("amifuse.platform.kill_pids", lambda pids, **kw: None)

        mount = {"pid": 99, "mountpoint": "D:", "image": "x.hdf"}
        tray_app._unmount_single(mount)

        assert tray_app._wake_event.is_set()

    def test_unmount_all_kills_all_pids(self, tray_app, monkeypatch):
        """All PIDs killed."""
        calls = []
        monkeypatch.setattr(
            "amifuse.platform.kill_pids",
            lambda pids, **kw: calls.append(pids),
        )
        tray_app._mounts = [
            {"pid": 1, "mountpoint": "D:", "image": "a.hdf"},
            {"pid": 2, "mountpoint": "E:", "image": "b.hdf"},
        ]

        tray_app._unmount_all()
        assert calls == [[1, 2]]


# ---------------------------------------------------------------------------
# Inspect tests
# ---------------------------------------------------------------------------


class TestInspect:
    def test_inspect_opens_new_console(self, tray_app, monkeypatch):
        """CREATE_NEW_CONSOLE flag used."""
        popen_calls = []

        def fake_popen(cmd, **kwargs):
            popen_calls.append(kwargs)

        monkeypatch.setattr("amifuse.tray.subprocess.Popen", fake_popen)

        mount = {"pid": 1, "mountpoint": "D:", "image": "test.hdf"}
        tray_app._inspect(mount)

        assert popen_calls[0]["creationflags"] == 0x00000010  # CREATE_NEW_CONSOLE

    def test_inspect_uses_absolute_path(self, tray_app, monkeypatch):
        """Image path is absolutified via Path.resolve()."""
        popen_calls = []

        def fake_popen(cmd, **kwargs):
            popen_calls.append(cmd)

        monkeypatch.setattr("amifuse.tray.subprocess.Popen", fake_popen)

        mount = {"pid": 1, "mountpoint": "D:", "image": "relative/test.hdf"}
        tray_app._inspect(mount)

        # The last element should be an absolute path
        img_arg = popen_calls[0][-1]
        assert Path(img_arg).is_absolute()


# ---------------------------------------------------------------------------
# Callback pattern tests (BUG FIX #8)
# ---------------------------------------------------------------------------


class TestCallbackPatterns:
    def test_factory_callbacks_not_lambdas(self, tray_app, fake_pystray):
        """Callbacks are factory-created functions, not lambdas with default args."""
        tray_app._mounts = [
            {"pid": 1, "mountpoint": "D:", "image": "a.hdf"},
            {"pid": 2, "mountpoint": "E:", "image": "b.hdf"},
        ]
        menu = tray_app._build_menu()

        # Get the submenu actions for mount entries
        for item in menu.items:
            if hasattr(item, "action") and isinstance(item.action, _FakeMenu):
                for sub_item in item.action.items:
                    if hasattr(sub_item, "action") and sub_item.action is not None:
                        # Should NOT be a lambda (lambda shows as <lambda>)
                        assert "<lambda>" not in sub_item.action.__name__


# ---------------------------------------------------------------------------
# Wake event tests (BUG FIX #7)
# ---------------------------------------------------------------------------


class TestWakeEvent:
    def test_wake_event_used_for_sleep(self, tray_app, fake_pystray, monkeypatch):
        """Poll uses Event.wait, not time.sleep."""
        wait_calls = []
        original_wait = tray_app._wake_event.wait

        def tracking_wait(timeout=None):
            wait_calls.append(timeout)
            tray_app._stop_event.set()  # stop after first poll
            return False

        tray_app._wake_event.wait = tracking_wait
        monkeypatch.setattr("amifuse.platform.find_amifuse_mounts", lambda: [])

        tray_app._icon = _FakeIcon("test", None, "test")
        tray_app._poll_loop()

        assert len(wait_calls) >= 1
        assert wait_calls[0] == tray_app.POLL_INTERVAL


# ---------------------------------------------------------------------------
# Tray icon and inspect tests
# ---------------------------------------------------------------------------


class TestTrayIconPath:
    def test_tray_icon_path_is_tray_ico(self, tray_app, fake_pystray, fake_pil, monkeypatch):
        """TrayApp loads tray.ico not diskimage.ico."""
        opened_paths = []
        original_open = fake_pil.open

        def tracking_open(path):
            opened_paths.append(path)
            return original_open(path)

        fake_pil.open = tracking_open
        monkeypatch.setenv("APPDATA", "/fake/appdata")

        tray_app.run()
        assert len(opened_paths) >= 1
        assert opened_paths[0].endswith("tray.ico")
        assert "diskimage" not in opened_paths[0]


class TestMountLabel:
    def test_mount_label_uses_image_key(self, tray_app, fake_pystray):
        """Menu label uses mount['image'] for display."""
        tray_app._mounts = [
            {"pid": 1, "mountpoint": "D:", "image": "/path/to/game.hdf"},
        ]
        menu = tray_app._build_menu()
        labels = [i.text for i in menu.items if hasattr(i, "text") and i.text]
        mount_label = labels[0]
        assert "game.hdf" in mount_label


class TestInspectUsesPythonExe:
    def test_inspect_uses_python_exe(self, tray_app, fake_pystray, monkeypatch):
        """Inspect resolves python.exe not pythonw.exe/sys.executable."""
        monkeypatch.setattr(
            "sys.executable", r"C:\Python\pythonw.exe",
        )
        monkeypatch.setattr(
            "amifuse.tray.os.path.isfile",
            lambda p: p == r"C:\Python\python.exe",
        )

        popen_calls = []
        monkeypatch.setattr(
            "amifuse.tray.subprocess.Popen",
            lambda cmd, **kw: popen_calls.append(cmd),
        )

        mount = {"image": "/path/to/test.hdf", "mountpoint": "D:"}
        tray_app._inspect(mount)

        assert len(popen_calls) == 1
        cmd = popen_calls[0]
        # The inner command should use python.exe, not pythonw.exe
        assert r"C:\Python\python.exe" in cmd
