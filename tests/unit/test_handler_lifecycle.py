"""Failure paths must release image ownership without hiding the cause."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amitools.vamos.disk import HostFileLock
from amifuse.fuse_fs import HandlerBridge
from amifuse.vamos_runner import VamosHandlerRuntime


@pytest.mark.parametrize("stage", ["setup", "load_handler", "bootstrap"])
@pytest.mark.parametrize("failure", [RuntimeError("startup failed"), SystemExit(1)])
def test_constructor_failure_releases_image(tmp_path, monkeypatch, stage, failure):
    image = tmp_path / "disk.adf"
    image.write_bytes(bytes(1024))
    info = SimpleNamespace(block_size=512, cylinders=1, heads=1,
                           sectors_per_track=2, total_blocks=2)
    runtime = Mock()
    runtime.shutdown.side_effect = OSError("shutdown failed")
    bootstrap = Mock()
    if stage == "bootstrap":
        bootstrap.alloc_all.side_effect = failure
    else:
        getattr(runtime, stage).side_effect = failure
    monkeypatch.setattr("amifuse.fuse_fs.VamosHandlerRuntime", lambda: runtime)
    monkeypatch.setattr("amifuse.fuse_fs.BootstrapAllocator", Mock(return_value=bootstrap))

    with pytest.raises(type(failure)) as caught:
        HandlerBridge(image, tmp_path / "handler", adf_info=info)
    assert caught.value is failure
    runtime.shutdown.assert_called_once()
    # Keep the traceback alive: garbage collection must not release the lock
    # on our behalf before this independent open tests ownership.
    lock = HostFileLock(image)
    try:
        lock.acquire()
    finally:
        lock.release()


def test_bridge_close_releases_backend_after_shutdown_failure():
    bridge = HandlerBridge.__new__(HandlerBridge)
    runtime = bridge.vh = Mock()
    backend = bridge.backend = Mock()
    failure = RuntimeError("shutdown failed")
    runtime.shutdown.side_effect = failure
    backend.close.side_effect = OSError("backend close failed")
    with pytest.raises(RuntimeError) as caught:
        bridge.close()
    assert caught.value is failure
    backend.close.assert_called_once()
    assert bridge.vh is None
    assert bridge.backend is None
    bridge.close()
    runtime.shutdown.assert_called_once()
    backend.close.assert_called_once()


@pytest.mark.parametrize("failing_resource", ["disk_session", "slm", "path_mgr"])
def test_runtime_shutdown_attempts_all_resources(failing_resource):
    runtime = VamosHandlerRuntime()
    resources = [("disk_session", "close"), ("slm", "cleanup"),
                 ("path_mgr", "shutdown"), ("mem_map", "cleanup"),
                 ("machine", "cleanup"), ("_temp_dir", "cleanup")]
    mocks = {}
    failure = RuntimeError("cleanup failed")
    for attr, method in resources:
        resource = Mock()
        setattr(runtime, attr, resource)
        mocks[attr] = resource
        if attr == failing_resource:
            getattr(resource, method).side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        runtime.shutdown()
    assert caught.value is failure
    for attr, method in resources:
        getattr(mocks[attr], method).assert_called_once()
        if attr != "disk_session":
            assert getattr(runtime, attr) is None
    runtime.shutdown()
    for attr, method in resources:
        getattr(mocks[attr], method).assert_called_once()
