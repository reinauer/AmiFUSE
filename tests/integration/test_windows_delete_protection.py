"""Delete protection must be refused before WinFsp cleanup reports success."""

import os
import sys
from pathlib import Path

import pytest

from tests.integration.test_bundled_ffs import _seed_adf, _xdftool, _unmount

pytestmark = [pytest.mark.fuse,
              pytest.mark.skipif(sys.platform != "win32", reason="WinFsp")]


def test_protected_file_and_directory_refuse_delete(mount_image, tmp_path):
    image = _seed_adf(tmp_path, "DOS3")
    _xdftool(image, "protect", "Root.txt", "rwe", "+", "makedir", "LockedDir",
             "+", "protect", "LockedDir", "rwe")
    proc, root = mount_image(image, extra_args=["--write"])
    root = Path(root)
    with pytest.raises(PermissionError):
        os.remove(root / "Root.txt")
    with pytest.raises(PermissionError):
        os.rmdir(root / "LockedDir")
    assert (root / "Root.txt").exists()
    assert (root / "LockedDir").is_dir()
    (root / "Plain.txt").write_bytes(b"delete me")
    os.remove(root / "Plain.txt")
    (root / "PlainDir").mkdir()
    os.rmdir(root / "PlainDir")
    assert not (root / "Plain.txt").exists()
    assert not (root / "PlainDir").exists()
    _unmount(root, proc)
    listing = _xdftool(image, "list")
    assert "Root.txt" in listing and "LockedDir" in listing
    assert "Plain.txt" not in listing and "PlainDir" not in listing
