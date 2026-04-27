"""Unit tests for amifuse.windows_shell module.

Mocks winreg and ctypes entirely so tests run on all platforms.
Uses a dict-based fake registry to track CreateKey/SetValueEx/QueryValueEx calls.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fake winreg implementation
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Dict-based fake Windows registry.

    Keys are stored as (hkey, subpath) -> {values: {name: (data, type)}, exists: True}.
    Supports CreateKey, OpenKey, SetValueEx, QueryValueEx, DeleteKey, DeleteValue,
    EnumKey, EnumValue, CloseKey.
    """

    HKEY_CURRENT_USER = 0x80000001
    HKEY_LOCAL_MACHINE = 0x80000002

    REG_SZ = 1
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002

    def __init__(self):
        self._keys: dict[str, dict] = {}
        self._next_handle = 100

    def _norm(self, hkey, sub_key=None):
        if sub_key is not None:
            return f"{hkey}\\{sub_key}"
        return str(hkey)

    def CreateKey(self, hkey, sub_key):
        path = self._norm(hkey, sub_key)
        if path not in self._keys:
            self._keys[path] = {"values": {}}
        handle = self._next_handle
        self._next_handle += 1
        # Store path on handle for later lookups
        self._keys[f"_handle_{handle}"] = path
        return handle

    CreateKeyEx = CreateKey

    def OpenKey(self, hkey, sub_key, reserved=0, access=0):
        path = self._norm(hkey, sub_key)
        if path not in self._keys:
            raise FileNotFoundError(f"Registry key not found: {path}")
        handle = self._next_handle
        self._next_handle += 1
        self._keys[f"_handle_{handle}"] = path
        return handle

    def SetValueEx(self, key, name, reserved, type_, value):
        path = self._keys.get(f"_handle_{key}")
        if path is None:
            raise OSError("Invalid handle")
        if path not in self._keys:
            self._keys[path] = {"values": {}}
        self._keys[path]["values"][name] = (value, type_)

    def QueryValueEx(self, key, name):
        path = self._keys.get(f"_handle_{key}")
        if path is None:
            raise OSError("Invalid handle")
        entry = self._keys.get(path)
        if entry is None or name not in entry["values"]:
            raise FileNotFoundError(f"Value not found: {name}")
        val, typ = entry["values"][name]
        return (val, typ)

    def EnumKey(self, key, index):
        path = self._keys.get(f"_handle_{key}")
        if path is None:
            raise OSError("Invalid handle")
        children = []
        prefix = path + "\\"
        for k in self._keys:
            if k.startswith("_handle_"):
                continue
            if k.startswith(prefix):
                rest = k[len(prefix):]
                child = rest.split("\\")[0]
                if child and child not in children:
                    children.append(child)
        if index >= len(children):
            raise OSError("No more data")
        return children[index]

    def EnumValue(self, key, index):
        path = self._keys.get(f"_handle_{key}")
        if path is None:
            raise OSError("Invalid handle")
        entry = self._keys.get(path, {"values": {}})
        items = list(entry["values"].items())
        if index >= len(items):
            raise OSError("No more data")
        name, (val, typ) = items[index]
        return (name, val, typ)

    def DeleteKey(self, hkey, sub_key):
        path = self._norm(hkey, sub_key)
        if path in self._keys:
            del self._keys[path]
        else:
            raise FileNotFoundError(f"Key not found: {path}")

    def DeleteValue(self, key, name):
        path = self._keys.get(f"_handle_{key}")
        if path is None:
            raise OSError("Invalid handle")
        entry = self._keys.get(path)
        if entry is None or name not in entry["values"]:
            raise FileNotFoundError(f"Value not found: {name}")
        del entry["values"][name]

    def CloseKey(self, key):
        pass

    def get_value(self, hkey, sub_key, name=""):
        """Helper to read a value without handles."""
        path = self._norm(hkey, sub_key)
        entry = self._keys.get(path)
        if entry is None or name not in entry["values"]:
            return None
        return entry["values"][name][0]

    def key_exists(self, hkey, sub_key):
        """Helper to check if a key exists."""
        path = self._norm(hkey, sub_key)
        return path in self._keys


@pytest.fixture
def fake_registry(monkeypatch):
    """Install a fake winreg module backed by _FakeRegistry and return the registry."""
    reg = _FakeRegistry()

    mod = types.ModuleType("winreg")
    for attr in (
        "HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE", "REG_SZ",
        "KEY_READ", "KEY_SET_VALUE",
        "CreateKey", "CreateKeyEx", "OpenKey", "SetValueEx", "QueryValueEx",
        "EnumKey", "EnumValue", "DeleteKey", "DeleteValue", "CloseKey",
    ):
        setattr(mod, attr, getattr(reg, attr))

    monkeypatch.setitem(sys.modules, "winreg", mod)
    monkeypatch.setattr("sys.platform", "win32")

    # Mock ctypes.windll for _notify_shell_change
    mock_windll = MagicMock()
    ctypes_mod = sys.modules.get("ctypes")
    monkeypatch.setattr(ctypes_mod, "windll", mock_windll)

    return reg, mock_windll


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_creates_progid_keys(self, fake_registry, tmp_path, monkeypatch):
        """ProgID keys created for each extension."""
        reg, _ = fake_registry
        monkeypatch.setattr("amifuse.windows_shell.ICON_DIR", tmp_path / "icons")

        from amifuse.windows_shell import register
        register()

        assert reg.key_exists(reg.HKEY_CURRENT_USER, r"Software\Classes\AmiFUSE.DiskImage")
        assert reg.key_exists(reg.HKEY_CURRENT_USER, r"Software\Classes\AmiFUSE.FloppyImage")

    def test_register_creates_flat_verb_keys(self, fake_registry, tmp_path, monkeypatch):
        """mount and mountrw verbs created under ProgID."""
        reg, _ = fake_registry
        monkeypatch.setattr("amifuse.windows_shell.ICON_DIR", tmp_path / "icons")

        from amifuse.windows_shell import register
        register()

        base = r"Software\Classes\AmiFUSE.DiskImage"
        assert reg.key_exists(reg.HKEY_CURRENT_USER, rf"{base}\shell\mount")
        assert reg.key_exists(reg.HKEY_CURRENT_USER, rf"{base}\shell\mount\command")
        assert reg.key_exists(reg.HKEY_CURRENT_USER, rf"{base}\shell\mountrw")
        assert reg.key_exists(reg.HKEY_CURRENT_USER, rf"{base}\shell\mountrw\command")

    def test_register_creates_open_with_progids(self, fake_registry, tmp_path, monkeypatch):
        """OpenWithProgids entry created for each extension."""
        reg, _ = fake_registry
        monkeypatch.setattr("amifuse.windows_shell.ICON_DIR", tmp_path / "icons")

        from amifuse.windows_shell import register
        register()

        val = reg.get_value(
            reg.HKEY_CURRENT_USER,
            r"Software\Classes\.hdf\OpenWithProgids",
            "AmiFUSE.DiskImage",
        )
        assert val == ""

    def test_register_warns_existing_progid(self, fake_registry, tmp_path, monkeypatch, capsys):
        """When .hdf already has another app's ProgID, warning is printed."""
        reg, _ = fake_registry
        monkeypatch.setattr("amifuse.windows_shell.ICON_DIR", tmp_path / "icons")

        # Pre-populate .hdf with another app's ProgID
        ext_path = r"Software\Classes\.hdf"
        h = reg.CreateKey(reg.HKEY_CURRENT_USER, ext_path)
        reg.SetValueEx(h, "", 0, reg.REG_SZ, "OtherApp.DiskImage")

        from amifuse.windows_shell import register
        register([".hdf"])

        captured = capsys.readouterr()
        assert "already associated" in captured.out
        assert "OtherApp.DiskImage" in captured.out

    def test_register_idempotent(self, fake_registry, tmp_path, monkeypatch):
        """Running register twice doesn't error."""
        reg, _ = fake_registry
        monkeypatch.setattr("amifuse.windows_shell.ICON_DIR", tmp_path / "icons")

        from amifuse.windows_shell import register
        register()
        register()  # Should not raise

    def test_notify_shell_change_called(self, fake_registry, tmp_path, monkeypatch):
        """SHChangeNotify called after register."""
        reg, mock_windll = fake_registry
        monkeypatch.setattr("amifuse.windows_shell.ICON_DIR", tmp_path / "icons")

        from amifuse.windows_shell import register
        register()

        mock_windll.shell32.SHChangeNotify.assert_called()


class TestUnregister:
    def test_unregister_removes_progid(self, fake_registry, tmp_path, monkeypatch):
        """ProgID tree deleted after unregister."""
        reg, _ = fake_registry
        monkeypatch.setattr("amifuse.windows_shell.ICON_DIR", tmp_path / "icons")

        from amifuse.windows_shell import register, unregister
        register()
        unregister()

        assert not reg.key_exists(reg.HKEY_CURRENT_USER, r"Software\Classes\AmiFUSE.DiskImage")

    def test_unregister_removes_empty_extension_keys(self, fake_registry, tmp_path, monkeypatch):
        """Extension keys are fully deleted when empty after unregister."""
        reg, _ = fake_registry
        monkeypatch.setattr("amifuse.windows_shell.ICON_DIR", tmp_path / "icons")

        from amifuse.windows_shell import register, unregister
        register([".hdf"])
        unregister([".hdf"])

        # Extension key should be gone, not left as an empty stub
        assert not reg.key_exists(reg.HKEY_CURRENT_USER, r"Software\Classes\.hdf")

    def test_unregister_preserves_other_apps_default(self, fake_registry, tmp_path, monkeypatch):
        """When another app owns Default, it's preserved."""
        reg, _ = fake_registry
        monkeypatch.setattr("amifuse.windows_shell.ICON_DIR", tmp_path / "icons")

        # Pre-populate with another app owning .hdf
        ext_path = r"Software\Classes\.hdf"
        h = reg.CreateKey(reg.HKEY_CURRENT_USER, ext_path)
        reg.SetValueEx(h, "", 0, reg.REG_SZ, "OtherApp.DiskImage")

        from amifuse.windows_shell import register, unregister
        register([".hdf"])
        unregister([".hdf"])

        # Other app's default should be preserved
        val = reg.get_value(reg.HKEY_CURRENT_USER, ext_path, "")
        assert val == "OtherApp.DiskImage"


class TestIsRegistered:
    def test_is_registered_true(self, fake_registry, tmp_path, monkeypatch):
        """Returns True when registry key exists with correct value."""
        reg, _ = fake_registry
        monkeypatch.setattr("amifuse.windows_shell.ICON_DIR", tmp_path / "icons")

        from amifuse.windows_shell import register, is_registered
        register()
        assert is_registered() is True

    def test_is_registered_false(self, fake_registry, monkeypatch):
        """Returns False when registry key is missing."""
        reg, _ = fake_registry

        from amifuse.windows_shell import is_registered
        assert is_registered() is False


class TestNonWindows:
    def test_non_windows_raises_system_exit(self, monkeypatch):
        """On non-Windows, register raises SystemExit."""
        monkeypatch.setattr("sys.platform", "linux")

        from amifuse.windows_shell import register
        with pytest.raises(SystemExit):
            register()


class TestInstallIcons:
    def test_install_icons_creates_files(self, fake_registry, tmp_path, monkeypatch):
        """Icon files written to ICON_DIR."""
        icon_dir = tmp_path / "icons"
        monkeypatch.setattr("amifuse.windows_shell.ICON_DIR", icon_dir)

        from amifuse.windows_shell import _install_icons
        _install_icons()

        assert (icon_dir / "diskimage.ico").exists()
        assert (icon_dir / "floppyimage.ico").exists()
        # Verify they're valid ICO files (start with ICO header)
        data = (icon_dir / "diskimage.ico").read_bytes()
        assert data[:4] == b"\x00\x00\x01\x00"

    def test_install_icons_includes_tray(self, fake_registry, tmp_path, monkeypatch):
        """_install_icons creates tray.ico."""
        icon_dir = tmp_path / "icons"
        monkeypatch.setattr("amifuse.windows_shell.ICON_DIR", icon_dir)

        from amifuse.windows_shell import _install_icons
        _install_icons()

        assert (icon_dir / "tray.ico").exists()
        data = (icon_dir / "tray.ico").read_bytes()
        assert data[:4] == b"\x00\x00\x01\x00"


class TestRemoveIcons:
    def test_remove_icons_handles_lock(self, fake_registry, tmp_path, monkeypatch):
        """_remove_icons handles PermissionError gracefully."""
        icon_dir = tmp_path / "icons"
        icon_dir.mkdir()
        # Create icon files
        for name in ("diskimage.ico", "floppyimage.ico", "tray.ico"):
            (icon_dir / name).write_bytes(b"\x00\x00\x01\x00")

        monkeypatch.setattr("amifuse.windows_shell.ICON_DIR", icon_dir)
        monkeypatch.setattr("amifuse.windows_shell._LAUNCH_VBS", tmp_path / "launch.vbs")

        # Make one file raise PermissionError on unlink
        original_unlink = Path.unlink

        def guarded_unlink(self, *args, **kwargs):
            if self.name == "diskimage.ico":
                raise PermissionError("locked by Explorer")
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", guarded_unlink)

        from amifuse.windows_shell import _remove_icons
        # Should not raise
        _remove_icons()

        # diskimage.ico should still exist (locked), others removed
        assert (icon_dir / "diskimage.ico").exists()
        assert not (icon_dir / "floppyimage.ico").exists()


class TestRegisterCreatesLaunchVbs:
    def test_install_creates_launch_vbs(self, fake_registry, tmp_path, monkeypatch):
        """register() creates launch.vbs."""
        icon_dir = tmp_path / "icons"
        launch_vbs = tmp_path / "launch.vbs"
        monkeypatch.setattr("amifuse.windows_shell.ICON_DIR", icon_dir)
        monkeypatch.setattr("amifuse.windows_shell._LAUNCH_VBS", launch_vbs)

        from amifuse.windows_shell import register
        register()

        assert launch_vbs.exists()
        content = launch_vbs.read_text(encoding="utf-8")
        assert "WScript" in content or "CreateObject" in content
