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


@pytest.mark.parametrize("target, link, expected", [
    # The drive name of the partition, in any case, is the mount's root
    # too, as AmigaDOS resolves DH0: to the volume in it (#447).
    ("DH0:x", "/a/sl", "../x"),
    ("dh0:x", "/a/sl", "../x"),
    ("Links:x", "/sl", "x"),
    (":x", "/sl", "x"),
    # Another drive is not.
    ("DH1:x", "/sl", "DH1:x"),
])
def test_target_by_drive_name(target, link, expected):
    assert _posix_link_target(target, link, "Links", "DH0") == expected


def test_no_drive_name_on_a_floppy():
    # Nothing records which drive a floppy was in, so DF0: stays as it is.
    assert _posix_link_target("DF0:x", "/sl", "Links", None) == "DF0:x"


def test_drive_name_folds_latin1():
    assert _posix_link_target("\xc4H0:x", "/sl", "Links", "\xe4h0") == "x"


def test_volume_name_folds_latin1():
    assert _posix_link_target("\xc9t\xc9:x", "/sl", "\xe9t\xe9") == "x"
