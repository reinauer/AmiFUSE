"""format with the real pfs3aio refuses a volume name over 31 characters.

pfs3aio answers success for a longer name, but its root block holds only
31 characters, and with 40 the handler then crashes at every open.
"""

import subprocess
import sys

import pytest

from tests.integration.test_cli_commands import _parse_json_stdout
from tests.integration.test_large_block_rdb import make_rdb

pytestmark = pytest.mark.integration

NAME_40 = "ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJKLMN"


def _amifuse(*args):
    return subprocess.run([sys.executable, "-m", "amifuse", *map(str, args)],
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("length", [32, 40])
def test_a_long_name_is_refused_and_the_image_is_untouched(
        pfs3_driver, tmp_path, length):
    image = tmp_path / "pfs3.hdf"
    make_rdb(image, 512, 0x50465303)
    before = image.read_bytes()

    result = _amifuse("format", image, "DH0", NAME_40[:length],
                      "--driver", pfs3_driver)

    assert result.returncode == 1, result.stdout + result.stderr
    assert (f"Error: volume name is {length} characters long; pfs3aio "
            "stores at most 31. Use a shorter name.") in result.stderr
    assert "Format complete" not in result.stdout
    assert image.read_bytes() == before


def test_31_characters_format_and_read_back(pfs3_driver, tmp_path):
    image = tmp_path / "pfs3.hdf"
    make_rdb(image, 512, 0x50465303)
    name = NAME_40[:31]

    result = _amifuse("format", image, "DH0", name, "--driver", pfs3_driver)
    assert result.returncode == 0, result.stdout + result.stderr

    result = _amifuse("verify", "--json", "--driver", pfs3_driver, image)
    assert result.returncode == 0, result.stdout + result.stderr
    data = _parse_json_stdout(result)
    assert data["volume"] == name
    assert data["filesystem_responsive"] is True
