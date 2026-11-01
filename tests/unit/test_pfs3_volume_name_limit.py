"""format refuses a volume name over 31 characters for pfs3aio.

PFS3's root block holds 31 characters of volume name. pfs3aio answers
success for a longer one but stores the requested length with only 31
characters, and the volume then reads wrong or crashes the handler. format
refuses first, keyed by the driver's $VER, else by the partition's DOS type.
"""

import pytest

from amifuse import fuse_fs

PFS3 = 0x50465303
DOS3 = 0x444F5303
MUPF = 0x6D755046
VER_19 = b"$VER: Professional-File-System-III 19.2 PFS3AIO-VERSION (2.10.2018) written by Michiel"
VER_20 = b"$VER: Professional-File-System-III 20.0 PFS3AIO-VERSION (31.10.2026) written by Michiel"
VER_SFS = b"$VER: SmartFilesystem 1.293 (12.4.2020)"
NAME_40 = "ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJKLMN"


class BridgeStarted(OSError):
    """Raised by the stub bridge: format got past the refusal."""


def _driver(tmp_path, ver):
    driver = tmp_path / "pfs3aio"
    driver.write_bytes(b"\x00\x00\x03\xf3" + b"\x00" * 64 + ver + b"\x00" * 64)
    return driver


def _format(monkeypatch, tmp_path, driver, dostype, volname):
    """Run format_volume with a stub partition and bridge; return the exit."""
    started = []

    def bridge(*args, **kwargs):
        started.append(True)
        raise BridgeStarted("bridge started")

    monkeypatch.setattr(fuse_fs, "HandlerBridge", bridge)
    monkeypatch.setattr(fuse_fs, "get_partition_info", lambda *a, **kw: {
        "name": "DH0", "dostype": dostype, "block_size": 512})
    image = tmp_path / "disk.hdf"
    image.write_bytes(b"\xaa" * 4096)
    with pytest.raises(SystemExit) as caught:
        fuse_fs.format_volume(image, driver, None, "DH0", volname=volname)
    assert image.read_bytes() == b"\xaa" * 4096
    return str(caught.value.code), bool(started)


@pytest.mark.parametrize("length", [32, 40, 255])
def test_a_long_name_is_refused_before_the_handler_starts(monkeypatch, tmp_path,
                                                          length):
    message, started = _format(monkeypatch, tmp_path,
                               _driver(tmp_path, VER_19), PFS3,
                               (NAME_40 * 7)[:length])
    assert not started
    assert message == (f"Error: volume name is {length} characters long; "
                       "pfs3aio stores at most 31. Use a shorter name.")


def test_31_characters_go_on(monkeypatch, tmp_path):
    message, started = _format(monkeypatch, tmp_path,
                               _driver(tmp_path, VER_19), PFS3, NAME_40[:31])
    assert started
    assert message == "Error: bridge started"


def test_an_unreadable_driver_is_an_error(monkeypatch, tmp_path):
    message, started = _format(monkeypatch, tmp_path, tmp_path / "missing",
                               PFS3, NAME_40)
    assert not started
    assert message.startswith("Error: ")
    assert "missing" in message


@pytest.mark.parametrize("ver,dostype", [
    (VER_19, DOS3),
    (VER_20, PFS3),
    (b"", PFS3),
    (b"", MUPF),
], ids=["PFS3-VER-on-DOS3", "20.0", "no-VER-PFS3", "no-VER-muPF"])
def test_pfs3_gets_the_limit(tmp_path, ver, dostype):
    """A PFS3 $VER, whatever the DOS type and version, or no $VER on PFS."""
    assert fuse_fs._pfs3_volume_name_refusal(
        dostype, _driver(tmp_path, ver), NAME_40[:32]) == (
        "Error: volume name is 32 characters long; pfs3aio stores at most 31. "
        "Use a shorter name.")


@pytest.mark.parametrize("ver,dostype,volname", [
    (VER_SFS, PFS3, NAME_40),
    (b"", DOS3, NAME_40),
    (VER_19, PFS3, NAME_40[:31]),
], ids=["other-VER-on-PFS3", "no-VER-DOS3", "31-chars"])
def test_others_get_no_limit(tmp_path, ver, dostype, volname):
    assert fuse_fs._pfs3_volume_name_refusal(
        dostype, _driver(tmp_path, ver), volname) is None


def test_the_length_is_counted_in_latin1_bytes(tmp_path):
    """One byte per character, as the BSTR sends it."""
    driver = _driver(tmp_path, VER_19)
    # e-acute is Latin-1; the euro sign is not and is sent as "?".
    assert fuse_fs._pfs3_volume_name_refusal(PFS3, driver, chr(0xE9) * 31) is None
    assert fuse_fs._pfs3_volume_name_refusal(PFS3, driver, chr(0x20AC) * 32) == (
        "Error: volume name is 32 characters long; pfs3aio stores at most 31. "
        "Use a shorter name.")
