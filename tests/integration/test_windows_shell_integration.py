"""Integration tests for Windows shell registration (real registry).

The registry is real, but it is not the user's: for this module the process's
HKEY_CURRENT_USER is mapped onto a scratch key, and the AmiFUSE folder under
APPDATA is moved into tmp_path. register() and unregister() then leave the
Explorer registration, icons and launch.vbs of an installed AmiFUSE alone.

The mapping holds only inside this process: a test here that registers
through a child process (``python -m amifuse ...``, ``reg.exe``) would write
the real HKCU. A run killed before teardown (by pytest-timeout's
``os._exit``, for one) leaves its scratch key, HKCU\\Software\\AmiFUSE-test-<hex>,
behind; the registration itself is never touched.
"""

import ctypes
import sys
import uuid

import pytest

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only"),
    pytest.mark.windows,
]


@pytest.fixture(scope="module")
def hkcu_sandbox():
    """Map this process's HKEY_CURRENT_USER onto a scratch key.

    RegOverridePredefKey redirects every use of HKEY_CURRENT_USER in this
    process (not in child processes) to HKCU\\Software\\AmiFUSE-test-<hex>.
    A probe written through HKEY_CURRENT_USER must land in the scratch key
    before any test runs. Afterwards the mapping is removed and the scratch
    key deleted. Yields an open handle to the scratch key.
    """
    import winreg
    from ctypes import wintypes

    from amifuse.windows_shell import _delete_key_recursive

    override = ctypes.WinDLL("advapi32").RegOverridePredefKey
    override.argtypes = [wintypes.HKEY, wintypes.HKEY]
    override.restype = wintypes.LONG

    hkcu = winreg.HKEY_CURRENT_USER
    name = f"AmiFUSE-test-{uuid.uuid4().hex}"
    scratch_path = rf"Software\{name}"
    scratch = winreg.CreateKey(hkcu, scratch_path)
    restored = 0
    try:
        rc = override(hkcu, scratch.handle)
        if rc != 0:
            pytest.fail(f"RegOverridePredefKey failed: {ctypes.FormatError(rc)}")
        try:
            winreg.CloseKey(winreg.CreateKey(hkcu, name))
            try:
                winreg.CloseKey(winreg.OpenKey(scratch, name))
            except OSError:
                pytest.fail("HKEY_CURRENT_USER is not redirected to the scratch key")
            finally:
                # Removes the probe wherever it landed.
                try:
                    winreg.DeleteKey(hkcu, name)
                except OSError:
                    pass
            yield scratch
        finally:
            restored = override(hkcu, None)
    finally:
        # While the mapping is still in place, deleting through
        # HKEY_CURRENT_USER would act inside the scratch key, not on the hive.
        if restored == 0:
            winreg.CloseKey(scratch)
            _delete_key_recursive(hkcu, scratch_path)
            try:
                winreg.CloseKey(winreg.OpenKey(hkcu, scratch_path))
            except FileNotFoundError:
                pass
            else:
                pytest.fail(f"Scratch key HKCU\\{scratch_path} was not deleted")
        else:
            pytest.fail(
                f"RegOverridePredefKey restore failed: {ctypes.FormatError(restored)}; "
                f"HKEY_CURRENT_USER stays mapped onto HKCU\\{scratch_path} in this process"
            )


@pytest.fixture
def appdata_sandbox(monkeypatch, tmp_path):
    """Point APPDATA and the AmiFUSE folder, icons and launch.vbs into tmp_path.

    windows_shell works these paths out from APPDATA at import, so the
    module attributes are patched as well as the variable.
    """
    from amifuse import windows_shell

    amifuse_dir = tmp_path / "AmiFUSE"
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(windows_shell, "_AMIFUSE_DIR", amifuse_dir)
    monkeypatch.setattr(windows_shell, "ICON_DIR", amifuse_dir / "icons")
    monkeypatch.setattr(windows_shell, "_LAUNCH_VBS", amifuse_dir / "launch.vbs")
    return amifuse_dir


@pytest.fixture(autouse=True)
def clean_registration(hkcu_sandbox, appdata_sandbox):
    """Ensure clean state before/after each test."""
    from amifuse.windows_shell import unregister
    try:
        unregister()
    except Exception:
        pass
    yield
    try:
        unregister()
    except Exception:
        pass


class TestWindowsShellRegistration:
    """Real registry round-trip tests using HKCU (no elevation needed)."""

    def test_register_creates_progid_key(self):
        """register() should create the AmiFUSE.DiskImage ProgID key."""
        import winreg
        from amifuse.windows_shell import register

        register()

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Classes\AmiFUSE.DiskImage",
            0,
            winreg.KEY_READ,
        )
        try:
            value, _ = winreg.QueryValueEx(key, "")
            assert value == "Amiga Disk Image"
        finally:
            winreg.CloseKey(key)

    def test_register_creates_file_associations(self):
        """register() should add .hdf to OpenWithProgids."""
        import winreg
        from amifuse.windows_shell import register

        register()

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Classes\.hdf\OpenWithProgids",
            0,
            winreg.KEY_READ,
        )
        try:
            # Should not raise -- value exists
            winreg.QueryValueEx(key, "AmiFUSE.DiskImage")
        finally:
            winreg.CloseKey(key)

    def test_register_installs_icon_files(self):
        """register() should create icon files under APPDATA/AmiFUSE/icons/."""
        from amifuse.windows_shell import register, ICON_DIR

        register()

        assert (ICON_DIR / "diskimage.ico").exists()
        assert (ICON_DIR / "floppyimage.ico").exists()

    def test_register_idempotent(self):
        """Calling register() twice should not error and keys remain correct."""
        import winreg
        from amifuse.windows_shell import register

        register()
        register()  # second call -- should not raise

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Classes\AmiFUSE.DiskImage",
            0,
            winreg.KEY_READ,
        )
        try:
            value, _ = winreg.QueryValueEx(key, "")
            assert value == "Amiga Disk Image"
        finally:
            winreg.CloseKey(key)

    def test_unregister_removes_keys_and_icons(self):
        """register() then unregister() should remove ProgID key and icons."""
        import winreg
        from amifuse.windows_shell import register, unregister, ICON_DIR

        register()
        # Verify precondition
        assert (ICON_DIR / "diskimage.ico").exists()

        unregister()

        # ProgID key should be gone
        with pytest.raises(OSError):
            winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Classes\AmiFUSE.DiskImage",
                0,
                winreg.KEY_READ,
            )

        # Icons should be removed
        assert not (ICON_DIR / "diskimage.ico").exists()
        assert not (ICON_DIR / "floppyimage.ico").exists()

    def test_register_stays_in_sandbox(self, hkcu_sandbox, appdata_sandbox):
        """register() writes into the scratch key, and its verbs run the
        sandboxed launch.vbs."""
        import winreg
        from amifuse.windows_shell import register

        register()

        key = winreg.OpenKey(
            hkcu_sandbox,
            r"Software\Classes\AmiFUSE.DiskImage\shell\open\command",
            0,
            winreg.KEY_READ,
        )
        try:
            command, _ = winreg.QueryValueEx(key, "")
        finally:
            winreg.CloseKey(key)
        launch_vbs = appdata_sandbox / "launch.vbs"
        assert f'"{launch_vbs}"' in command
        assert launch_vbs.exists()
