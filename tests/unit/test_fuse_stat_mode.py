"""Unit tests for the host mode AmigaFuseFS reports for Amiga entries.

WinFsp turns st_mode into the file's ACL, with "other" mapped to Everyone,
so the group/other bits decide whether any other Windows account can open
a file. These tests pin the Windows owner-to-other read/execute mirroring
and that macOS/Linux keep the plain Amiga protection mapping.
"""

import pytest

# The fuse_mock fixture replaces DosProtection with a stub, so capture the
# real class before any fixture patches sys.modules.
try:
    from amitools.vamos.lib.dos.DosProtection import DosProtection as _RealDosProtection
except ImportError:  # pragma: no cover - amitools submodule not checked out
    _RealDosProtection = None

pytestmark = pytest.mark.skipif(
    _RealDosProtection is None, reason="amitools submodule not available"
)

# Amiga protection masks (owner RWED bits are active low)
PROT_RWED = 0x00  # ----rwed
PROT_RW_D = 0x02  # ----rw-d (typical .info file)
PROT_NO_READ = 0x08  # ----_wed (read-protected)


def _make_fs(fuse_mock, monkeypatch, write_enabled):
    import amifuse.fuse_fs as fuse_fs_mod

    monkeypatch.setattr(fuse_fs_mod, "DosProtection", _RealDosProtection)
    bridge = type("MockBridge", (), {"_write_enabled": write_enabled})()
    return fuse_fs_mod.AmigaFuseFS(bridge, debug=False, icons=False)


def _mode(fs, protection, is_dir=False):
    ent = {
        "dir_type": 2 if is_dir else -3,
        "size": 0 if is_dir else 100,
        "num_blocks": 1,
        "protection": protection,
    }
    return fs._stat_from_fib(ent, "/x", 0)["st_mode"]


@pytest.mark.parametrize(
    "protection,is_dir,expected",
    [
        (PROT_RWED, False, 0o100755),
        (PROT_RW_D, False, 0o100644),
        (PROT_RWED, True, 0o040755),
    ],
    ids=["file-rwed", "file-rw-d", "dir-rwed"],
)
def test_windows_mirrors_owner_read_execute(
    fuse_mock, monkeypatch, protection, is_dir, expected
):
    """Other accounts get the owner's read/execute bits, never write."""
    fs = _make_fs(fuse_mock, monkeypatch, write_enabled=True)
    monkeypatch.setattr("sys.platform", "win32")
    assert _mode(fs, protection, is_dir) == expected


def test_windows_read_protected_file_stays_unreadable(fuse_mock, monkeypatch):
    """An Amiga file without the r bit is unreadable for every account."""
    fs = _make_fs(fuse_mock, monkeypatch, write_enabled=True)
    monkeypatch.setattr("sys.platform", "win32")
    assert _mode(fs, PROT_NO_READ) & 0o444 == 0


def test_windows_read_only_mount(fuse_mock, monkeypatch):
    """Read-only mounts strip write for the owner and mirror read/execute."""
    fs = _make_fs(fuse_mock, monkeypatch, write_enabled=False)
    monkeypatch.setattr("sys.platform", "win32")
    assert _mode(fs, PROT_RWED) == 0o100555


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_unix_keeps_amiga_protection_mapping(fuse_mock, monkeypatch, platform):
    """macOS and Linux report the Amiga owner bits without mirroring."""
    fs = _make_fs(fuse_mock, monkeypatch, write_enabled=True)
    monkeypatch.setattr("sys.platform", platform)
    assert _mode(fs, PROT_RWED) == 0o100700
    assert _mode(fs, PROT_RWED, is_dir=True) == 0o040700
