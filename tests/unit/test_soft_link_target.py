"""How a soft link's AmigaDOS target is reported to FUSE (#434)."""

import pytest

from amifuse.fuse_fs import _posix_link_target


@pytest.mark.parametrize("target, link, expected", [
    # Relative to the link's directory.
    ("target.txt", "/a/sl", "target.txt"),
    ("b/c", "/a/sl", "b/c"),
    ("b/", "/a/sl", "b"),
    # A leading slash, or one after another, goes up to the parent.
    ("/x", "/a/sl", "../x"),
    ("//x", "/a/b/sl", "../../x"),
    ("b//x", "/a/sl", "b/../x"),
    ("/", "/a/sl", ".."),
    ("b//", "/a/sl", "b/.."),
    # This volume, named in any case or not at all, is the mount's root.
    ("Links:a/target.txt", "/a/sl", "../a/target.txt"),
    ("LINKS:x", "/a/b/sl", "../../x"),
    (":x", "/sl", "x"),
    ("Links:", "/sl", "."),
    ("Links:", "/a/sl", ".."),
    # Another volume or an assign is not on the mount.
    ("Work:x", "/a/sl", "Work:x"),
    ("SYS:C/Dir", "/sl", "SYS:C/Dir"),
])
def test_target(target, link, expected):
    assert _posix_link_target(target, link, "Links") == expected


def test_volume_name_folds_latin1():
    assert _posix_link_target("\xc9t\xc9:x", "/sl", "\xe9t\xe9") == "x"
