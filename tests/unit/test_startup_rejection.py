"""Distinguish an explicit startup refusal from a silent handler."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from amifuse import fuse_fs as fs


@pytest.mark.parametrize("res2", [0, 225, 999])
@pytest.mark.parametrize("replied,crashed", [(True, False), (False, False),
                                             (False, True)])
def test_startup_refusal_requires_a_reply(tmp_path, monkeypatch, res2, replied,
                                          crashed):
    backend, runtime = Mock(), MagicMock()
    bootstrap = Mock()
    bootstrap.alloc_all.return_value = {"part": None}
    state = SimpleNamespace(process_addr=1, stdpkt_addr=2, crashed=crashed,
                            main_loop_pc=3)
    launcher = Mock()
    launcher.launch_with_startup.return_value = state
    packet = SimpleNamespace(res1=SimpleNamespace(val=0), res2=SimpleNamespace(val=res2))
    monkeypatch.setattr(fs, "BlockDeviceBackend", Mock(return_value=backend))
    monkeypatch.setattr(fs, "VamosHandlerRuntime", lambda: runtime)
    monkeypatch.setattr(fs, "BootstrapAllocator", Mock(return_value=bootstrap))
    monkeypatch.setattr(fs, "HandlerLauncher", Mock(return_value=launcher))
    monkeypatch.setattr(fs, "ProcessManager", Mock())
    monkeypatch.setattr(fs, "DosPacketStruct", Mock(return_value=packet))
    monkeypatch.setattr(fs.HandlerBridge, "_run_startup_until_replies",
                        Mock(return_value=[(1, 2, 0, res2)] if replied else []))
    monkeypatch.setattr(fs.HandlerBridge, "_flush_pending_signals", Mock())

    with pytest.raises(SystemExit) as caught:
        fs.HandlerBridge(tmp_path / "image", tmp_path / "driver")
    assert isinstance(caught.value, fs.StartupDiskRejected) is replied
    if replied:
        assert caught.value.res1 == 0
        assert caught.value.res2 == res2
        if res2 == 0:
            assert "wrong --driver" in str(caught.value)
    elif crashed:
        assert str(caught.value) == "Filesystem handler crashed during startup."
    else:
        assert "did not reply" in str(caught.value)
    backend.close.assert_called_once()
    runtime.shutdown.assert_called_once()


@pytest.mark.parametrize("use_json", [True, False])
@pytest.mark.parametrize("refused", [True, False])
@pytest.mark.parametrize("res2", [0, 225, 999])
def test_factory_classifies_refusal_and_cleans_driver(tmp_path, monkeypatch, capsys,
                                                     use_json, refused, res2):
    image, driver = tmp_path / "image", tmp_path / "driver"
    image.write_bytes(b"image")
    driver.write_bytes(b"driver")
    monkeypatch.setattr("amifuse.rdb_inspect.detect_adf", lambda image: None)
    monkeypatch.setattr("amifuse.rdb_inspect.detect_iso", lambda image: None)
    monkeypatch.setattr(fs, "extract_embedded_driver", lambda *a: (driver, "DOS3", 0))
    failure = fs.StartupDiskRejected(0, res2) if refused else SystemExit("startup failed")
    monkeypatch.setattr(fs, "HandlerBridge", Mock(side_effect=failure))
    args = SimpleNamespace(image=image, json=use_json)
    with pytest.raises(SystemExit) as caught:
        fs._create_bridge_from_args(args, "ls")
    captured = capsys.readouterr()
    if use_json:
        assert caught.value.code == 1
        error = json.loads(captured.out)["error"]
        assert error["code"] == ("NOT_MOUNTED" if refused else "HANDLER_ERROR")
        if refused:
            assert error["details"] == {"dos_error": res2}
            assert error["message"].startswith("No usable volume mounted:")
    else:
        assert caught.value is failure
        if refused:
            assert str(caught.value).startswith("Error: no usable volume mounted:")
    assert not driver.exists()

