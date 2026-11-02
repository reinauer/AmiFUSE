"""rdb-inspect --json gives a damaged drive name as null, as inspect --json
does, so a script can tell it from a partition named None."""

import json

from amitools.fs.DosType import DOS3, DOS7

from amifuse import rdb_inspect
from tests.unit.test_mbr_rdb_block_size import _make_mbr
from tests.unit.test_partition_name_before_number import (_damage_first_name,
                                                          _make_rdb_parts)


def _damaged_rdb(tmp_path, first="DH0", second="DH1"):
    return _damage_first_name(tmp_path, _make_rdb_parts(
        tmp_path, 512, [(first, DOS3), (second, DOS7)]))


def _rdb_inspect_json(image, capsys):
    rdb_inspect.main(["--json", str(image)])
    return json.loads(capsys.readouterr().out)


def _names(desc):
    return [(part["num"], part["name"]) for part in desc["partitions"]]


def test_damaged_name_is_null_beside_a_partition_named_none(tmp_path, capsys):
    image = tmp_path / "disk.hdf"
    image.write_bytes(_damaged_rdb(tmp_path, second="None"))
    assert _names(_rdb_inspect_json(image, capsys)) == [(0, None), (1, "None")]


def test_damaged_name_is_null_in_every_rdb(tmp_path, capsys):
    image = _make_mbr(tmp_path, [(64, _damaged_rdb(tmp_path)),
                                 (448, _damaged_rdb(tmp_path, "DH2", "DH3"))])
    first, second = _rdb_inspect_json(image, capsys)
    assert _names(first) == [(0, None), (1, "DH1")]
    assert _names(second) == [(0, None), (1, "DH3")]
