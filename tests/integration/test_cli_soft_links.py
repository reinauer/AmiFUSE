"""The file commands name a soft link as one, with its target (#436).

`ls` listed a soft link as a directory, and read, hash and verify, which
can't examine it, reported it as not found. The image is the bundled
FastFileSystem floppy of test_soft_links, with a/target.txt and a/sl, a
soft link to target.txt.
"""

import json
import subprocess
import sys

import pytest

from amifuse.driver_resolver import BUNDLED_DRIVER
from tests.integration.test_soft_links import _soft_link_image

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def image(tmp_path):
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")
    return _soft_link_image(tmp_path)


def _amifuse(*args):
    return subprocess.run(
        [sys.executable, "-m", "amifuse", *args, "--driver", str(BUNDLED_DRIVER)],
        capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("recursive", [[], ["--recursive"]], ids=["ls", "ls-R"])
def test_ls_lists_a_soft_link_with_its_target(image, recursive):
    path = [] if recursive else ["--path", "a"]
    result = _amifuse("ls", str(image), "--json", *path, *recursive)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["target"] == str(image)
    entries = {e["path"]: e for e in data["entries"]}
    assert entries["/a/sl"]["type"] == "link"
    assert entries["/a/sl"]["link_target"] == "target.txt"
    assert entries["/a/target.txt"]["type"] == "file"
    assert "link_target" not in entries["/a/target.txt"]
    # A soft link is not a directory the walk leaves alone.
    assert "not listed" not in result.stderr


def test_ls_text_shows_the_target(image):
    result = _amifuse("ls", str(image), "--path", "a")
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(line for line in result.stdout.splitlines() if " sl " in line)
    assert "<link>" in line and line.endswith("-> target.txt")


@pytest.mark.parametrize("command, extra", [
    ("read", ["--out", "-"]),
    ("hash", []),
])
def test_read_and_hash_say_the_path_is_a_soft_link(image, command, extra):
    result = _amifuse(command, str(image), "--file", "a/sl", *extra)
    assert result.returncode == 1
    assert "/a/sl is a soft link to target.txt" in result.stderr
    assert "not found" not in result.stderr


@pytest.mark.parametrize("command", ["read", "hash"])
def test_read_and_hash_json_error(image, tmp_path, command):
    extra = ["--out", str(tmp_path / "out")] if command == "read" else []
    result = _amifuse(command, str(image), "--file", "a/sl", "--json", *extra)
    assert result.returncode == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "IS_SOFT_LINK"
    assert error["details"]["link_target"] == "target.txt"


def test_verify_reports_a_soft_link(image):
    result = _amifuse("verify", str(image), "--file", "a/sl", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert (data["exists"], data["type"], data["link_target"]) == (True, "link", "target.txt")
    # "target" stays the image, as in every command's JSON.
    assert data["target"] == str(image)


def test_verify_text_names_the_link_target(image):
    result = _amifuse("verify", str(image), "--file", "a/sl")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "  Link target: target.txt" in result.stdout.splitlines()


def test_verify_text_of_a_plain_file_has_no_link_target(image):
    result = _amifuse("verify", str(image), "--file", "a/target.txt")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Type: file" in result.stdout
    assert "target" not in result.stdout.lower().replace("a/target.txt", "")


def test_volume_verify_counts_soft_links_on_their_own(image):
    result = _amifuse("verify", str(image), "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    # a, a/target.txt and the soft link a/sl.
    assert (data["total_dirs"], data["total_files"], data["total_links"]) == (1, 1, 1)
    assert data["total_size_bytes"] == 7


def test_a_missing_file_is_still_not_found(image):
    result = _amifuse("read", str(image), "--file", "a/missing", "--out", "-")
    assert result.returncode == 1
    assert "file not found" in result.stderr
