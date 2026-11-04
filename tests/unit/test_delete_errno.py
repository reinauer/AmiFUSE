"""Handler delete refusals retain their meaning at the FUSE boundary."""

import errno

import pytest

from amifuse.fuse_fs import AmigaFuseFS, FuseOSError
from tests.unit.test_rmdir_object_in_use import _InUseVolume


@pytest.mark.parametrize("operation,path", [("unlink", "/d/x"), ("rmdir", "/d")])
@pytest.mark.parametrize("code,expected", [
    (205, errno.ENOENT),
    (214, errno.EROFS),
    (221, errno.ENOSPC),
    (222, errno.EACCES),
    (223, errno.EACCES),
    (999, errno.EIO),
])
def test_delete_errno(operation, path, code, expected):
    volume = _InUseVolume({"/", "/d"}, {"/d/x"})
    volume.delete_object = lambda lock, name: (0, code)
    volume.delete_dir_atomic = lambda path: (0, code)
    fs = AmigaFuseFS(volume, icons=False)

    with pytest.raises(FuseOSError) as caught:
        getattr(fs, operation)(path)

    assert caught.value.errno == expected
    assert not fs._pending_deletes
    assert not fs._pending_dir_deletes
