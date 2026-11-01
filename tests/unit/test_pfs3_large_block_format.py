"""format refuses pfs3aio before 20.0 on blocks over 1024 bytes.

pfs3aio 19.2 fixes its reserved blocks at 1024 bytes, so with 2048- or
4096-byte partition blocks it divides by zero in ACTION_FORMAT, whatever the
partition's DOS type; 20.0 formats them. format refuses before the handler
starts, by the driver's $VER.
"""

from pathlib import Path

import pytest

from amifuse import fuse_fs
from amifuse.driver_resolver import ResolvedDriver
from amifuse.fuse_fs import get_partition_info
from tests.unit.test_partition_name_before_number import _make_rdb_parts

PFS3 = 0x50465303
PDS3 = 0x50445303
DOS3 = 0x444F5303
MUPF = 0x6D755046
AFS1 = 0x41465301
VER_19 = b"$VER: Professional-File-System-III 19.2 PFS3AIO-VERSION (2.10.2018) written by Michiel"
VER_20 = b"$VER: Professional-File-System-III 20.0 PFS3AIO-VERSION (31.10.2026) written by Michiel"
VER_FFS = b"$VER: fs 47.4 (16.6.2020)"
VER_SFS = b"$VER: SmartFilesystem 1.293 (12.4.2020)"


class BridgeStarted(OSError):
    """Raised by the stub bridge: format got past the refusal."""


def _driver(tmp_path, ver):
    driver = tmp_path / "pfs3aio"
    driver.write_bytes(b"\x00\x00\x03\xf3" + b"\x00" * 64 + ver + b"\x00" * 64)
    return driver


def _format(monkeypatch, tmp_path, driver, dostype, block_size):
    """Run format_volume with a stub partition and bridge; return the exit."""
    started = []

    def bridge(*args, **kwargs):
        started.append(True)
        raise BridgeStarted("bridge started")

    monkeypatch.setattr(fuse_fs, "HandlerBridge", bridge)
    monkeypatch.setattr(fuse_fs, "get_partition_info", lambda *a, **kw: {
        "name": "DH0", "dostype": dostype, "block_size": block_size})
    image = tmp_path / "disk.hdf"
    image.write_bytes(b"\xaa" * 4096)
    with pytest.raises(SystemExit) as caught:
        fuse_fs.format_volume(image, driver, None, "DH0")
    assert image.read_bytes() == b"\xaa" * 4096
    return str(caught.value.code), bool(started)


@pytest.mark.parametrize("block_size", [2048, 4096])
@pytest.mark.parametrize("dostype", [PFS3, PDS3, MUPF, AFS1, DOS3],
                         ids=["PFS3", "PDS3", "muPF", "AFS1", "DOS3"])
def test_pfs3aio_19_is_refused_above_1024(monkeypatch, tmp_path, dostype,
                                          block_size):
    """19.2 formats whatever the partition's DOS type, so each is refused."""
    message, started = _format(monkeypatch, tmp_path,
                               _driver(tmp_path, VER_19), dostype, block_size)
    assert not started
    assert message == (
        f"Error: partition 'DH0' has {block_size}-byte blocks, which pfs3aio "
        "before 20.0 cannot format, and the driver is version 19.2. Use 512- "
        "or 1024-byte blocks, or pfs3aio 20.0 or later.")


@pytest.mark.parametrize("ver,dostype", [
    (b"", PFS3),
    (b"", MUPF),
    (b"$VER: Professional-File-System-III beta (1.1.2020)", DOS3),
], ids=["no-VER-PFS3", "no-VER-muPF", "PFS-no-number-DOS3"])
def test_a_driver_without_version_is_refused(monkeypatch, tmp_path, ver,
                                             dostype):
    message, started = _format(monkeypatch, tmp_path, _driver(tmp_path, ver),
                               dostype, 2048)
    assert not started
    assert "the driver's version could not be determined" in message
    assert "pfs3aio 20.0 or later" in message


def test_an_older_pfs3_name_is_refused(monkeypatch, tmp_path):
    message, started = _format(
        monkeypatch, tmp_path,
        _driver(tmp_path, b"$VER: PFS-III 18.5 (1.1.2000)"), DOS3, 2048)
    assert not started
    assert "the driver is version 18.5" in message


def test_an_unreadable_driver_is_an_error(monkeypatch, tmp_path):
    """The error from reading the driver is reported; the bridge never starts."""
    message, started = _format(monkeypatch, tmp_path, tmp_path / "missing",
                               PFS3, 2048)
    assert not started
    assert message.startswith("Error: ")
    assert "missing" in message


@pytest.mark.parametrize("ver,dostype,block_size", [
    (VER_20, PFS3, 2048),
    (VER_20, PFS3, 4096),
    (VER_20, DOS3, 2048),
    (VER_19, PFS3, 1024),
    (VER_19, PFS3, 512),
    (VER_FFS, DOS3, 4096),
    (VER_SFS, PFS3, 2048),
    (b"", DOS3, 2048),
], ids=["20.0-2048", "20.0-4096", "20.0-DOS3", "19.2-1024", "19.2-512",
        "FFS-4096", "SFS-on-PFS3", "no-VER-DOS3"])
def test_format_goes_on(monkeypatch, tmp_path, ver, dostype, block_size):
    message, started = _format(monkeypatch, tmp_path, _driver(tmp_path, ver),
                               dostype, block_size)
    assert started
    assert message == "Error: bridge started"


def test_rdb_driver_is_checked_and_removed(monkeypatch, tmp_path):
    """The driver extracted from the RDB is read, refused and removed."""
    driver = _driver(tmp_path, VER_19)
    monkeypatch.setattr(fuse_fs, "resolve_driver", lambda *a, **kw: ResolvedDriver(
        driver, "rdb", "PFS3/0x50465303 (from RDB)", temporary=True))
    message, started = _format(monkeypatch, tmp_path, None, PFS3, 2048)
    assert not started
    assert "the driver is version 19.2" in message
    assert not driver.exists()


@pytest.mark.parametrize("ver,number", [
    ("Professional-File-System-III 19.2 PFS3AIO-VERSION (2.10.2018)", (19, "19.2")),
    ("Professional-File-System-III 20.0 PFS3AIO-VERSION (31.10.2026)", (20, "20.0")),
    ("fs 47.4 (16.6.2020)", (47, "47.4")),
    ("handler (1.2.2020)", None),
    (None, None),
])
def test_ver_number(ver, number):
    assert fuse_fs._ver_number(ver) == number


@pytest.mark.parametrize("block_bytes", [512, 2048])
def test_partition_info_has_block_size(tmp_path, block_bytes):
    image = tmp_path / "rdb.hdf"
    image.write_bytes(_make_rdb_parts(tmp_path, block_bytes, [("DH0", PFS3)]))
    assert get_partition_info(image, None, "DH0")["block_size"] == block_bytes
