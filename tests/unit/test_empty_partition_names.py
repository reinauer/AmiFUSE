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
    part = json.loads(capsys.readouterr().out)['partitions'][0]
    assert part['name'] is None
    assert part['driver']['source'] == 'bundled'
    main(['inspect', str(image)])
    out = capsys.readouterr().out
    assert 'Partition: #0 (damaged drive name)' in out
    assert '  #0: FastFileSystem' in out
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
    import sys
    quoted = '"#0"' if sys.platform == 'win32' else "'#0'"
    assert str(caught.value).endswith(f'Open one with --partition {quoted}.')


def test_hint_omits_numeric_name_collision(tmp_path, user_driver_dir):
    image = _failed_disk(tmp_path, ('PI0', '0'))
    _damage_entry(image, tmp_path, 200)
    with pytest.raises(SystemExit) as caught:
        main(['ls', str(image)])
    assert 'read-only: 0. Open one with --partition 0.' in str(caught.value)


def test_partitionless_rdb_has_no_skipped_partition_note(tmp_path):
    image = tmp_path / 'empty.hdf'
    image.write_bytes(_make_rdb_parts(tmp_path, 512, []))
    units, reason = _enumerate_mount_units(str(image))
    assert [(u.name, u.label) for u in units] == [(None, 'disk image')]
    assert reason is None


@pytest.mark.parametrize('wrapped', [False, True])
def test_inspect_resolves_only_numbers_that_select_the_unnamed_partition(
        tmp_path, capsys, user_driver_dir, wrapped):
    data = _make_rdb_parts(tmp_path, 512, [('', DOS3), ('0', DOS3), ('', DOS3)])
    if wrapped:
        image = _make_mbr(tmp_path, [(200, data), (400, data)])
    else:
        image = tmp_path / 'names.hdf'
        image.write_bytes(data)
    main(['inspect', str(image), '--json'])
    result = json.loads(capsys.readouterr().out)
    rdbs = result['rdbs'] if wrapped else [result]
    assert rdbs[0]['partitions'][0]['driver'] is None  # '0' names another part.
    assert rdbs[0]['partitions'][2]['driver']['source'] == 'bundled'
    if wrapped:
        assert rdbs[1]['partitions'][2]['driver'] is None  # '2' selects entry 0.
    main(['inspect', str(image)])
    output = capsys.readouterr().out
    assert '  #2: FastFileSystem' in output
    assert "--partition 0 selects '0' instead" in output


@pytest.mark.parametrize('platform,selector,quoted', [
    ('win32', '#0', '"#0"'),
    ('win32', 'My Disk', '"My Disk"'),
    ('linux', '#0', "'#0'"),
    ('linux', 'My Disk', "'My Disk'"),
])
def test_partition_hint_quotes_for_the_host_shell(monkeypatch, platform, selector, quoted):
    from amifuse import fuse_fs
    monkeypatch.setattr(fuse_fs.sys, 'platform', platform)
    assert fuse_fs._entry_partitions_hint([(selector, selector)]).endswith(
        f'--partition {quoted}.')
