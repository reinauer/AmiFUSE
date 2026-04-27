"""Integration tests for Windows shell registration (real registry)."""

import sys
import pytest

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only"),
    pytest.mark.windows,
]


@pytest.fixture(scope="session")
def registry_snapshot():
    """Snapshot relevant HKCU keys before tests, restore after all tests."""
    import winreg

    KEY_PATHS = [
        r"Software\Classes\.hdf",
        r"Software\Classes\.adf",
        r"Software\Classes\AmiFUSE.DiskImage",
        r"Software\Classes\AmiFUSE.FloppyImage",
    ]

    def _snapshot_key(path):
        """Return (exists, {values}, [subkeys]) or (False, {}, [])."""
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ)
        except OSError:
            return (False, {}, [])
        values = {}
        try:
            i = 0
            while True:
                try:
                    name, data, typ = winreg.EnumValue(key, i)
                    values[name] = (data, typ)
                    i += 1
                except OSError:
                    break
        finally:
            winreg.CloseKey(key)
        return (True, values, [])

    snapshots = {p: _snapshot_key(p) for p in KEY_PATHS}
    yield
    # Restore: delete any keys we created, leave pre-existing ones alone
    for path, (existed, _values, _subkeys) in snapshots.items():
        if not existed:
            # Key didn't exist before -- remove it if it exists now
            from amifuse.windows_shell import _delete_key_recursive
            _delete_key_recursive(winreg.HKEY_CURRENT_USER, path)


@pytest.fixture(autouse=True)
def clean_registration(registry_snapshot):
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
