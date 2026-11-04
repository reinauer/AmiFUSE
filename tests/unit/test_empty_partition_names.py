"""Unnamed partitions have labels, and only unambiguous selectors are offered."""
import json

import pytest
from amitools.fs.DosType import DOS3

from amifuse.fuse_fs import get_partition_info, main
from amifuse.launcher import _enumerate_mount_units
from amifuse.rdb_inspect import find_partition, list_partitions
from tests.unit.test_entry_partitions_hint import _failed_disk, _damage_entry
from tests.unit.test_mbr_rdb_block_size import _make_mbr
from tests.unit.test_partition_name_before_number import _make_rdb_parts


def test_empty_name_is_unnamed_in_listing_and_reports(tmp_path, capsys, user_driver_dir):
    image = tmp_path / 'empty.hdf'
    image.write_bytes(_make_rdb_parts(tmp_path, 512, [('', DOS3), ('None', DOS3)]))
    assert [p.name for p in list_partitions(image).partitions] == [None, 'None']
    assert find_partition(image, None, '') is None
    assert get_partition_info(image, None, '0')['name'] == '#0'
    main(['inspect', str(image), '--json'])
    assert json.loads(capsys.readouterr().out)['partitions'][0]['name'] is None
    main(['inspect', str(image)])
    out = capsys.readouterr().out
    assert 'Partition: #0 (damaged drive name)' in out
    assert '  #0: damaged drive name' in out
    units, reason = _enumerate_mount_units(str(image))
    assert [(u.name, u.label) for u in units] == [('0', '#0'), ('None', 'None')]
    assert reason is None


@pytest.mark.parametrize('second,expected', [
    ('', [(None, 'disk image'), ('1', '#1')]),
    ('0', [('0', '0')]),
])
def test_launcher_checks_numeric_collisions(tmp_path, second, expected):
    image = tmp_path / 'names.hdf'
    image.write_bytes(_make_rdb_parts(tmp_path, 512, [('', DOS3), (second, DOS3)]))
    units, reason = _enumerate_mount_units(str(image))
    assert [(u.name, u.label) for u in units] == expected
    assert bool(reason) == (second == '0')


def test_launcher_does_not_select_a_later_rdb_by_an_earlier_number(tmp_path):
    image = _make_mbr(tmp_path, [
        (200, _make_rdb_parts(tmp_path, 512, [('', DOS3)])),
        (400, _make_rdb_parts(tmp_path, 512, [('', DOS3), ('Data', DOS3)])),
    ])
    units, reason = _enumerate_mount_units(str(image))
    assert [(u.name, u.label) for u in units] == [('0', '#0'), ('Data', 'Data')]
    assert reason and 'not mounted' in reason


def test_hint_keeps_numeric_label_out_of_selector(tmp_path, capsys, user_driver_dir):
    image = _failed_disk(tmp_path, ('PI0', 'PI1'))
    _damage_entry(image, tmp_path, 200)
    with pytest.raises(SystemExit) as caught:
        main(['ls', str(image)])
    assert '#0, PI1. Open one with --partition 0.' in str(caught.value)
    with pytest.raises(SystemExit):
        main(['ls', str(image), '--json'])
    assert json.loads(capsys.readouterr().out)['error']['details']['partitions'] == ['#0', 'PI1']


def test_hint_preserves_a_real_hash_prefixed_name(tmp_path, user_driver_dir):
    image = _failed_disk(tmp_path, ('#0',))
    with pytest.raises(SystemExit) as caught:
        main(['ls', str(image)])
    assert str(caught.value).endswith('Open one with --partition #0.')


def test_hint_omits_numeric_name_collision(tmp_path, user_driver_dir):
    image = _failed_disk(tmp_path, ('PI0', '0'))
    _damage_entry(image, tmp_path, 200)
    with pytest.raises(SystemExit) as caught:
        main(['ls', str(image)])
    assert 'read-only: 0. Open one with --partition 0.' in str(caught.value)
