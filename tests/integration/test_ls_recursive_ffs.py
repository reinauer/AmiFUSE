"""`ls --recursive` and `verify` end on real FFS images (#320).

The images have a directory named `q:`, written with xdftool, and a hard link
to an ancestor, made by turning a directory's header into an ST_LINKDIR
block, as xdftool can't write one. Both commands drive the bundled
FastFileSystem without a host FUSE driver. Before the fix neither returned.
"""

import json
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from amifuse import driver_resolver, fuse_fs

AMITOOLS_ROOT = Path(__file__).resolve().parents[2] / "amitools"
BLOCK = 512
ST_USERDIR = 2
ST_LINKDIR = 4

pytestmark = pytest.mark.timeout(60)


@pytest.fixture(autouse=True)
def needs_cpu():
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")


def _xdftool(image, *args):
    proc = subprocess.run(
        [sys.executable, "-m", "amitools.tools.xdftool", str(image), *args],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _header_key(data, name):
    """Find the ST_USERDIR header block of the directory called `name`."""
    for key in range(len(data) // BLOCK):
        block = data[key * BLOCK:(key + 1) * BLOCK]
        if (struct.unpack(">I", block[:4])[0] == 2
                and struct.unpack(">i", block[-4:])[0] == ST_USERDIR
                and block[433:433 + block[432]] == name):
            return key
    raise AssertionError(f"no directory header for {name!r}")


def _put_checksum(block):
    struct.pack_into(">I", block, 20, 0)
    total = sum(struct.unpack(f">{BLOCK // 4}I", block))
    struct.pack_into(">I", block, 20, -total & 0xFFFFFFFF)


def _edit_block(data, key, edit):
    block = bytearray(data[key * BLOCK:(key + 1) * BLOCK])
    edit(block)
    _put_checksum(block)
    data[key * BLOCK:(key + 1) * BLOCK] = block


def _link_to_ancestor(image, link, target):
    """Make directory `link` a hard link to directory `target`."""
    data = bytearray(image.read_bytes())
    link_key = _header_key(data, link)
    target_key = _header_key(data, target)

    def to_link(block):
        struct.pack_into(">i", block, BLOCK - 4, ST_LINKDIR)
        struct.pack_into(">I", block, BLOCK - 11 * 4, target_key)  # real_entry

    def link_back(block):
        struct.pack_into(">I", block, BLOCK - 10 * 4, link_key)  # next_link

    _edit_block(data, link_key, to_link)
    _edit_block(data, target_key, link_back)
    image.write_bytes(data)


@pytest.fixture
def colon_image(tmp_path):
    image = tmp_path / "colon.adf"
    source = tmp_path / "hello.txt"
    source.write_bytes(b"hello\n")
    _xdftool(image, "create", "+", "format", "Test", "ffs",
             "+", "makedir", "q:", "+", "write", str(source), "q:/f.txt",
             "+", "write", str(source), "top.txt")
    return image


@pytest.fixture
def link_image(tmp_path):
    image = tmp_path / "link.adf"
    _xdftool(image, "create", "+", "format", "Test", "ffs",
             "+", "makedir", "a", "+", "makedir", "a/b",
             "+", "makedir", "a/b/loop")
    _link_to_ancestor(image, b"loop", b"a")
    return image


def _args(image, **extra):
    values = dict(image=image, json=True, debug=False, partition=None,
                  block_size=None, driver=driver_resolver.BUNDLED_DRIVER)
    values.update(extra)
    return SimpleNamespace(**values)


def _ls(image, capsys):
    fuse_fs.cmd_ls(_args(image, path="/", recursive=True))
    captured = capsys.readouterr()
    return [ent["path"] for ent in json.loads(captured.out)["entries"]], captured.err


def _verify(image, capsys):
    fuse_fs.cmd_verify(_args(image, file=None, expect_size=None))
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.err


def test_ls_recursive_lists_a_colon_directory_once(colon_image, capsys):
    paths, err = _ls(colon_image, capsys)

    assert sorted(paths) == ["/q:", "/top.txt"]
    assert "contents of /q: not listed" in err


def test_verify_ends_on_a_colon_directory(colon_image, capsys):
    result, err = _verify(colon_image, capsys)

    assert result["total_dirs"] == 1
    assert result["total_files"] == 1
    assert "contents of /q: not listed" in err


def test_ls_recursive_stops_at_a_hard_link_to_an_ancestor(link_image, capsys):
    paths, err = _ls(link_image, capsys)

    assert sorted(paths) == ["/a", "/a/b", "/a/b/loop"]
    assert "/a/b/loop not listed (it is a hard link" in err


def test_verify_stops_at_a_hard_link_to_an_ancestor(link_image, capsys):
    result, err = _verify(link_image, capsys)

    assert result["total_dirs"] == 3
    assert "/a/b/loop not listed (it is a hard link" in err
