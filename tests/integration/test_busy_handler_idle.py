"""A handler whose packet wait ran out finishes the packet on an idle mount.

pfs3aio frees the blocks of a deleted file in one long stretch of work. With
a small busy limit the delete's wait runs out part way, and the mount's
timer thread must then run the handler on, with no further FUSE operation,
until it parks, leaving its late reply for the next packet's wait (#145).
The rest of this delete fits in the thread's first slice; the slices and the
budget across several calls are covered by the unit tests.
"""

import os
import shutil
import time

import pytest

pytestmark = pytest.mark.integration

DATA = bytes(range(256)) * 8192  # 2 MiB: about half of pfs.hdf's free space
CHUNK = 64 * 1024  # one write packet, as a FUSE write sends it


def test_idle_mount_finishes_a_delete_whose_wait_ran_out(fixture_root, tmp_path, monkeypatch):
    from amifuse.fuse_fs import AmigaFuseFS, HandlerBridge

    driver = fixture_root / "drivers" / "pfs3aio"
    source = fixture_root / "fixtures" / "readonly" / "pfs.hdf"
    if not driver.exists() or not source.exists():
        pytest.skip("pfs3aio handler or pfs.hdf not found")
    image = tmp_path / "pfs.hdf"
    shutil.copy2(source, image)

    bridge = HandlerBridge(image, driver, read_only=False)
    mount = AmigaFuseFS(bridge)
    try:
        fh, lock = bridge.open_file("/big.bin", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            for offset in range(0, len(DATA), CHUNK):
                chunk = DATA[offset:offset + CHUNK]
                assert bridge.write_handle(fh, chunk) == len(chunk)
        finally:
            bridge.close_file(fh)
            if lock:
                bridge.free_lock(lock)
        root, _ = bridge.locate(0, "")
        run_until_replies = HandlerBridge._run_until_replies

        def one_short_burst(self, **wait):
            # The delete gets a single burst of 10,000 cycles.
            return run_until_replies(self, **{**wait, "cycles": 10_000})

        with monkeypatch.context() as m:
            m.setattr(HandlerBridge, "_MAX_BUSY_CYCLES", 1)
            m.setattr(HandlerBridge, "_run_until_replies", one_short_burst)
            assert bridge.delete_object(root, "big.bin") == (0, -1)  # no reply
        assert bridge._busy_ran_out

        mount.init("/")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with bridge._lock:
                if not bridge._busy_ran_out:
                    break
            time.sleep(0.05)
        with bridge._lock:
            assert not bridge._busy_ran_out
            assert not bridge.state.crashed
            # The delete's late reply waits for the next packet's wait.
            pmgr = bridge.vh.slm.exec_impl.port_mgr
            assert pmgr.has_msg(bridge.state.reply_port_addr)
        assert bridge.stat_path("/big.bin") is None
    finally:
        mount.destroy("/")

    check = HandlerBridge(image, driver, read_only=True)
    try:
        assert check.stat_path("/big.bin") is None
    finally:
        check.close()
