"""Case-only renames must reach the case-insensitive Amiga handler."""

import os
import shutil
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.fuse,
              pytest.mark.skipif(sys.platform != "win32", reason="WinFsp")]


@pytest.mark.parametrize("directory", [False, True])
def test_case_only_rename(mount_image, pfs3_image, pfs3_driver, directory, tmp_path):
    image = tmp_path / "disk.hdf"
    shutil.copy2(pfs3_image, image)
    _proc, root = mount_image(image, driver=pfs3_driver,
                              extra_args=["--write"])
    root = Path(root)
    old, new = root / "CaseProbe", root / "CASEPROBE"
    if directory:
        old.mkdir()
        (old / "Child").write_bytes(b"keep child")
    else:
        old.write_bytes(b"keep content")
    os.rename(old, new)
    assert "CASEPROBE" in os.listdir(root)
    assert "CaseProbe" not in os.listdir(root)
    if directory:
        assert (root / "caseprobe" / "child").read_bytes() == b"keep child"
        os.replace(new, old)
        assert "CaseProbe" in os.listdir(root)
    else:
        assert (root / "caseprobe").read_bytes() == b"keep content"
    # Declaring case-insensitivity must not allow two distinct objects to
    # overwrite each other when the caller has not requested replacement.
    other = root / "Other"
    if directory:
        other.mkdir()
    else:
        other.write_bytes(b"other content")
    with pytest.raises(FileExistsError):
        os.rename(other, old if directory else new)
    assert "Other" in os.listdir(root)
