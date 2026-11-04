"""An unnamed on-disk entry cannot hide the named entries after it."""

import struct

import pytest

from amifuse.driver_resolver import BUNDLED_DRIVER
from amifuse.fuse_fs import AmigaFuseFS, HandlerBridge
from amifuse.rdb_inspect import detect_adf, detect_iso
from tests.fixtures.images import make_adf


@pytest.mark.integration
@pytest.mark.parametrize("damaged", [f"f{n}" for n in range(1, 9)])
def test_empty_name_keeps_other_files(tmp_path, damaged, capsys, fixture_root):
    driver = fixture_root / "drivers" / "FastFileSystem"
    if not driver.exists():
        pytest.skip("FastFileSystem 47.4 fixture required")
    files = {f"f{n}": b"hello" for n in range(1, 9)}
    image = make_adf(tmp_path / "empty-name.adf", files=files)
    data = bytearray(image.read_bytes())
    changed = 0
    for offset in range(0, len(data), 512):
        block = memoryview(data)[offset:offset + 512]
        if (struct.unpack_from(">i", block, 0)[0] != 2
                or struct.unpack_from(">i", block, 508)[0] != -3
                or bytes(block[433:433 + block[432]]) != damaged.encode()):
            continue
        block[432:463] = bytes(31)
        struct.pack_into(">I", block, 20, 0)
        checksum = -sum(struct.unpack(">128I", block)) & 0xffffffff
        struct.pack_into(">I", block, 20, checksum)
        changed += 1
    assert changed == 1
    image.write_bytes(data)
    bridge = HandlerBridge(image, driver, adf_info=detect_adf(image))
    try:
        assert {e["name"] for e in bridge.list_dir_path("/")} == files.keys() - {damaged}
        ops = AmigaFuseFS(bridge, icons=False)
        assert set(ops.readdir("/", None)) == {".", ".."} | (files.keys() - {damaged})
    finally:
        bridge.close()
    assert "empty name" in capsys.readouterr().err


@pytest.mark.integration
@pytest.mark.parametrize("driver_name,image_name,partition", [
    ("FastFileSystem", "ofs.adf", None),
    ("pfs3aio", "pfs.hdf", None),
    ("SmartFilesystem", "sfs.hdf", None),
    ("BFFSFilesystem", "netbsdamiga92.hdf", "netbsd-root"),
    ("CDFileSystem", None, None),
    ("ODFileSystem", None, None),
    (None, "generated", None),
])
def test_handler_ends_listing_with_a_failed_packet(
        tmp_path, fixture_root, driver_name, image_name, partition):
    if driver_name is None:
        image = make_adf(tmp_path / "normal.adf", files={"kept": b"data"})
        driver = BUNDLED_DRIVER
    elif image_name is None:
        from tests.integration.test_ls_recursive_iso import _iso, B
        image = _iso(tmp_path / "normal.iso", B)
        # CDFileSystem reads ahead beyond the last populated sector.
        data = bytearray(image.read_bytes())
        data.extend(bytes(64 * 2048 - len(data)))
        data[16 * 2048 + 80:16 * 2048 + 88] = (
            struct.pack("<I", 64) + struct.pack(">I", 64))
        image.write_bytes(data)
        driver = fixture_root / "drivers" / driver_name
    else:
        directory = "downloaded" if driver_name == "BFFSFilesystem" else "readonly"
        image = fixture_root / "fixtures" / directory / image_name
        driver = fixture_root / "drivers" / driver_name
    if not image.exists() or not driver.exists():
        pytest.skip(f"Missing fixture: {image} or {driver}")
    bridge = HandlerBridge(image, driver, partition=partition,
                           adf_info=detect_adf(image), iso_info=detect_iso(image))
    replies = []
    original = bridge._run_until_reply

    def record(packet, reason, *args, **kwargs):
        reply = original(packet, reason, *args, **kwargs)
        if reason == "examine_next":
            replies.append(reply)
        return reply

    bridge._run_until_reply = record
    try:
        assert bridge.list_dir_path("/")
        assert replies[-1] == (0, 232)  # ERROR_NO_MORE_ENTRIES
    finally:
        bridge.close()
