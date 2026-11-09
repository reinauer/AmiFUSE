"""A soft link on a mounted volume is a symbolic link to its target (#434).

On Windows, WinFsp makes it a symbolic link reparse point, which the system
follows to the target.
"""

import os
import stat
from pathlib import Path

import pytest

from amifuse.driver_resolver import BUNDLED_DRIVER
from tests.integration.test_soft_links import _soft_link_image

pytestmark = pytest.mark.fuse


def _outcome(func, *args):
    try:
        return func(*args)
    except OSError as exc:
        return repr(exc)


def _seen(link):
    """What the system reports for link, all of it, for a failure to show."""
    st = os.lstat(link)
    return {
        "is_link": stat.S_ISLNK(st.st_mode),
        "mode": oct(st.st_mode),
        "attributes": hex(getattr(st, "st_file_attributes", 0)),
        "reparse_tag": hex(getattr(st, "st_reparse_tag", 0)),
        "readlink": _outcome(os.readlink, link),
        "content": _outcome(Path.read_bytes, link),
    }


@pytest.mark.parametrize("extra_args", [[], ["--write"]], ids=["read-only", "write"])
def test_soft_link_leads_to_its_target(mount_image, tmp_path, extra_args):
    image = _soft_link_image(tmp_path)
    _proc, root = mount_image(image, driver=BUNDLED_DRIVER, extra_args=extra_args)
    link = Path(root) / "a" / "sl"
    assert sorted(os.listdir(link.parent)) == ["sl", "target.txt"]
    seen = _seen(link)
    assert (seen["is_link"], seen["readlink"], seen["content"]) == (
        True, "target.txt", b"target\n"), str(seen)
