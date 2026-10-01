"""Exercise the missing-module path used by the declared dependency floor."""

import builtins
import runpy
from pathlib import Path

import pytest


def load_without_win32disk(monkeypatch, missing="amitools.util.Win32Disk"):
    original = builtins.__import__

    def import_module(name, *args, **kwargs):
        if name == "amitools.util.Win32Disk":
            raise ModuleNotFoundError("missing dependency", name=missing)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)
    return runpy.run_path(str(Path(__file__).parents[2] / "amifuse/image_access.py"))


def test_old_amitools_keeps_file_probes_working(monkeypatch, tmp_path):
    helpers = load_without_win32disk(monkeypatch)
    image = tmp_path / "image.hdf"
    image.write_bytes(b"RDSK" + bytes(508))
    assert helpers["image_size"](image) == 512
    with helpers["open_image"](image) as stream:
        assert stream.read(4) == b"RDSK"
    assert not helpers["is_windows_disk"](r"\\.\PhysicalDrive2")


def test_broken_dependency_is_not_silently_hidden(monkeypatch):
    with pytest.raises(ModuleNotFoundError):
        load_without_win32disk(monkeypatch, missing="another_dependency")
