"""Unit tests for filesystem driver resolution and the bundled FastFileSystem.

Covers the bundled files and their provenance record, the folder lookup in
amifuse.driver_resolver (your drivers folder, the bundled FastFileSystem,
the dev fallback), fuse_fs.resolve_driver() in front of it (--driver, then
the RDB), and how doctor and inspect report the result.

Every test that resolves a driver points the user's drivers folder at
tmp_path and disables the dev fallback, through the user_driver_dir
fixture in conftest.py or the same patches.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from amifuse import driver_resolver
from amifuse.driver_resolver import DriverNotFound

DOS0 = 0x444F5300
ALPHA1_SHA256 = "0a9ffd7c7b144a172273d8b0e7099b4ea9a92d90a517a604f6aaecc5f58f8826"
HYPERION_VER = b"fs 47.4 (16.6.2020)"


def _dos(n: int) -> int:
    return DOS0 + n


def _user_copy(folder: Path, data: bytes = b"\x00\x00\x03\xf3" + b"\x01" * 64) -> Path:
    path = folder / "FastFileSystem"
    path.write_bytes(data)
    return path


def _unreadable(monkeypatch, target: Path):
    """Make reading target fail as a permission error would."""
    read_bytes = Path.read_bytes

    def fake(self):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake)


# ---------------------------------------------------------------------------
# A. The bundled files -- 3 tests
# ---------------------------------------------------------------------------


class TestBundledFiles:
    """The binary, licence and provenance shipped in amifuse/drivers/."""

    def test_provenance_matches_the_binary(self):
        """The provenance record describes the file that ships beside it."""
        info = json.loads(driver_resolver.BUNDLED_PROVENANCE.read_text(encoding="utf-8"))
        data = driver_resolver.BUNDLED_DRIVER.read_bytes()
        assert info["asset"] == driver_resolver.BUNDLED_DRIVER.name
        assert info["size"] == len(data) == 66724
        assert info["sha256"] == hashlib.sha256(data).hexdigest() == ALPHA1_SHA256
        assert info["tag"] == "alpha1"
        assert info["commit"] == "043b8030c1bac896e49385f8c328214b024c280d"
        assert info["license"] == "BSD-2-Clause"
        assert info["dos_types"] == ["DOS0", "DOS1", "DOS2", "DOS3", "DOS4", "DOS5"]
        assert info["build"] == {
            "image": "stefanreinauer/amiga-gcc:gcc-v13.4",
            "command": "make amigaos3",
            "reproducible": True,
        }

    def test_license_ships_beside_the_binary(self):
        """The BSD-2 notice is in the package, as its terms require."""
        text = driver_resolver.BUNDLED_LICENSE.read_text(encoding="utf-8")
        assert "Copyright (c) 2026 Stefan Reinauer" in text
        assert "Redistributions in binary form must reproduce" in text

    def test_placeholder_is_gone(self):
        """No file named FastFileSystem is bundled, so none is mistaken for
        a user's copy of Hyperion's FFS."""
        assert not (driver_resolver.BUNDLED_DIR / "FastFileSystem").exists()


# Ranges as written in help text and docs: DOS0-5, DOS0-DOS5, DOS6/7, DOS6/DOS7.
_FIXED_RANGE = re.compile(r"\bDOS([0-7])(?:([-/])(?:DOS)?([0-7]))?\b")


def _fixed_ranges(text: str):
    """The DOS types each range in text names, as sets of tags."""
    found = []
    for first, sep, last in _FIXED_RANGE.findall(text):
        if not sep:
            numbers = [int(first)]
        elif sep == "/":
            numbers = [int(first), int(last)]
        else:
            numbers = range(int(first), int(last) + 1)
        found.append({f"DOS{n}" for n in numbers})
    return found


def _bundled_dos_types():
    info = json.loads(driver_resolver.BUNDLED_PROVENANCE.read_text(encoding="utf-8"))
    return set(info["dos_types"])


class TestFixedTextMatchesProvenance:
    """Text that cannot read the provenance record states what it records."""

    def test_help_text(self):
        """Every "bundled FastFileSystem for <range>" in the CLI help names
        the DOS types in dos_types."""
        import amifuse.fuse_fs as fuse_fs_mod

        source = " ".join(Path(fuse_fs_mod.__file__).read_text(encoding="utf-8").split())
        mentions = re.findall(r"bundled FastFileSystem for (DOS[0-7][-/0-7DOS]*)", source)
        assert len(mentions) >= 8
        for mention in mentions:
            assert _fixed_ranges(mention) == [_bundled_dos_types()], mention

    def test_readme(self):
        """Every DOS range in the README's lines about the bundled handler
        is the covered set, the rest of DOS0-DOS7, or all of it."""
        readme = Path(__file__).resolve().parents[2] / "README.md"
        lines = readme.read_text(encoding="utf-8").splitlines()
        start = lines.index("## Bundled FastFileSystem")
        end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
        about = lines[start:end] + [line for line in lines if "bundled" in line.lower()]
        covered = _bundled_dos_types()
        every = {f"DOS{n}" for n in range(8)}
        ranges = _fixed_ranges("\n".join(about))
        assert len(ranges) >= 8
        for found in ranges:
            assert found in (covered, every - covered, every), sorted(found)


# ---------------------------------------------------------------------------
# B. Folder lookup -- user, bundled, dev fallback
# ---------------------------------------------------------------------------


class TestFolderLookup:
    """driver_resolver.find_folder_driver() for DOS0-DOS7 volumes."""

    @pytest.mark.parametrize("n", range(6))
    def test_bundled_serves_dos0_to_dos5(self, user_driver_dir, n):
        """With an empty drivers folder, DOS0-DOS5 get the bundled handler."""
        resolved = driver_resolver.find_folder_driver(_dos(n), "subject")
        assert resolved.source == "bundled"
        assert resolved.path == driver_resolver.BUNDLED_DRIVER
        assert resolved.temporary is False
        assert resolved.description == (
            f"FastFileSystem alpha1 (bundled, sha256 0a9ffd7c7b14) "
            f"{driver_resolver.BUNDLED_DRIVER}"
        )

    def test_user_copy_beats_bundled(self, user_driver_dir):
        """Your own FastFileSystem wins, and the banner names its $VER."""
        user = _user_copy(user_driver_dir, b"\x00\x00\x03\xf3$VER: " + HYPERION_VER + b"\x00rest")
        resolved = driver_resolver.find_folder_driver(_dos(1), "subject")
        assert resolved.source == "user"
        assert resolved.path == user
        assert resolved.temporary is False
        assert resolved.description == f"{user} (your drivers folder, fs 47.4 (16.6.2020))"

    def test_user_copy_without_ver_shows_sha256(self, user_driver_dir):
        """A user copy without $VER is named by its sha256 prefix."""
        data = b"\x00\x00\x03\xf3no version here"
        user = _user_copy(user_driver_dir, data)
        resolved = driver_resolver.find_folder_driver(_dos(3), "subject")
        digest = hashlib.sha256(data).hexdigest()
        assert resolved.description == f"{user} (your drivers folder, sha256 {digest[:12]})"
        assert resolved.sha256 == digest
        assert resolved.version is None

    @pytest.mark.parametrize("n", [6, 7])
    def test_user_copy_serves_dos6_and_dos7(self, user_driver_dir, n):
        """Long-name volumes use your own FastFileSystem."""
        user = _user_copy(user_driver_dir)
        assert driver_resolver.find_folder_driver(_dos(n), "subject").path == user

    def test_dos7_without_user_copy_explains(self, user_driver_dir):
        """The bundled build covers DOS0-DOS5; DOS7 says what to do."""
        with pytest.raises(DriverNotFound) as caught:
            driver_resolver.find_folder_driver(
                _dos(7), "ADF floppy image detected (DD, DOS7).")
        assert str(caught.value) == (
            "ADF floppy image detected (DD, DOS7).\n"
            "The bundled FastFileSystem supports DOS0-DOS5 only; DOS7 "
            "(long file names) needs another FFS.\n"
            "Place a FastFileSystem that supports DOS7 (e.g. from AmigaOS 3.2) "
            f"in {user_driver_dir}, or use --driver."
        )

    def test_empty_user_copy_is_skipped(self, user_driver_dir):
        """A 0-byte file in the drivers folder does not hide the bundled one."""
        (user_driver_dir / "FastFileSystem").write_bytes(b"")
        assert driver_resolver.find_folder_driver(_dos(1), "s").source == "bundled"

    def test_dev_fallback_comes_last(self, user_driver_dir, monkeypatch, tmp_path):
        """The dev fallback serves only what nothing before it serves."""
        dev = tmp_path / "AmiFUSE-testing" / "drivers" / "FastFileSystem"
        dev.parent.mkdir(parents=True)
        dev.write_bytes(b"dev")
        monkeypatch.setattr(driver_resolver, "DEV_FALLBACK_DRIVER", dev)

        assert driver_resolver.find_folder_driver(_dos(1), "s").source == "bundled"
        resolved = driver_resolver.find_folder_driver(_dos(7), "s")
        assert resolved.source == "dev_fallback"
        assert resolved.description == f"{dev} (dev fallback)"
        user = _user_copy(user_driver_dir)
        assert driver_resolver.find_folder_driver(_dos(7), "s").path == user

    @pytest.mark.parametrize("state", ["missing", "empty"])
    def test_broken_install_names_the_bundled_path(
            self, user_driver_dir, monkeypatch, tmp_path, state):
        """A missing or empty bundled file is reported by its expected path."""
        bundled = tmp_path / "pkg" / "FastFileSystem.OS3"
        if state == "empty":
            bundled.parent.mkdir()
            bundled.write_bytes(b"")
        monkeypatch.setattr(driver_resolver, "BUNDLED_DRIVER", bundled)
        with pytest.raises(DriverNotFound) as caught:
            driver_resolver.find_folder_driver(_dos(1), "Subject line.")
        assert str(caught.value) == (
            "Subject line.\n"
            f"The bundled FastFileSystem is missing or damaged: {bundled}\n"
            f"Reinstall AmiFUSE, place a FastFileSystem in {user_driver_dir}, or use --driver."
        )

    def test_checksum_mismatch_warns_and_still_uses_it(
            self, user_driver_dir, monkeypatch, tmp_path, capsys):
        """A changed bundled file is reported, not refused."""
        bundled = tmp_path / "FastFileSystem.OS3"
        bundled.write_bytes(b"changed")
        monkeypatch.setattr(driver_resolver, "BUNDLED_DRIVER", bundled)
        resolved = driver_resolver.find_folder_driver(_dos(1), "s")
        assert resolved.path == bundled
        digest = hashlib.sha256(b"changed").hexdigest()
        assert f"sha256 {digest[:12]}" in resolved.description
        err = capsys.readouterr().err
        assert "does not match the sha256" in err and ALPHA1_SHA256 in err

    def test_quiet_skips_the_checksum_warning(
            self, user_driver_dir, monkeypatch, tmp_path, capsys):
        """doctor resolves quietly and reports a mismatch in its own check."""
        bundled = tmp_path / "FastFileSystem.OS3"
        bundled.write_bytes(b"changed")
        monkeypatch.setattr(driver_resolver, "BUNDLED_DRIVER", bundled)
        driver_resolver.find_folder_driver(_dos(1), "s", quiet=True)
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("which", ["user", "bundled"])
    def test_unreadable_driver_names_the_file(
            self, user_driver_dir, monkeypatch, which):
        """A driver file that cannot be read is a DriverNotFound naming it."""
        target = driver_resolver.BUNDLED_DRIVER
        if which == "user":
            target = _user_copy(user_driver_dir)
        _unreadable(monkeypatch, target)
        with pytest.raises(DriverNotFound) as caught:
            driver_resolver.find_folder_driver(_dos(1), "Subject line.")
        lines = str(caught.value).splitlines()
        assert lines[0] == "Subject line."
        assert lines[1].startswith("Cannot read ")
        assert f"{target} (Permission denied)" in lines[1]
        assert "--driver" in lines[2]

    @pytest.mark.parametrize("content", [None, "{not json", '{"tag": "alpha1"}'])
    def test_broken_provenance_names_the_json(
            self, user_driver_dir, monkeypatch, tmp_path, content):
        """A missing, invalid or incomplete provenance record is named as
        the cause, not the binary beside it."""
        record = tmp_path / "FastFileSystem.OS3.json"
        if content is not None:
            record.write_text(content, encoding="utf-8")
        monkeypatch.setattr(driver_resolver, "BUNDLED_PROVENANCE", record)
        with pytest.raises(DriverNotFound) as caught:
            driver_resolver.find_folder_driver(_dos(1), "Subject line.")
        lines = str(caught.value).splitlines()
        assert lines[0] == "Subject line."
        assert "provenance record" in lines[1] and str(record) in lines[1]
        assert "missing or damaged" not in lines[1]
        assert lines[2] == (
            f"Reinstall AmiFUSE, place a FastFileSystem in {user_driver_dir}, "
            "or use --driver.")

    def test_coverage_comes_from_the_provenance(
            self, user_driver_dir, monkeypatch, tmp_path):
        """The covered DOS types and the error text follow dos_types."""
        record = tmp_path / "FastFileSystem.OS3.json"
        info = json.loads(driver_resolver.BUNDLED_PROVENANCE.read_text(encoding="utf-8"))
        info["dos_types"] = ["DOS0", "DOS1", "DOS2", "DOS3", "DOS6"]
        record.write_text(json.dumps(info), encoding="utf-8")
        monkeypatch.setattr(driver_resolver, "BUNDLED_PROVENANCE", record)
        assert driver_resolver.find_folder_driver(_dos(6), "s").source == "bundled"
        with pytest.raises(DriverNotFound) as caught:
            driver_resolver.find_folder_driver(_dos(4), "s")
        assert str(caught.value).splitlines()[1] == (
            "The bundled FastFileSystem supports DOS0-DOS3, DOS6 only; "
            "DOS4 needs another FFS.")

    def test_windows_without_localappdata_uses_the_home_folder(
            self, monkeypatch, tmp_path):
        """Lookup and messages share get_primary_driver_dir(): with
        LOCALAPPDATA unset, Windows searches the home fallback it names."""
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(
            driver_resolver, "DEV_FALLBACK_DRIVER", tmp_path / "no-dev" / "FastFileSystem")
        folder = tmp_path / ".local" / "share" / "amifuse" / "drivers"
        assert driver_resolver.user_driver_dir() == folder
        folder.mkdir(parents=True)
        user = _user_copy(folder)
        assert driver_resolver.find_folder_driver(_dos(7), "s").path == user


class TestFindVersion:
    """The generic $VER scan."""

    def test_reads_the_version_text(self):
        assert driver_resolver.find_version(
            b"\x00\x01$VER: " + HYPERION_VER + b"\x00\x02") == "fs 47.4 (16.6.2020)"

    def test_stops_at_a_line_break(self):
        assert driver_resolver.find_version(b"$VER: FFS 1.0 (1.1.2027)\r\nx") == "FFS 1.0 (1.1.2027)"

    def test_none_without_a_version(self):
        assert driver_resolver.find_version(b"no version") is None

    def test_space_after_the_colon_is_optional(self):
        assert driver_resolver.find_version(b"$VER:FFS 2.0 (1.1.2027)\x00") == "FFS 2.0 (1.1.2027)"

    def test_stops_at_a_non_ascii_byte(self):
        """Only printable ASCII is taken, so stray bytes never reach a banner."""
        assert driver_resolver.find_version(b"$VER: FFS 2.0\xa0\xffjunk") == "FFS 2.0"
        assert driver_resolver.find_version(b"$VER: \xe9t\xe9") is None


class TestDosRange:
    """Compact names for sets of DOS types."""

    @pytest.mark.parametrize("tags, text", [
        (["DOS0", "DOS1", "DOS2", "DOS3", "DOS4", "DOS5"], "DOS0-DOS5"),
        (["DOS7", "DOS6"], "DOS6-DOS7"),
        (["DOS3"], "DOS3"),
        (["DOS0", "DOS1", "DOS3", "DOS5", "DOS6"], "DOS0-DOS1, DOS3, DOS5-DOS6"),
    ])
    def test_ranges(self, tags, text):
        assert driver_resolver.dos_range(tags) == text

    def test_alpha1_has_none_yet(self):
        """alpha1 has no $VER, so reports use its release tag."""
        assert driver_resolver.find_version(driver_resolver.BUNDLED_DRIVER.read_bytes()) is None


@pytest.fixture
def no_home(monkeypatch, tmp_path):
    """No home folder: Path.home() raises, as on Windows without these
    variables, so there is no drivers folder; no dev fallback either."""
    for name in ("LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        driver_resolver, "DEV_FALLBACK_DRIVER", tmp_path / "no-dev" / "FastFileSystem")
    try:
        Path.home()
    except RuntimeError:
        return
    # POSIX falls back to the password database; fail as Windows does.
    def home(cls):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", classmethod(home))


class TestNoHomeFolder:
    """Without a home folder, the drivers folder counts as missing."""

    def test_lookup_and_doctor(self, no_home):
        from amifuse import doctor

        assert driver_resolver.user_driver_dir() is None
        assert driver_resolver.user_copy_problem() is None
        assert driver_resolver.find_folder_driver(_dos(1), "s").source == "bundled"
        with pytest.raises(DriverNotFound) as caught:
            driver_resolver.find_folder_driver(_dos(7), "s")
        assert str(caught.value).endswith(
            "in your drivers folder (no home folder found), or use --driver.")
        check = doctor._drivers_check()
        assert check.status == "ok"
        assert check.message == (
            "DOS0-DOS5: FastFileSystem alpha1 (bundled, sha256 0a9ffd7c7b14) "
            f"{driver_resolver.BUNDLED_DRIVER}; DOS6-DOS7: none; place your own "
            "FastFileSystem in your drivers folder (no home folder found)")

    def test_python39_key_error(self, no_home, monkeypatch):
        """Python 3.9's Path.home() raises KeyError when the password
        database has no entry for the user."""
        def home(cls):
            raise KeyError("getpwuid(): uid not found: 1000")

        monkeypatch.setattr(Path, "home", classmethod(home))
        assert driver_resolver.user_driver_dir() is None

    def test_inspect(self, no_home, tmp_path, capsys):
        from amifuse.fuse_fs import main

        main(["inspect", "--json", str(_adf(tmp_path, 1))])
        assert json.loads(capsys.readouterr().out)["driver"]["source"] == "bundled"
        main(["inspect", "--json", str(_adf(tmp_path, 7, "dos7.adf"))])
        data = json.loads(capsys.readouterr().out)
        assert data["driver"] is None
        assert "in your drivers folder (no home folder found)" in data["note"]


# ---------------------------------------------------------------------------
# C. fuse_fs.resolve_driver -- --driver and the RDB in front of the lookup
# ---------------------------------------------------------------------------


class TestResolveDriver:
    """The full order: --driver, RDB entry, user, bundled, dev fallback."""

    def test_driver_option_wins(self, fuse_mock, user_driver_dir, monkeypatch, tmp_path):
        """--driver is used as given; the RDB is not even read."""
        import amifuse.fuse_fs as fuse_fs_mod

        extract = MagicMock()
        monkeypatch.setattr(fuse_fs_mod, "extract_embedded_driver", extract)
        _user_copy(user_driver_dir)
        given = tmp_path / "pfs3aio"
        resolved = fuse_fs_mod.resolve_driver(tmp_path / "x.hdf", given, None, None)
        assert (resolved.path, resolved.source, resolved.description) == (
            given, "explicit", str(given))
        assert resolved.temporary is False
        extract.assert_not_called()

    def test_rdb_entry_beats_folders(self, fuse_mock, user_driver_dir, monkeypatch, tmp_path):
        """A driver in the RDB for the exact DOS type wins over your folder."""
        import amifuse.fuse_fs as fuse_fs_mod

        temp = tmp_path / "amifuse_x_DOS3.handler"
        monkeypatch.setattr(fuse_fs_mod, "extract_embedded_driver",
                            lambda *a: (temp, "DOS3", _dos(3)))
        _user_copy(user_driver_dir)
        resolved = fuse_fs_mod.resolve_driver(tmp_path / "x.hdf", None, None, None)
        assert resolved.source == "rdb"
        assert resolved.temporary is True
        assert resolved.path == temp
        assert resolved.description == "DOS3/0x444f5303 (from RDB)"

    @pytest.mark.parametrize("n", range(6))
    def test_rdb_dos_partition_without_entry_gets_bundled(
            self, fuse_mock, user_driver_dir, monkeypatch, tmp_path, n):
        """A DOS0-DOS5 partition with no RDB driver falls back to bundled."""
        import amifuse.fuse_fs as fuse_fs_mod

        monkeypatch.setattr(fuse_fs_mod, "extract_embedded_driver", lambda *a: None)
        monkeypatch.setattr(fuse_fs_mod, "get_partition_info",
                            lambda *a: {"name": "DH0", "dostype": _dos(n)})
        resolved = fuse_fs_mod.resolve_driver(tmp_path / "x.hdf", None, None, "DH0")
        assert resolved.source == "bundled"
        assert resolved.temporary is False

    def test_rdb_partition_info_from_caller_is_reused(
            self, fuse_mock, user_driver_dir, monkeypatch, tmp_path):
        """Mount and format pass the partition info they already read."""
        import amifuse.fuse_fs as fuse_fs_mod

        monkeypatch.setattr(fuse_fs_mod, "extract_embedded_driver", lambda *a: None)
        reader = MagicMock()
        monkeypatch.setattr(fuse_fs_mod, "get_partition_info", reader)
        resolved = fuse_fs_mod.resolve_driver(
            tmp_path / "x.hdf", None, None, None,
            part_info={"name": "DH0", "dostype": _dos(3)})
        assert resolved.source == "bundled"
        reader.assert_not_called()

    def test_rdb_dos7_partition_without_entry_explains(
            self, fuse_mock, user_driver_dir, monkeypatch, tmp_path):
        """A DOS7 partition with no RDB driver and no user copy is refused."""
        import amifuse.fuse_fs as fuse_fs_mod

        monkeypatch.setattr(fuse_fs_mod, "extract_embedded_driver", lambda *a: None)
        with pytest.raises(DriverNotFound) as caught:
            fuse_fs_mod.resolve_driver(
                tmp_path / "x.hdf", None, None, None,
                part_info={"name": "DH1", "dostype": _dos(7)})
        message = str(caught.value)
        assert message.startswith(
            "Partition DH1 has no filesystem driver for DOS7 in its RDB.\n"
            "The bundled FastFileSystem supports DOS0-DOS5 only;")

    def test_non_dos_partition_keeps_todays_error(
            self, fuse_mock, user_driver_dir, monkeypatch, tmp_path):
        """PFS3, SFS and other non-DOS types never get FastFileSystem."""
        import amifuse.fuse_fs as fuse_fs_mod

        monkeypatch.setattr(fuse_fs_mod, "extract_embedded_driver", lambda *a: None)
        _user_copy(user_driver_dir)
        with pytest.raises(DriverNotFound) as caught:
            fuse_fs_mod.resolve_driver(
                tmp_path / "x.hdf", None, None, None,
                part_info={"name": "PDH0", "dostype": 0x50465303})
        assert str(caught.value) == (
            "No embedded filesystem driver found for this partition.\n"
            "You need to specify a filesystem handler with --driver"
        )

    def test_adf_uses_the_folder_lookup(self, fuse_mock, user_driver_dir, monkeypatch, tmp_path):
        """An ADF never reads an RDB; its error names the floppy."""
        import amifuse.fuse_fs as fuse_fs_mod

        extract = MagicMock()
        monkeypatch.setattr(fuse_fs_mod, "extract_embedded_driver", extract)
        adf = SimpleNamespace(dos_type=_dos(1), is_hd=False)
        resolved = fuse_fs_mod.resolve_driver(tmp_path / "x.adf", None, None, None, adf_info=adf)
        assert resolved.source == "bundled"
        hd7 = SimpleNamespace(dos_type=_dos(7), is_hd=True)
        with pytest.raises(DriverNotFound) as caught:
            fuse_fs_mod.resolve_driver(tmp_path / "x.adf", None, None, None, adf_info=hd7)
        assert str(caught.value).startswith("ADF floppy image detected (HD, DOS7).\n")
        extract.assert_not_called()

    def test_iso_needs_the_driver_option(self, fuse_mock, user_driver_dir, monkeypatch, tmp_path):
        """An ISO gets --driver or one error, for mount and the file commands."""
        import amifuse.fuse_fs as fuse_fs_mod

        extract = MagicMock()
        monkeypatch.setattr(fuse_fs_mod, "extract_embedded_driver", extract)
        _user_copy(user_driver_dir)
        iso = SimpleNamespace(volume_id="CD_VOL")
        with pytest.raises(DriverNotFound) as caught:
            fuse_fs_mod.resolve_driver(tmp_path / "x.iso", None, None, None, iso_info=iso)
        assert str(caught.value) == (
            "ISO 9660 image detected (volume: CD_VOL).\n"
            "ISO images don't contain embedded filesystem drivers.\n"
            "You need to specify a filesystem handler with --driver.\n"
            "For ISO 9660, use CDFileSystem from an AmigaOS installation."
        )
        given = tmp_path / "CDFileSystem"
        resolved = fuse_fs_mod.resolve_driver(
            tmp_path / "x.iso", given, None, None, iso_info=iso)
        assert (resolved.path, resolved.source) == (given, "explicit")
        extract.assert_not_called()


AMITOOLS_ROOT = Path(__file__).resolve().parents[2] / "amitools"


def _rdb(tmp_path, part, fs=(), name="disk.hdf", drive="DH0"):
    """An rdbtool image with partition drive of DOS type part and, given fs,
    the bundled FastFileSystem in its RDB under DOS type fs: one type, or a
    tuple of types, each a further entry in the RDB's driver list."""
    image = tmp_path / name
    fs = (fs,) if isinstance(fs, str) else fs
    # 8 RDB cylinders hold one copy of the driver.
    args = ["create", "size=8Mi", "+", "init", f"rdb_cyls={8 * max(1, len(fs))}",
            "+", "add", "size=6MiB", f"name={drive}", f"fs={part}"]
    for dos_type in fs:
        args += ["+", "fsadd", str(driver_resolver.BUNDLED_DRIVER), f"dostype={dos_type}"]
    proc = subprocess.run(
        [sys.executable, "-m", "amitools.tools.rdbtool", str(image), *args],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return image


def _flip_byte(image, tag: bytes, nth: int = 0):
    """Flip one byte in the nth block tagged tag, as a bad sector would.
    Returns the block's number."""
    data = image.read_bytes()
    blocks = [o for o in range(0, len(data), 512) if data[o:o + 4] == tag]
    offset = blocks[nth] + 100
    with open(image, "r+b") as f:
        f.seek(offset)
        f.write(bytes([data[offset] ^ 0xFF]))
    return blocks[nth] // 512


@pytest.fixture
def temp_dir(monkeypatch, tmp_path):
    """Where RDB drivers are extracted, to check that none is left."""
    folder = tmp_path / "temp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


class TestRdbImages:
    """resolve_driver() on rdbtool images."""

    def test_damaged_driver_is_not_replaced(self, user_driver_dir, tmp_path, temp_dir):
        """A damaged RDB driver for the partition's DOS type stops the
        lookup, even with your own copy present; --driver overrides it."""
        from amifuse.fuse_fs import resolve_driver

        image = _rdb(tmp_path, "DOS3", "DOS3")
        assert resolve_driver(image, None, None, None).source == "rdb"
        _flip_byte(image, b"LSEG")
        _user_copy(user_driver_dir)
        with pytest.raises(DriverNotFound) as caught:
            resolve_driver(image, None, None, None)
        assert str(caught.value) == (
            "Partition DH0 (DOS3) has a damaged filesystem driver in its RDB: "
            "Filesystem #0 (DOS3/0x444f5303): corrupt data block in LSEG chain "
            "(driver data unavailable).\n"
            "AmiFUSE does not use another driver in its place; use --driver to choose one.")
        given = driver_resolver.BUNDLED_DRIVER
        assert resolve_driver(image, given, None, None).source == "explicit"

    def test_unreadable_header_may_hide_the_driver(self, user_driver_dir, tmp_path, temp_dir):
        """A corrupt filesystem header hides its DOS type, so it stops the
        lookup too, even with your own copy present; --driver overrides it."""
        from amifuse.fuse_fs import resolve_driver

        image = _rdb(tmp_path, "DOS3", "DOS3")
        block = _flip_byte(image, b"FSHD")
        _user_copy(user_driver_dir)
        with pytest.raises(DriverNotFound) as caught:
            resolve_driver(image, None, None, None)
        assert str(caught.value) == (
            "Partition DH0 (DOS3) may have a damaged filesystem driver in its RDB: "
            f"Corrupt filesystem header at block {block} "
            "(remaining filesystem entries skipped).\n"
            "AmiFUSE does not use another driver in its place; use --driver to choose one.")
        given = driver_resolver.BUNDLED_DRIVER
        assert resolve_driver(image, given, None, None).source == "explicit"

    def test_damaged_driver_for_another_dos_type_is_ignored(
            self, user_driver_dir, tmp_path, temp_dir):
        """A damaged RDB driver for another DOS type does not stop the
        lookup: the DOS1 partition gets the bundled handler."""
        from amifuse.fuse_fs import resolve_driver

        image = _rdb(tmp_path, "DOS1", "DOS3")
        _flip_byte(image, b"LSEG")
        assert resolve_driver(image, None, None, None).source == "bundled"
        assert list(temp_dir.iterdir()) == []

    def test_readable_driver_before_a_corrupt_header_is_used(
            self, user_driver_dir, tmp_path, temp_dir):
        """A corrupt header after the partition's readable driver hides
        nothing it needs."""
        from amifuse.fuse_fs import resolve_driver
        from amifuse.rdb_inspect import open_rdisk

        image = _rdb(tmp_path, "DOS3", ("DOS3", "DOS1"))
        _flip_byte(image, b"FSHD", nth=1)
        blkdev, rdisk, _ = open_rdisk(image)
        try:
            assert [fs.fshd.dos_type for fs in rdisk.fs] == [_dos(3)]
            assert [dos_type for dos_type, _ in rdisk.damaged_fs] == [None]
        finally:
            rdisk.close()
            blkdev.close()
        assert resolve_driver(image, None, None, None).source == "rdb"

    def test_file_commands_report_driver_not_found(self, user_driver_dir, tmp_path, capsys):
        from amifuse.fuse_fs import main

        image = _rdb(tmp_path, "DOS3", "DOS3")
        _flip_byte(image, b"LSEG")
        with pytest.raises(SystemExit):
            main(["ls", "--json", str(image)])
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "DRIVER_NOT_FOUND"

    def test_driver_for_another_dos_type(self, user_driver_dir, tmp_path, temp_dir):
        """An RDB driver serves only its own DOS type; a DOS1 partition
        beside it gets the bundled handler, as it would get the FFS in ROM."""
        from amifuse.fuse_fs import resolve_driver

        image = _rdb(tmp_path, "DOS1", "DOS3")
        assert resolve_driver(image, None, None, None).source == "bundled"
        image7 = _rdb(tmp_path, "DOS7", "DOS3", "dos7.hdf")
        with pytest.raises(DriverNotFound) as caught:
            resolve_driver(image7, None, None, None)
        assert str(caught.value).startswith(
            "Partition DH0 has no filesystem driver for DOS7 in its RDB.\n")
        assert list(temp_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# D. Cleanup never deletes a bundled or user driver
# ---------------------------------------------------------------------------


class TestNothingPermanentIsDeleted:
    """Only an RDB extraction is temporary."""

    @pytest.mark.parametrize("source", ["bundled", "user"])
    def test_file_commands_return_no_temp_driver(
            self, fuse_mock, user_driver_dir, monkeypatch, tmp_path, source):
        """An ADF's driver is never handed to _cleanup_bridge for deletion."""
        import amifuse.fuse_fs as fuse_fs_mod

        image = tmp_path / "test.adf"
        image.write_bytes(b"DOS\x01")
        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = SimpleNamespace(dos_type=_dos(1), is_hd=False)
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)
        expected = driver_resolver.BUNDLED_DRIVER
        if source == "user":
            expected = _user_copy(user_driver_dir)
        bridge_cls = MagicMock()
        monkeypatch.setattr(fuse_fs_mod, "HandlerBridge", bridge_cls)

        args = argparse.Namespace(image=image, json=False, partition=None,
                                  driver=None, block_size=None, debug=False)
        bridge, temp_driver = fuse_fs_mod._create_bridge_from_args(args, "ls")
        assert bridge_cls.call_args.args[1] == expected
        assert temp_driver is None
        fuse_fs_mod._cleanup_bridge(bridge, temp_driver)
        assert expected.exists()


# ---------------------------------------------------------------------------
# E. Reporting: doctor and inspect
# ---------------------------------------------------------------------------


def _drivers_check():
    from amifuse.doctor import run_checks

    return next(c for c in run_checks() if c.name == "drivers")


class TestDoctorDriversCheck:
    """doctor's drivers check checks the bundled handler and names the
    handler for DOS0-DOS7, grouped by handler."""

    @pytest.fixture(autouse=True)
    def _quiet_other_checks(self, monkeypatch):
        monkeypatch.setattr("amifuse.doctor.subprocess.run",
                            MagicMock(return_value=SimpleNamespace(returncode=0)))
        monkeypatch.setattr("amifuse.platform.detect_fuse_backend",
                            lambda: {"installed": True, "name": "FUSE", "version": None})

    def test_bundled_only(self, user_driver_dir):
        check = _drivers_check()
        assert check.status == "ok"
        assert check.message == (
            "DOS0-DOS5: FastFileSystem alpha1 (bundled, sha256 0a9ffd7c7b14) "
            f"{driver_resolver.BUNDLED_DRIVER}; "
            f"DOS6-DOS7: none; place your own FastFileSystem in {user_driver_dir}"
        )

    def test_user_copy_serves_everything(self, user_driver_dir):
        user = _user_copy(user_driver_dir, b"$VER: " + HYPERION_VER + b"\x00")
        check = _drivers_check()
        assert check.status == "ok"
        assert check.message == (
            f"DOS0-DOS7: {user} (your drivers folder, fs 47.4 (16.6.2020))")

    def test_missing_bundled_warns(self, user_driver_dir, monkeypatch, tmp_path):
        bundled = tmp_path / "pkg" / "FastFileSystem.OS3"
        monkeypatch.setattr(driver_resolver, "BUNDLED_DRIVER", bundled)
        check = _drivers_check()
        assert check.status == "warning"
        assert check.message == (
            f"The bundled FastFileSystem is missing or damaged: {bundled}; "
            f"DOS0-DOS7: none; place your own FastFileSystem in {user_driver_dir}"
        )
        assert str(user_driver_dir) in check.fix_description

    def test_dev_fallback_does_not_hide_a_broken_install(
            self, user_driver_dir, monkeypatch, tmp_path):
        """A missing provenance record warns, naming the record, even when
        the dev fallback serves every DOS type."""
        dev = tmp_path / "AmiFUSE-testing" / "drivers" / "FastFileSystem"
        dev.parent.mkdir(parents=True)
        dev.write_bytes(b"dev")
        monkeypatch.setattr(driver_resolver, "DEV_FALLBACK_DRIVER", dev)
        record = tmp_path / "pkg" / "FastFileSystem.OS3.json"
        monkeypatch.setattr(driver_resolver, "BUNDLED_PROVENANCE", record)
        check = _drivers_check()
        assert check.status == "warning"
        first, rest = check.message.split("; ", 1)
        assert first.startswith(
            "Cannot read the bundled FastFileSystem's provenance record: "
            f"{record} (")
        assert rest == f"DOS0-DOS7: {dev} (dev fallback)"

    def test_checksum_mismatch_is_in_the_check(
            self, user_driver_dir, monkeypatch, tmp_path, capsys):
        """A changed bundled file warns inside the check, not on stderr."""
        bundled = tmp_path / "FastFileSystem.OS3"
        bundled.write_bytes(b"changed")
        monkeypatch.setattr(driver_resolver, "BUNDLED_DRIVER", bundled)
        check = _drivers_check()
        assert check.status == "warning"
        assert check.message.startswith(
            "The bundled FastFileSystem does not match the sha256 recorded in "
            f"FastFileSystem.OS3.json ({ALPHA1_SHA256}): {bundled}; DOS0-DOS5: ")
        assert capsys.readouterr().err == ""

    def test_unreadable_user_copy_warns(self, user_driver_dir, monkeypatch):
        """An unreadable copy in the drivers folder is a warning, not a crash."""
        user = _user_copy(user_driver_dir)
        _unreadable(monkeypatch, user)
        check = _drivers_check()
        assert check.status == "warning"
        assert check.message.startswith(
            f"Cannot read your FastFileSystem: {user} (Permission denied); DOS0-DOS7: none")

    def test_groups_follow_the_provenance(self, user_driver_dir, monkeypatch, tmp_path):
        """DOS types are grouped by the handler that serves them."""
        record = tmp_path / "FastFileSystem.OS3.json"
        info = json.loads(driver_resolver.BUNDLED_PROVENANCE.read_text(encoding="utf-8"))
        info["dos_types"] = [f"DOS{n}" for n in range(8)]
        record.write_text(json.dumps(info), encoding="utf-8")
        monkeypatch.setattr(driver_resolver, "BUNDLED_PROVENANCE", record)
        check = _drivers_check()
        assert check.status == "ok"
        assert check.message.startswith("DOS0-DOS7: FastFileSystem alpha1 (bundled")
        assert ";" not in check.message

    def test_doctor_json_reports_the_check(self, user_driver_dir, capsys):
        from amifuse.doctor import cmd_doctor

        with pytest.raises(SystemExit):
            cmd_doctor(SimpleNamespace(json=True, fix=False))
        checks = json.loads(capsys.readouterr().out)["checks"]
        drivers = next(c for c in checks if c["name"] == "drivers")
        assert drivers["status"] == "ok"
        assert drivers["message"].startswith("DOS0-DOS5: FastFileSystem alpha1 (bundled")


def _adf(tmp_path, n, name="test.adf"):
    image = tmp_path / name
    image.write_bytes(b"DOS" + bytes([n]) + bytes(901116))
    return image


class TestInspectAdf:
    """inspect names the handler an ADF would mount with."""

    def test_json_names_the_bundled_handler(self, user_driver_dir, tmp_path, capsys):
        from amifuse.fuse_fs import main

        main(["inspect", "--json", str(_adf(tmp_path, 1))])
        data = json.loads(capsys.readouterr().out)
        assert data["driver"] == {
            "source": "bundled",
            "path": str(driver_resolver.BUNDLED_DRIVER),
            "version": None,
            "tag": "alpha1",
            "sha256": ALPHA1_SHA256,
        }

    def test_json_names_the_user_copy(self, user_driver_dir, tmp_path, capsys):
        from amifuse.fuse_fs import main

        data = b"$VER: " + HYPERION_VER + b"\x00"
        user = _user_copy(user_driver_dir, data)
        main(["inspect", "--json", str(_adf(tmp_path, 7))])
        driver = json.loads(capsys.readouterr().out)["driver"]
        assert driver == {
            "source": "user",
            "path": str(user),
            "version": "fs 47.4 (16.6.2020)",
            "tag": None,
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def test_json_dev_fallback_reports_what_it_can(
            self, user_driver_dir, monkeypatch, tmp_path, capsys):
        from amifuse.fuse_fs import main

        dev = tmp_path / "AmiFUSE-testing" / "drivers" / "FastFileSystem"
        dev.parent.mkdir(parents=True)
        dev.write_bytes(b"$VER: " + HYPERION_VER + b"\x00")
        monkeypatch.setattr(driver_resolver, "DEV_FALLBACK_DRIVER", dev)
        main(["inspect", "--json", str(_adf(tmp_path, 7))])
        driver = json.loads(capsys.readouterr().out)["driver"]
        assert driver["source"] == "dev_fallback"
        assert driver["version"] == "fs 47.4 (16.6.2020)"
        assert driver["tag"] is None
        assert driver["sha256"] == hashlib.sha256(dev.read_bytes()).hexdigest()

    def test_unreadable_user_copy_is_reported(
            self, user_driver_dir, monkeypatch, tmp_path, capsys):
        """inspect reports an unreadable driver instead of crashing."""
        from amifuse.fuse_fs import main

        user = _user_copy(user_driver_dir)
        _unreadable(monkeypatch, user)
        main(["inspect", "--json", str(_adf(tmp_path, 1))])
        data = json.loads(capsys.readouterr().out)
        assert data["driver"] is None
        assert f"Cannot read your FastFileSystem: {user}" in data["note"]

    def test_json_driver_is_null_for_dos7_without_user_copy(self, user_driver_dir, tmp_path, capsys):
        from amifuse.fuse_fs import main

        main(["inspect", "--json", str(_adf(tmp_path, 7))])
        data = json.loads(capsys.readouterr().out)
        assert data["driver"] is None
        assert "DOS7 (long file names) needs another FFS" in data["note"]

    def test_text_names_the_handler(self, user_driver_dir, tmp_path, capsys):
        from amifuse.fuse_fs import main

        main(["inspect", str(_adf(tmp_path, 3))])
        out = capsys.readouterr().out
        assert (
            "Filesystem driver: FastFileSystem alpha1 (bundled, sha256 0a9ffd7c7b14)"
            in out
        )


class TestInspectRdb:
    """inspect names the handler each RDB partition would mount with."""

    def _partition(self, image, capsys):
        from amifuse.fuse_fs import main

        main(["inspect", "--json", str(image)])
        [part] = json.loads(capsys.readouterr().out)["partitions"]
        return part

    def test_json_rdb_driver(self, user_driver_dir, tmp_path, temp_dir, capsys):
        """The RDB's driver is hashed and its extracted copy deleted."""
        part = self._partition(_rdb(tmp_path, "DOS3", "DOS3"), capsys)
        assert part["driver"] == {
            "source": "rdb", "path": None, "version": None, "tag": None,
            "sha256": ALPHA1_SHA256,
        }
        assert "driver_note" not in part
        assert list(temp_dir.iterdir()) == []

    def test_json_bundled_driver(self, user_driver_dir, tmp_path, capsys):
        """The same object as for an ADF."""
        part = self._partition(_rdb(tmp_path, "DOS3"), capsys)
        assert part["driver"] == {
            "source": "bundled",
            "path": str(driver_resolver.BUNDLED_DRIVER),
            "version": None,
            "tag": "alpha1",
            "sha256": ALPHA1_SHA256,
        }

    def test_json_damaged_driver(self, user_driver_dir, tmp_path, capsys):
        image = _rdb(tmp_path, "DOS3", "DOS3")
        _flip_byte(image, b"LSEG")
        part = self._partition(image, capsys)
        assert part["driver"] is None
        assert part["driver_note"].startswith(
            "Partition DH0 (DOS3) has a damaged filesystem driver in its RDB: ")

    def test_json_unreadable_extracted_driver(
            self, user_driver_dir, tmp_path, temp_dir, monkeypatch, capsys):
        """A failed read of the extracted RDB driver is reported, and the
        extracted copy is still deleted."""
        image = _rdb(tmp_path, "DOS3", "DOS3")
        read_bytes = Path.read_bytes

        def fake(self):
            if self.parent == temp_dir:
                raise PermissionError(13, "Permission denied", str(self))
            return read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", fake)
        part = self._partition(image, capsys)
        assert part["driver"] is None
        assert part["driver_note"].startswith(
            "cannot read the driver extracted from the RDB: [Errno 13] Permission denied: ")
        assert list(temp_dir.iterdir()) == []

    def test_text_names_the_handler(self, user_driver_dir, tmp_path, capsys):
        from amifuse.fuse_fs import main

        main(["inspect", str(_rdb(tmp_path, "DOS3"))])
        out = capsys.readouterr().out
        assert (
            "Filesystem driver per partition (without --driver):\n"
            "  DH0: FastFileSystem alpha1 (bundled, sha256 0a9ffd7c7b14)"
        ) in out


def _two_rdbs(tmp_path, second_drive):
    """Two 0x76 MBR entries: DH0 with the bundled driver in its RDB, then
    second_drive with no driver in its RDB, so the bundled file serves it."""
    from tests.unit.test_mbr_rdb_block_size import _make_mbr

    first = _rdb(tmp_path, "DOS3", "DOS3", name="first.hdf").read_bytes()
    second = _rdb(tmp_path, "DOS3", name="second.hdf",
                  drive=second_drive).read_bytes()
    return _make_mbr(tmp_path, [(64, first), (64 + len(first) // 512, second)])


class TestInspectMultiRdb:
    """Each partition's driver comes from the RDB mount would open for it."""

    def _partitions(self, image, capsys):
        from amifuse.fuse_fs import main

        main(["inspect", "--json", str(image)])
        rdbs = json.loads(capsys.readouterr().out)["rdbs"]
        return [part for rdb in rdbs for part in rdb["partitions"]]

    def test_json_distinct_names(self, user_driver_dir, tmp_path, capsys):
        first, second = self._partitions(_two_rdbs(tmp_path, "DH1"), capsys)
        assert first["driver"]["source"] == "rdb"
        assert second["driver"]["source"] == "bundled"

    def test_json_shared_name_is_not_given_the_first_rdbs_driver(
            self, user_driver_dir, tmp_path, capsys):
        """mount --partition dh0 opens the first RDB, whose DH0 matches the
        name, so the second RDB's dh0 has no driver mount would use."""
        first, second = self._partitions(_two_rdbs(tmp_path, "dh0"), capsys)
        assert first["driver"]["source"] == "rdb"
        assert second["driver"] is None
        assert second["driver_note"] == (
            "another RDB on this disk also has a partition named dh0, "
            "and mount --partition dh0 uses that one")

    def test_text_says_why_partitions_are_missing(
            self, user_driver_dir, tmp_path, capsys):
        from amifuse.fuse_fs import main

        main(["inspect", str(_two_rdbs(tmp_path, "DH0"))])
        out = capsys.readouterr().out
        assert out.endswith(
            "Filesystem driver per partition (without --driver):\n"
            "  DH0: DOS3/0x444f5303 (from RDB)\n"
            "  Note: multiple RDBs with colliding partition names ('DH0'); "
            "mounted first only\n")

    def test_json_failed_lookup_is_not_a_clash(
            self, user_driver_dir, tmp_path, capsys, monkeypatch):
        """A name mount's lookup cannot place gets the driver mount would
        use, not a clash note."""
        from amifuse import rdb_inspect

        image = _two_rdbs(tmp_path, "dh0")
        monkeypatch.setattr(rdb_inspect, "find_partition_mbr_index",
                            lambda *a: None)
        for part in self._partitions(image, capsys):
            assert part["driver"]["source"] == "rdb"
            assert "driver_note" not in part


def _parceiro(tmp_path):
    """A Parceiro disk: its own RDB, holding DH0, at block 2 beside an MBR
    that also lists two empty 0x76 entries."""
    from tests.unit.test_mbr_rdb_block_size import (
        _make_mbr, _make_rdb, _with_partition_list)

    rdb = _make_rdb(tmp_path, 512, "DH0")
    image = _make_mbr(tmp_path, [(200, bytes(160 * 512)),
                                 (400, bytes(160 * 512))])
    data = bytearray(image.read_bytes())
    data[2 * 512:3 * 512] = _with_partition_list(rdb, 512, 3)[:512]
    data[3 * 512:4 * 512] = rdb[512:1024]
    image.write_bytes(bytes(data))
    return image


class TestInspectParceiro:
    """The 0x76 entries beside a Parceiro disk's own RDB all reach that one
    RDB, so its names do not clash with themselves."""

    def test_json_gives_every_partition_a_driver(
            self, user_driver_dir, tmp_path, capsys):
        from amifuse.fuse_fs import main

        main(["inspect", "--json", str(_parceiro(tmp_path))])
        env = json.loads(capsys.readouterr().out)
        parts = [part for rdb in env.get("rdbs", [env])
                 for part in rdb["partitions"]]
        assert parts
        for part in parts:
            assert part["driver"] is not None
            assert "driver_note" not in part

    def test_text_prints_no_clash_note(self, user_driver_dir, tmp_path, capsys):
        from amifuse.fuse_fs import main

        main(["inspect", str(_parceiro(tmp_path))])
        out = capsys.readouterr().out
        assert "  DH0: " in out
        assert "Note:" not in out
