"""Deferred file growth preserves bytes and avoids the copy's initial fill."""
import errno
import os

import pytest

from amifuse.fuse_fs import AmigaFuseFS, FuseOSError
from tests.fixtures.images import make_adf
from tests.fixtures.paths import BUNDLED_FFS
from tests.integration.test_handler_bridge_write import _bridge
from tests.integration.test_truncate_follow_ups import _volume

pytestmark = pytest.mark.integration
VOLUMES = [
    pytest.param('ffs', id='ffs', marks=pytest.mark.no_fixtures),
    pytest.param('ofs', id='ofs', marks=pytest.mark.no_fixtures),
    pytest.param('pfs3', id='pfs3'),
    pytest.param('sfs', id='sfs'),
]


@pytest.fixture(params=VOLUMES)
def volume(request, tmp_path):
    kind = request.param
    if kind in ('ffs', 'ofs'):
        image = make_adf(tmp_path / 'disk.adf', dos_type=0x444f5301 if kind == 'ffs' else 0x444f5300)
        driver = BUNDLED_FFS
    else:
        name = 'pfs3aio' if kind == 'pfs3' else 'SmartFilesystem'
        image, driver = _volume(request, tmp_path, name, 'pfs.hdf' if kind == 'pfs3' else 'sfs.hdf')
    bridge = _bridge(image, driver, read_only=False)
    ops = AmigaFuseFS(bridge, icons=False)
    try:
        yield ops, bridge, image, driver
    finally:
        bridge.close()


def test_copy_does_not_write_the_grown_range_twice(volume, monkeypatch):
    ops, bridge, image, driver = volume
    writes = []
    send = bridge.launcher.send_write_handle
    def record(state, fh, address, size):
        writes.append(size)
        return send(state, fh, address, size)
    monkeypatch.setattr(bridge.launcher, 'send_write_handle', record)
    h = ops.create('/copy', 0o644)
    data = bytes(range(256)) * 1024
    ops.truncate('/copy', len(data), h)
    assert sum(writes) == 0
    assert ops.getattr('/copy', h)['st_size'] == len(data)
    for offset in range(0, len(data), 65536):
        assert ops.write('/copy', data[offset:offset + 65536], offset, h) == 65536
    ops.flush('/copy', h)
    ops.release('/copy', h)
    assert sum(writes) == len(data)
    ops.fsync('/copy', False, None)
    bridge.close()
    reopened = _bridge(image, driver)
    try:
        assert reopened.read_file('/copy', len(data), 0) == data
    finally:
        reopened.close()


@pytest.mark.parametrize('finish', ['read', 'flush', 'fsync', 'release', 'destroy'])
def test_unwritten_tail_is_zero_and_persisted(volume, finish):
    ops, bridge, image, driver = volume
    h = ops.create('/tail', 0o644)
    ops.write('/tail', b'head', 0, h)
    ops.truncate('/TAIL', 4096, h)
    assert ops.getattr('/tail')['st_size'] == 4096
    ops.write('/tail', b'end', 1024, h)
    expected = b'head' + bytes(1020) + b'end' + bytes(4096 - 1027)
    if finish == 'read':
        assert ops.read('/tail', 4096, 0, h) == expected
    elif finish == 'flush':
        ops.flush('/tail', h)
    elif finish == 'fsync':
        ops.fsync('/tail', False, h)
    elif finish == 'destroy':
        ops.destroy('/')
    if finish != 'destroy':
        ops.release('/tail', h)
        assert bridge.read_file('/tail', 8192, 0) == expected
        ops.fsync('/tail', False, None)
        bridge.close()
    reopened = _bridge(image, driver)
    try:
        assert reopened.read_file('/tail', 8192, 0) == expected
    finally:
        reopened.close()


def test_pending_length_follows_handles_and_rename(volume):
    ops, bridge, image, driver = volume
    h = ops.create('/original', 0o644)
    ops.write('/original', b'abc', 0, h)
    ops.release('/original', h)
    h = ops.open('/original', os.O_RDWR)
    ops.truncate('/original', 1000, h)
    other = ops.open('/ORIGINAL', os.O_RDONLY)
    assert ops.getattr('/ORIGINAL', other)['st_size'] == 1000
    if driver == BUNDLED_FFS:
        # FFS/OFS refuse renaming an open file. Its pending size survives.
        with pytest.raises(FuseOSError) as exc:
            ops.rename('/original', '/renamed')
        assert exc.value.errno == errno.EBUSY
        assert ops.getattr('/original')['st_size'] == 1000
    else:
        ops.rename('/original', '/renamed')
        assert ops.getattr('/renamed')['st_size'] == 1000
    ops.release('/original', h)
    assert ops.read('/original', 1000, 0, other) == b'abc' + bytes(997)
    ops.release('/original', other)


def test_pending_grow_can_shrink_before_filling(volume):
    ops, bridge, image, driver = volume
    h = ops.create('/short', 0o644)
    ops.write('/short', b'abc', 0, h)
    ops.truncate('/short', 8192, h)
    ops.truncate('/short', 100, h)
    assert ops.getattr('/short', h)['st_size'] == 100
    ops.truncate('/short', 2, h)
    ops.release('/short', h)
    assert bridge.read_file('/short', 9000, 0) == b'ab'


def test_oversized_grow_fails_before_changing_the_file(volume):
    ops, bridge, image, driver = volume
    h = ops.create('/full', 0o644)
    with pytest.raises(FuseOSError) as exc:
        ops.truncate('/full', 0x7fffffff, h)
    assert exc.value.errno == errno.ENOSPC
    assert ops.getattr('/full', h)['st_size'] == 0
    ops.release('/full', h)


@pytest.mark.parametrize('finish', ['flush', 'fsync', 'release'])
def test_failed_tail_fill_keeps_the_length_for_retry(volume, monkeypatch, finish):
    ops, bridge, image, driver = volume
    h = ops.create('/retry', 0o644)
    ops.write('/retry', b'head', 0, h)
    ops.truncate('/retry', 4096, h)
    truncate = bridge.truncate_handle
    monkeypatch.setattr(bridge, 'truncate_handle', lambda *a, **kw: (-1, 221))
    def complete():
        if finish == 'flush':
            ops.flush('/retry', h)
        elif finish == 'fsync':
            ops.fsync('/retry', False, h)
        else:
            ops.release('/retry', h)
    with pytest.raises(FuseOSError) as exc:
        complete()
    assert exc.value.errno == errno.ENOSPC
    assert ops.getattr('/retry', h)['st_size'] == 4096
    assert not ops._fh_cache[h]['closed']
    monkeypatch.setattr(bridge, 'truncate_handle', truncate)
    complete()
    if finish != 'release':
        ops.release('/retry', h)
    assert bridge.read_file('/retry', 4096, 0) == b'head' + bytes(4092)


def test_second_file_does_not_overcommit_the_first_grow(volume, monkeypatch):
    ops, bridge, image, driver = volume
    first = ops.create('/first', 0o644)
    second = ops.create('/second', 0o644)
    ops.truncate('/first', 4096, first)
    assert '/first' in ops._deferred_grows
    ops.truncate('/second', 8192, second)
    # Allocation for a different file commits the earlier promise first.
    assert '/first' not in ops._deferred_grows
    assert bridge._handle_end(ops._fh_cache[first]['fh_addr']) == 4096
    ops.release('/first', first)
    ops.release('/second', second)


def test_refused_write_probe_does_not_publish_a_length(volume, monkeypatch):
    ops, bridge, image, driver = volume
    h = ops.create('/protected', 0o644)
    monkeypatch.setattr(bridge, 'write_empty_reply', lambda fh: (-1, 223))
    with pytest.raises(FuseOSError) as exc:
        ops.truncate('/protected', 4096, h)
    assert exc.value.errno == errno.EACCES
    assert ops.getattr('/protected', h)['st_size'] == 0
    assert not ops._deferred_grows
    ops.release('/protected', h)


@pytest.mark.no_fixtures
@pytest.mark.parametrize('dos_type', [0x444f5300, 0x444f5301])
def test_protected_file_refuses_deferred_growth(tmp_path, dos_type):
    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.FSString import FSString
    from amitools.fs.blkdev.ADFBlockDevice import ADFBlockDevice
    image = make_adf(tmp_path / 'protected.adf', dos_type=dos_type,
                     files={'locked': b'original'})
    raw = ADFBlockDevice(str(image))
    raw.open()
    disk = ADFSVolume(raw)
    try:
        disk.open()
        disk.get_path_name(FSString('locked')).change_protect(4)
    finally:
        disk.close()
        raw.close()
    bridge = _bridge(image, BUNDLED_FFS, read_only=False)
    try:
        ops = AmigaFuseFS(bridge, icons=False)
        h = ops.open('/locked', os.O_RDWR)
        with pytest.raises(FuseOSError) as exc:
            ops.truncate('/locked', 4096, h)
        assert exc.value.errno == errno.EACCES
        assert ops.getattr('/locked', h)['st_size'] == 8
        ops.release('/locked', h)
    finally:
        bridge.close()


def test_two_large_grows_cannot_claim_the_same_free_blocks(volume):
    ops, bridge, image, driver = volume
    first = ops.create('/reserve-a', 0o644)
    second = ops.create('/reserve-b', 0o644)
    info = bridge.get_disk_info()
    free = (info['num_blocks'] - info['num_blocks_used']) * info['bytes_per_block']
    length = free * 3 // 5
    ops.truncate('/reserve-a', length, first)
    with pytest.raises(FuseOSError) as exc:
        ops.truncate('/reserve-b', length, second)
    assert exc.value.errno == errno.ENOSPC
    assert ops.getattr('/reserve-a', first)['st_size'] == length
    assert ops.getattr('/reserve-b', second)['st_size'] == 0
    ops.release('/reserve-a', first)
    ops.release('/reserve-b', second)
