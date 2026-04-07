#!/usr/bin/env python3
"""Run timed AmiFuse filesystem smoke checks against canonical fixtures."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import statistics
import subprocess
import sys
import time
import traceback
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from fixture_paths import DEFAULT_HDF_URL, DOWNLOADED_DIR, DRIVERS_DIR, FIXTURE_ROOT
from fixture_paths import GENERATED_DIR, NETBSD_AMIGA_92_URL, ODFS_DRIVER
from fixture_paths import READONLY_DIR
from fixture_paths import ensure_downloaded_fixture

REPO_ROOT = Path(__file__).resolve().parent.parent
AMITOOLS_ROOT = REPO_ROOT / "amitools"
DEFAULT_TIMEOUT = 60.0
LOAD_FILE_COUNT = 256
LOAD_FILE_SIZE_BYTES = 256
LOAD_READ_COUNT = 1600
LOAD_READ_SIZE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Fixture:
    key: str
    fs_name: str
    image: Path
    driver: Path
    partition: Optional[str]
    mode: str
    image_kind: str
    image_size_mb: int
    expected_root: tuple[str, ...] = ()
    lookup_path: Optional[str] = None
    small_read_path: Optional[str] = None
    large_read_path: Optional[str] = None
    seed_image: Optional[Path] = None
    default_run: bool = True
    create_args: tuple[str, ...] = ()
    format_volname: Optional[str] = None
    cleanup_image: bool = False
    min_partition_start_byte: Optional[int] = None
    min_partition_size_byte: Optional[int] = None
    optional: bool = False
    download_url: Optional[str] = None
    seed_download_url: Optional[str] = None
    load_file_count: int = 0
    load_file_size_bytes: int = 0
    load_read_count: int = 0
    load_read_size_bytes: int = 0


FIXTURES: Dict[str, Fixture] = {
    "pfs3": Fixture(
        key="pfs3",
        fs_name="PFS3",
        image=READONLY_DIR / "pfs.hdf",
        driver=DRIVERS_DIR / "pfs3aio",
        partition="PDH0",
        mode="ro",
        image_kind="rdb-hdf",
        image_size_mb=8,
        expected_root=("Libs", "S", "foo.md", "plan.md"),
        lookup_path="/foo.md",
        small_read_path="/foo.md",
        large_read_path="/S/pci.db",
    ),
    "pfs3-rw": Fixture(
        key="pfs3-rw",
        fs_name="PFS3 rw",
        image=GENERATED_DIR / "pfs3_rw.hdf",
        driver=DRIVERS_DIR / "pfs3aio",
        partition="PDH0",
        mode="rw",
        image_kind="rdb-hdf",
        image_size_mb=8,
        seed_image=READONLY_DIR / "pfs.hdf",
        default_run=False,
    ),
    "pfs3-load": Fixture(
        key="pfs3-load",
        fs_name="PFS3 load",
        image=GENERATED_DIR / "pfs3_load.hdf",
        driver=DRIVERS_DIR / "pfs3aio",
        partition="PDH0",
        mode="load",
        image_kind="rdb-hdf",
        image_size_mb=8,
        seed_image=READONLY_DIR / "pfs.hdf",
        default_run=False,
        load_file_count=LOAD_FILE_COUNT,
        load_file_size_bytes=LOAD_FILE_SIZE_BYTES,
        load_read_count=LOAD_READ_COUNT,
        load_read_size_bytes=LOAD_READ_SIZE_BYTES,
    ),
    "pfs3-fmt": Fixture(
        key="pfs3-fmt",
        fs_name="PFS3 fmt",
        image=GENERATED_DIR / "pfs3_fmt.hdf",
        driver=DRIVERS_DIR / "pfs3aio",
        partition="PDH0",
        mode="fmt",
        image_kind="rdb-hdf",
        image_size_mb=8,
        default_run=False,
        create_args=(
            "create",
            "size=8Mi",
            "+",
            "init",
            "rdb_cyls=2",
            "+",
            "add",
            "size=6MiB",
            "name=PDH0",
            "fs=PFS3",
        ),
        format_volname="PFS3Fmt",
    ),
    "pfs3-4g": Fixture(
        key="pfs3-4g",
        fs_name="PFS3 >4G",
        image=GENERATED_DIR / "pfs3_4g.hdf",
        driver=DRIVERS_DIR / "pfs3aio",
        partition="LDH0",
        mode="fmt",
        image_kind="rdb-hdf",
        image_size_mb=5120,
        default_run=False,
        create_args=(
            "create",
            "chs=10400,16,63",
            "+",
            "init",
            "rdb_cyls=2",
            "+",
            "add",
            "start=9000",
            "size=64MiB",
            "name=LDH0",
            "fs=PFS3",
        ),
        format_volname="Large4G",
        cleanup_image=True,
        min_partition_start_byte=4 * 1024 * 1024 * 1024,
    ),
    "pfs3-part-4g": Fixture(
        key="pfs3-part-4g",
        fs_name="PFS3 partition >4G",
        image=GENERATED_DIR / "pfs3_part_4g.hdf",
        driver=DRIVERS_DIR / "pfs3aio",
        partition="XDH0",
        mode="fmt",
        image_kind="rdb-hdf",
        image_size_mb=6144,
        default_run=False,
        create_args=(
            "create",
            "chs=12483,16,63",
            "+",
            "init",
            "rdb_cyls=2",
            "+",
            "add",
            "size=8500",
            "name=XDH0",
            "fs=PFS3",
        ),
        format_volname="Span4G",
        cleanup_image=True,
        min_partition_size_byte=4 * 1024 * 1024 * 1024,
    ),
    "sfs": Fixture(
        key="sfs",
        fs_name="SFS",
        image=READONLY_DIR / "sfs.hdf",
        driver=DRIVERS_DIR / "SmartFilesystem",
        partition="SDH0",
        mode="ro",
        image_kind="rdb-hdf",
        image_size_mb=8,
    ),
    "sfs-rw": Fixture(
        key="sfs-rw",
        fs_name="SFS rw",
        image=GENERATED_DIR / "sfs_rw.hdf",
        driver=DRIVERS_DIR / "SmartFilesystem",
        partition="SDH0",
        mode="rw",
        image_kind="rdb-hdf",
        image_size_mb=8,
        seed_image=READONLY_DIR / "sfs.hdf",
        default_run=False,
    ),
    "sfs-load": Fixture(
        key="sfs-load",
        fs_name="SFS load",
        image=GENERATED_DIR / "sfs_load.hdf",
        driver=DRIVERS_DIR / "SmartFilesystem",
        partition="SDH0",
        mode="load",
        image_kind="rdb-hdf",
        image_size_mb=8,
        seed_image=READONLY_DIR / "sfs.hdf",
        default_run=False,
        load_file_count=LOAD_FILE_COUNT,
        load_file_size_bytes=LOAD_FILE_SIZE_BYTES,
        load_read_count=LOAD_READ_COUNT,
        load_read_size_bytes=LOAD_READ_SIZE_BYTES,
    ),
    "sfs-fmt": Fixture(
        key="sfs-fmt",
        fs_name="SFS fmt",
        image=GENERATED_DIR / "sfs_fmt.hdf",
        driver=DRIVERS_DIR / "SmartFilesystem",
        partition="SDH0",
        mode="fmt",
        image_kind="rdb-hdf",
        image_size_mb=8,
        default_run=False,
        create_args=(
            "create",
            "size=8Mi",
            "+",
            "init",
            "rdb_cyls=2",
            "+",
            "add",
            "size=6MiB",
            "name=SDH0",
            "fs=SFS0",
        ),
        format_volname="SFSFmt",
    ),
    "ffs": Fixture(
        key="ffs",
        fs_name="FFS",
        image=READONLY_DIR / "Default.hdf",
        driver=DRIVERS_DIR / "FastFileSystem",
        partition="QDH0",
        mode="ro",
        image_kind="rdb-hdf",
        image_size_mb=512,
        download_url=DEFAULT_HDF_URL,
    ),
    "ffs-rw": Fixture(
        key="ffs-rw",
        fs_name="FFS rw",
        image=GENERATED_DIR / "ffs_rw.hdf",
        driver=DRIVERS_DIR / "FastFileSystem",
        partition="QDH0",
        mode="rw",
        image_kind="rdb-hdf",
        image_size_mb=512,
        seed_image=READONLY_DIR / "Default.hdf",
        default_run=False,
        seed_download_url=DEFAULT_HDF_URL,
    ),
    "ffs-load": Fixture(
        key="ffs-load",
        fs_name="FFS load",
        image=GENERATED_DIR / "ffs_load.hdf",
        driver=DRIVERS_DIR / "FastFileSystem",
        partition="QDH0",
        mode="load",
        image_kind="rdb-hdf",
        image_size_mb=512,
        seed_image=READONLY_DIR / "Default.hdf",
        default_run=False,
        seed_download_url=DEFAULT_HDF_URL,
        load_file_count=LOAD_FILE_COUNT,
        load_file_size_bytes=LOAD_FILE_SIZE_BYTES,
        load_read_count=LOAD_READ_COUNT,
        load_read_size_bytes=LOAD_READ_SIZE_BYTES,
    ),
    "ffs-fmt": Fixture(
        key="ffs-fmt",
        fs_name="FFS fmt",
        image=GENERATED_DIR / "ffs_fmt.hdf",
        driver=DRIVERS_DIR / "FastFileSystem",
        partition="FDH0",
        mode="fmt",
        image_kind="rdb-hdf",
        image_size_mb=8,
        default_run=False,
        create_args=(
            "create",
            "size=8Mi",
            "+",
            "init",
            "rdb_cyls=2",
            "+",
            "add",
            "size=6MiB",
            "name=FDH0",
            "fs=ffs",
        ),
        format_volname="FFSFmt",
    ),
    "ofs": Fixture(
        key="ofs",
        fs_name="OFS",
        image=READONLY_DIR / "ofs.adf",
        driver=DRIVERS_DIR / "FastFileSystem",
        partition=None,
        mode="ro",
        image_kind="adf",
        image_size_mb=1,
        expected_root=("Docs", "OFS_README.txt"),
        lookup_path="/OFS_README.txt",
        small_read_path="/OFS_README.txt",
        large_read_path="/Docs/OFS_LARGE.bin",
    ),
    "bffs": Fixture(
        key="bffs",
        fs_name="BFFS",
        image=DOWNLOADED_DIR / "netbsdamiga92.hdf",
        driver=DRIVERS_DIR / "BFFSFilesystem",
        partition="netbsd-root",
        mode="ro",
        image_kind="rdb-hdf",
        image_size_mb=4095,
        expected_root=("bin", "etc", "usr", "var", "netbsd"),
        lookup_path="/bin/cat",
        small_read_path="/.cshrc",
        large_read_path="/netbsd",
        download_url=NETBSD_AMIGA_92_URL,
    ),
    "ofs-rw": Fixture(
        key="ofs-rw",
        fs_name="OFS rw",
        image=GENERATED_DIR / "ofs_rw.adf",
        driver=DRIVERS_DIR / "FastFileSystem",
        partition=None,
        mode="rw",
        image_kind="adf",
        image_size_mb=1,
        seed_image=READONLY_DIR / "ofs.adf",
        default_run=False,
    ),
    "ofs-fmt": Fixture(
        key="ofs-fmt",
        fs_name="OFS fmt",
        image=GENERATED_DIR / "ofs_fmt.hdf",
        driver=DRIVERS_DIR / "FastFileSystem",
        partition="ODH0",
        mode="fmt",
        image_kind="rdb-hdf",
        image_size_mb=8,
        default_run=False,
        create_args=(
            "create",
            "size=8Mi",
            "+",
            "init",
            "rdb_cyls=2",
            "+",
            "add",
            "size=6MiB",
            "name=ODH0",
            "fs=ofs",
        ),
        format_volname="OFSFmt",
    ),
    "cdfs": Fixture(
        key="cdfs",
        fs_name="CDFileSystem",
        image=READONLY_DIR / "AmigaOS3.2CD.iso",
        driver=DRIVERS_DIR / "CDFileSystem",
        partition=None,
        mode="ro",
        image_kind="iso",
        image_size_mb=74,
        expected_root=("C", "Devs", "Libs", "System"),
        lookup_path="/System",
        small_read_path="/CDVersion",
        large_read_path="/ADF/Backdrops3.2.adf",
    ),
    "odfs": Fixture(
        key="odfs",
        fs_name="ODFileSystem",
        image=READONLY_DIR / "AmigaOS3.2CD.iso",
        driver=ODFS_DRIVER,
        partition=None,
        mode="ro",
        image_kind="iso",
        image_size_mb=74,
        expected_root=("C", "Devs", "Libs", "System"),
        lookup_path="/System",
        small_read_path="/CDVersion",
        large_read_path="/CDVersion",
        default_run=False,
        optional=True,
    ),
}


def _ensure_import_path():
    if str(AMITOOLS_ROOT) not in sys.path:
        sys.path.insert(0, str(AMITOOLS_ROOT))
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


def _install_fake_fuse():
    if "fuse" in sys.modules:
        return
    fake_fuse = types.ModuleType("fuse")

    class _DummyOperations:
        pass

    class _DummyLoggingMixIn:
        pass

    class _DummyFuseError(RuntimeError):
        pass

    fake_fuse.FUSE = object
    fake_fuse.FuseOSError = _DummyFuseError
    fake_fuse.LoggingMixIn = _DummyLoggingMixIn
    fake_fuse.Operations = _DummyOperations
    sys.modules["fuse"] = fake_fuse


def _load_runtime():
    _ensure_import_path()
    _install_fake_fuse()
    logging.getLogger().setLevel(logging.CRITICAL)
    from amifuse.fuse_fs import HandlerBridge, format_volume
    from amifuse.rdb_inspect import detect_adf, detect_iso, open_rdisk

    return HandlerBridge, format_volume, detect_adf, detect_iso, open_rdisk


def _timed(callable_obj, *args, **kwargs):
    start = time.perf_counter()
    result = callable_obj(*args, **kwargs)
    elapsed = time.perf_counter() - start
    return elapsed, result


def _inspect_fixture(fixture: Fixture, detect_adf, detect_iso, open_rdisk):
    info: Dict[str, object] = {
        "kind": None,
        "partition_found": fixture.partition is None,
    }
    adf_info = detect_adf(fixture.image)
    iso_info = None
    if adf_info is not None:
        info.update(
            {
                "kind": "adf",
                "dos_type": f"0x{adf_info.dos_type:08x}",
                "total_blocks": adf_info.total_blocks,
            }
        )
        return info, adf_info, None

    iso_info = detect_iso(fixture.image)
    if iso_info is not None:
        info.update(
            {
                "kind": "iso",
                "volume_id": iso_info.volume_id.rstrip("\x00"),
                "total_blocks": iso_info.total_blocks,
            }
        )
        return info, None, iso_info

    blkdev = None
    rdisk = None
    try:
        blkdev, rdisk, mbr_ctx = open_rdisk(fixture.image)
        parts = []
        partition_found = fixture.partition is None
        for part in rdisk.parts:
            part_name = str(part.part_blk.drv_name)
            dos_env = part.part_blk.dos_env
            cyl_blocks = dos_env.surfaces * dos_env.blk_per_trk
            start_block = dos_env.low_cyl * cyl_blocks
            parts.append(
                {
                    "name": part_name,
                    "dos_type": f"0x{part.part_blk.dos_env.dos_type:08x}",
                    "start_block": start_block,
                    "start_byte": start_block * blkdev.block_bytes,
                    "size_blocks": (dos_env.high_cyl - dos_env.low_cyl + 1) * cyl_blocks,
                    "size_byte": (dos_env.high_cyl - dos_env.low_cyl + 1)
                    * cyl_blocks
                    * blkdev.block_bytes,
                }
            )
            if part_name == fixture.partition:
                partition_found = True
        info.update(
            {
                "kind": "rdb",
                "partition_found": partition_found,
                "partitions": parts,
                "driver_count": len(rdisk.fs),
                "warning_count": len(getattr(rdisk, "rdb_warnings", [])),
                "mbr_scheme": getattr(mbr_ctx, "scheme", None) if mbr_ctx else None,
            }
        )
        return info, None, None
    finally:
        if rdisk is not None:
            rdisk.close()
        if blkdev is not None:
            blkdev.close()


def _pick_lookup_path(fixture: Fixture, root_entries: List[Dict[str, object]]) -> str:
    if fixture.lookup_path:
        return fixture.lookup_path
    for entry in sorted(root_entries, key=lambda item: item["name"]):
        if entry["dir_type"] > 0:
            return "/" + entry["name"]
        return "/" + entry["name"]
    return "/"


def _find_sample_files(bridge, limit_dirs: int = 8):
    root_entries = bridge.list_dir_path("/")
    preferred_small = None
    fallback_small = None
    preferred_large = None
    fallback_large = None
    preferred_large_size = -1
    fallback_large_size = -1

    def consider(path: str, entry: Dict[str, object]):
        nonlocal preferred_small, fallback_small
        nonlocal preferred_large, fallback_large
        nonlocal preferred_large_size, fallback_large_size
        size = int(entry.get("size", 0))
        if fallback_small is None:
            fallback_small = path
        if size > fallback_large_size:
            fallback_large_size = size
            fallback_large = path
        if path.lower().endswith(".info"):
            return
        if preferred_small is None:
            preferred_small = path
        if size > preferred_large_size:
            preferred_large_size = size
            preferred_large = path

    dirs_to_scan = []
    for entry in sorted(root_entries, key=lambda item: item["name"]):
        path = "/" + entry["name"]
        if entry["dir_type"] > 0:
            dirs_to_scan.append(path)
        else:
            consider(path, entry)

    for dir_path in dirs_to_scan[:limit_dirs]:
        entries = bridge.list_dir_path(dir_path)
        for entry in sorted(entries, key=lambda item: item["name"]):
            if entry["dir_type"] <= 0:
                consider(f"{dir_path}/{entry['name']}", entry)

    return {
        "small": preferred_small or fallback_small,
        "large": preferred_large or fallback_large or preferred_small or fallback_small,
    }


def _shutdown_bridge(bridge):
    close = getattr(bridge, "close", None)
    if close is not None:
        close()


def _split_parent(path: str) -> tuple[str, str]:
    parts = [part for part in path.split("/") if part]
    if not parts:
        raise ValueError(f"invalid path: {path!r}")
    name = parts[-1]
    if len(parts) == 1:
        return "/", name
    return "/" + "/".join(parts[:-1]), name


def _locate_dir_lock(bridge, dir_path: str):
    lock_bptr, res2, locks = bridge.locate_path(dir_path)
    if dir_path == "/" and lock_bptr == 0:
        lock_bptr, res2 = bridge.locate(0, "")
        locks = [lock_bptr] if lock_bptr else []
    return lock_bptr, res2, locks


def _create_dir_path(bridge, path: str):
    parent_path, name = _split_parent(path)
    lock_bptr, res2, locks = _locate_dir_lock(bridge, parent_path)
    try:
        if lock_bptr == 0 and parent_path != "/":
            raise RuntimeError(f"parent dir not found for mkdir {path}: res2={res2}")
        new_lock, io_err = bridge.create_dir(lock_bptr, name)
        if new_lock == 0:
            raise RuntimeError(f"mkdir failed for {path}: res2={io_err}")
        bridge.free_lock(new_lock)
    finally:
        for lock in reversed(locks):
            bridge.free_lock(lock)


def _delete_path(bridge, path: str):
    parent_path, name = _split_parent(path)
    lock_bptr, res2, locks = _locate_dir_lock(bridge, parent_path)
    try:
        if lock_bptr == 0 and parent_path != "/":
            raise RuntimeError(f"parent dir not found for delete {path}: res2={res2}")
        deleted, io_err = bridge.delete_object(lock_bptr, name)
        if deleted == 0:
            raise RuntimeError(f"delete failed for {path}: res2={io_err}")
    finally:
        for lock in reversed(locks):
            bridge.free_lock(lock)


def _rename_path(bridge, old_path: str, new_path: str):
    src_parent, src_name = _split_parent(old_path)
    dst_parent, dst_name = _split_parent(new_path)
    src_lock, src_res2, src_locks = _locate_dir_lock(bridge, src_parent)
    dst_lock, dst_res2, dst_locks = _locate_dir_lock(bridge, dst_parent)
    try:
        if src_lock == 0 and src_parent != "/":
            raise RuntimeError(
                f"source dir not found for rename {old_path}: res2={src_res2}"
            )
        if dst_lock == 0 and dst_parent != "/":
            raise RuntimeError(
                f"dest dir not found for rename {new_path}: res2={dst_res2}"
            )
        renamed, io_err = bridge.rename_object(src_lock, src_name, dst_lock, dst_name)
        if renamed == 0:
            raise RuntimeError(f"rename failed {old_path} -> {new_path}: res2={io_err}")
    finally:
        for lock in reversed(src_locks):
            bridge.free_lock(lock)
        for lock in reversed(dst_locks):
            if lock not in src_locks:
                bridge.free_lock(lock)


def _write_file_path(bridge, path: str, data: bytes):
    opened = bridge.open_file(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    if opened is None:
        raise RuntimeError(f"open_file failed for write path {path}")
    fh_addr, parent_lock = opened
    try:
        written = bridge.write_handle(fh_addr, data)
        if written != len(data):
            raise RuntimeError(f"short write for {path}: {written} != {len(data)}")
    finally:
        bridge.close_file(fh_addr)
        if parent_lock:
            bridge.free_lock(parent_lock)


def _prepare_rw_image(fixture: Fixture):
    if fixture.seed_image is None:
        return
    fixture.image.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture.seed_image, fixture.image)


def _run_amitools_tool(module: str, *args: str):
    proc = subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=AMITOOLS_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{module} failed with code {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc.stdout


def _prepare_format_image(fixture: Fixture):
    fixture.image.parent.mkdir(parents=True, exist_ok=True)
    if fixture.image.exists():
        fixture.image.unlink()
    if not fixture.create_args:
        raise RuntimeError(f"no create_args configured for {fixture.key}")
    _run_amitools_tool("amitools.tools.rdbtool", str(fixture.image), *fixture.create_args)


def _cleanup_generated_image(fixture: Fixture):
    if fixture.cleanup_image and fixture.image.exists():
        fixture.image.unlink()


def _pattern_bytes(size: int, seed: int = 0) -> bytes:
    return bytes(((idx + seed) % 251 for idx in range(size)))


def _exercise_rw_session(bridge):
    rw_dir = "/AmiFuseRW"
    created_path = f"{rw_dir}/hello.txt"
    renamed_path = f"{rw_dir}/hello-renamed.txt"
    payload = b"AmiFuse writable smoke\n"
    payload += bytes((ord("A") + (idx % 26) for idx in range(8192)))

    list_root_s, root_entries = _timed(bridge.list_dir_path, "/")
    root_names = [entry["name"] for entry in root_entries]
    mkdir_s, _ = _timed(_create_dir_path, bridge, rw_dir)
    create_s, opened = _timed(
        bridge.open_file, created_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    )
    if opened is None:
        raise RuntimeError(f"open_file failed for create path {created_path}")
    fh_addr, parent_lock = opened
    try:
        write_s, written = _timed(bridge.write_handle, fh_addr, payload)
        if written != len(payload):
            raise RuntimeError(
                f"short write for {created_path}: {written} != {len(payload)}"
            )
    finally:
        bridge.close_file(fh_addr)
        if parent_lock:
            bridge.free_lock(parent_lock)
    rename_s, _ = _timed(_rename_path, bridge, created_path, renamed_path)
    flush_s, _ = _timed(bridge.flush_volume)
    return {
        "root_entries": root_entries,
        "root_names": root_names,
        "list_root_s": list_root_s,
        "mkdir_s": mkdir_s,
        "create_s": create_s,
        "write_s": write_s,
        "rename_s": rename_s,
        "flush_s": flush_s,
        "renamed_path": renamed_path,
        "payload": payload,
    }


def _exercise_load_session(bridge, fixture: Fixture):
    load_dir = "/AmiFuseLoad"
    large_path = f"{load_dir}/bulk-read.bin"
    small_payload = _pattern_bytes(fixture.load_file_size_bytes, seed=17)
    large_payload = _pattern_bytes(fixture.load_read_size_bytes, seed=83)

    list_root_s, root_entries = _timed(bridge.list_dir_path, "/")
    root_names = [entry["name"] for entry in root_entries]
    mkdir_s, _ = _timed(_create_dir_path, bridge, load_dir)
    create_large_s, _ = _timed(_write_file_path, bridge, large_path, large_payload)

    create_many_start = time.perf_counter()
    for idx in range(fixture.load_file_count):
        path = f"{load_dir}/f{idx:04d}.bin"
        _write_file_path(bridge, path, small_payload)
    create_many_s = time.perf_counter() - create_many_start

    list_load_dir_s, load_entries = _timed(bridge.list_dir_path, load_dir)
    expected_entries = fixture.load_file_count + 1
    if len(load_entries) != expected_entries:
        raise RuntimeError(
            f"load dir entry count mismatch: {len(load_entries)} != {expected_entries}"
        )

    read_many_start = time.perf_counter()
    for _ in range(fixture.load_read_count):
        data = bridge.read_file(large_path, fixture.load_read_size_bytes, 0)
        if len(data) != len(large_payload):
            raise RuntimeError(
                f"short repeated read: {len(data)} != {len(large_payload)}"
            )
        if data[:64] != large_payload[:64] or data[-64:] != large_payload[-64:]:
            raise RuntimeError(f"repeated read content mismatch for {large_path}")
    read_many_s = time.perf_counter() - read_many_start

    flush_s, _ = _timed(bridge.flush_volume)
    steady_s = create_large_s + create_many_s + list_load_dir_s + read_many_s + flush_s

    return {
        "root_entries": root_entries,
        "root_names": root_names,
        "list_root_s": list_root_s,
        "mkdir_s": mkdir_s,
        "create_large_s": create_large_s,
        "create_many_s": create_many_s,
        "list_load_dir_s": list_load_dir_s,
        "read_many_s": read_many_s,
        "flush_s": flush_s,
        "steady_s": steady_s,
        "load_dir": load_dir,
        "large_path": large_path,
        "file_count": fixture.load_file_count,
        "file_size_bytes": fixture.load_file_size_bytes,
        "read_iterations": fixture.load_read_count,
        "read_size_bytes": fixture.load_read_size_bytes,
        "read_total_bytes": fixture.load_read_count * fixture.load_read_size_bytes,
    }


def _verify_rw_session(
    HandlerBridge,
    fixture: Fixture,
    renamed_path: str,
    payload: bytes,
    adf_info=None,
    iso_info=None,
):
    remount_s, verify_bridge = _timed(
        HandlerBridge,
        fixture.image,
        fixture.driver,
        partition=fixture.partition,
        read_only=False,
        adf_info=adf_info,
        iso_info=iso_info,
    )
    try:
        verify_stat_s, verify_stat = _timed(verify_bridge.stat_path, renamed_path)
        if verify_stat is None:
            raise RuntimeError(f"remount stat failed for {renamed_path}")
        if int(verify_stat.get("size", -1)) != len(payload):
            raise RuntimeError(
                f"remount size mismatch for {renamed_path}: {verify_stat.get('size')}"
            )
        verify_read_s, verify_data = _timed(
            verify_bridge.read_file, renamed_path, len(payload), 0
        )
        if verify_data != payload:
            raise RuntimeError(f"remount read mismatch for {renamed_path}")
        delete_s, _ = _timed(_delete_path, verify_bridge, renamed_path)
        _, _ = _timed(_delete_path, verify_bridge, "/AmiFuseRW")
        cleanup_flush_s, _ = _timed(verify_bridge.flush_volume)
        return {
            "remount_s": remount_s,
            "verify_stat_s": verify_stat_s,
            "verify_read_s": verify_read_s,
            "delete_s": delete_s,
            "cleanup_flush_s": cleanup_flush_s,
        }
    finally:
        _shutdown_bridge(verify_bridge)


def _run_ro_fixture(fixture: Fixture, HandlerBridge, adf_info, iso_info, inspect_s, inspect_meta):
    init_s, bridge = _timed(
        HandlerBridge,
        fixture.image,
        fixture.driver,
        partition=fixture.partition,
        adf_info=adf_info,
        iso_info=iso_info,
    )

    try:
        list_root_s, root_entries = _timed(bridge.list_dir_path, "/")
        root_names = [entry["name"] for entry in root_entries]
        missing_root = [
            name for name in fixture.expected_root if name not in set(root_names)
        ]
        if missing_root:
            raise RuntimeError(f"missing root entries: {', '.join(missing_root)}")

        lookup_path = _pick_lookup_path(fixture, root_entries)
        stat_s, lookup_info = _timed(bridge.stat_path, lookup_path)
        if lookup_info is None:
            raise RuntimeError(f"stat failed for {lookup_path}")

        samples = _find_sample_files(bridge)
        small_path = fixture.small_read_path or samples["small"]
        large_path = fixture.large_read_path or samples["large"]

        small_read_s = 0.0
        small_read_bytes = 0
        if small_path:
            small_read_s, small_data = _timed(bridge.read_file, small_path, 4096, 0)
            small_read_bytes = len(small_data)

        large_read_s = 0.0
        large_read_bytes = 0
        if large_path:
            large_read_s, large_data = _timed(bridge.read_file, large_path, 65536, 0)
            large_read_bytes = len(large_data)

        flush_s, _ = _timed(bridge.flush_volume)

        total_s = (
            inspect_s
            + init_s
            + list_root_s
            + stat_s
            + small_read_s
            + large_read_s
            + flush_s
        )
        return {
            "fixture": fixture.key,
            "fs_name": fixture.fs_name,
            "image": str(fixture.image),
            "driver": str(fixture.driver),
            "partition": fixture.partition,
            "mode": fixture.mode,
            "image_kind": fixture.image_kind,
            "image_size_mb": fixture.image_size_mb,
            "status": "ok",
            "inspect": inspect_meta,
            "root_count": len(root_entries),
            "root_names": root_names,
            "lookup_path": lookup_path,
            "small_read_path": small_path,
            "large_read_path": large_path,
            "create_image_s": 0.0,
            "format_s": 0.0,
            "inspect_s": inspect_s,
            "init_s": init_s,
            "list_root_s": list_root_s,
            "stat_s": stat_s,
            "small_read_s": small_read_s,
            "large_read_s": large_read_s,
            "mkdir_s": 0.0,
            "create_s": 0.0,
            "write_s": 0.0,
            "rename_s": 0.0,
            "flush_s": flush_s,
            "remount_s": 0.0,
            "verify_stat_s": 0.0,
            "verify_read_s": 0.0,
            "delete_s": 0.0,
            "cleanup_flush_s": 0.0,
            "create_large_s": 0.0,
            "create_many_s": 0.0,
            "list_load_dir_s": 0.0,
            "read_many_s": 0.0,
            "steady_s": 0.0,
            "total_s": total_s,
            "small_read_bytes": small_read_bytes,
            "large_read_bytes": large_read_bytes,
            "load_file_count": 0,
            "load_file_size_bytes": 0,
            "load_read_count": 0,
            "load_read_size_bytes": 0,
            "load_read_total_bytes": 0,
        }
    finally:
        _shutdown_bridge(bridge)


def _run_rw_fixture(fixture: Fixture, HandlerBridge, adf_info, iso_info, inspect_s, inspect_meta):
    init_s, bridge = _timed(
        HandlerBridge,
        fixture.image,
        fixture.driver,
        partition=fixture.partition,
        read_only=False,
        adf_info=adf_info,
        iso_info=iso_info,
    )

    try:
        session = _exercise_rw_session(bridge)
    finally:
        _shutdown_bridge(bridge)
    verify = _verify_rw_session(
        HandlerBridge,
        fixture,
        session["renamed_path"],
        session["payload"],
        adf_info=adf_info,
        iso_info=iso_info,
    )
    total_s = (
        inspect_s
        + init_s
        + session["list_root_s"]
        + session["mkdir_s"]
        + session["create_s"]
        + session["write_s"]
        + session["rename_s"]
        + session["flush_s"]
        + verify["remount_s"]
        + verify["verify_stat_s"]
        + verify["verify_read_s"]
        + verify["delete_s"]
        + verify["cleanup_flush_s"]
    )
    return {
        "fixture": fixture.key,
        "fs_name": fixture.fs_name,
        "image": str(fixture.image),
        "driver": str(fixture.driver),
        "partition": fixture.partition,
        "mode": fixture.mode,
        "image_kind": fixture.image_kind,
        "image_size_mb": fixture.image_size_mb,
        "status": "ok",
        "inspect": inspect_meta,
        "root_count": len(session["root_entries"]),
        "root_names": session["root_names"],
        "lookup_path": session["renamed_path"],
        "small_read_path": session["renamed_path"],
        "large_read_path": session["renamed_path"],
        "create_image_s": 0.0,
        "format_s": 0.0,
        "inspect_s": inspect_s,
        "init_s": init_s,
        "list_root_s": session["list_root_s"],
        "stat_s": 0.0,
        "small_read_s": 0.0,
        "large_read_s": 0.0,
        "mkdir_s": session["mkdir_s"],
        "create_s": session["create_s"],
        "write_s": session["write_s"],
        "rename_s": session["rename_s"],
        "flush_s": session["flush_s"],
        "remount_s": verify["remount_s"],
        "verify_stat_s": verify["verify_stat_s"],
        "verify_read_s": verify["verify_read_s"],
        "delete_s": verify["delete_s"],
        "cleanup_flush_s": verify["cleanup_flush_s"],
        "create_large_s": 0.0,
        "create_many_s": 0.0,
        "list_load_dir_s": 0.0,
        "read_many_s": 0.0,
        "steady_s": 0.0,
        "total_s": total_s,
        "small_read_bytes": len(session["payload"]),
        "large_read_bytes": len(session["payload"]),
        "load_file_count": 0,
        "load_file_size_bytes": 0,
        "load_read_count": 0,
        "load_read_size_bytes": 0,
        "load_read_total_bytes": 0,
    }


def _run_load_fixture(
    fixture: Fixture,
    HandlerBridge,
    adf_info,
    iso_info,
    inspect_s,
    inspect_meta,
):
    init_s, bridge = _timed(
        HandlerBridge,
        fixture.image,
        fixture.driver,
        partition=fixture.partition,
        read_only=False,
        adf_info=adf_info,
        iso_info=iso_info,
    )
    try:
        session = _exercise_load_session(bridge, fixture)
    finally:
        _shutdown_bridge(bridge)

    total_s = (
        inspect_s
        + init_s
        + session["list_root_s"]
        + session["mkdir_s"]
        + session["steady_s"]
    )
    return {
        "fixture": fixture.key,
        "fs_name": fixture.fs_name,
        "image": str(fixture.image),
        "driver": str(fixture.driver),
        "partition": fixture.partition,
        "mode": fixture.mode,
        "image_kind": fixture.image_kind,
        "image_size_mb": fixture.image_size_mb,
        "status": "ok",
        "inspect": inspect_meta,
        "root_count": len(session["root_entries"]),
        "root_names": session["root_names"],
        "lookup_path": session["large_path"],
        "small_read_path": session["large_path"],
        "large_read_path": session["large_path"],
        "create_image_s": 0.0,
        "format_s": 0.0,
        "inspect_s": inspect_s,
        "init_s": init_s,
        "list_root_s": session["list_root_s"],
        "stat_s": 0.0,
        "small_read_s": 0.0,
        "large_read_s": 0.0,
        "mkdir_s": session["mkdir_s"],
        "create_s": 0.0,
        "write_s": 0.0,
        "rename_s": 0.0,
        "flush_s": session["flush_s"],
        "remount_s": 0.0,
        "verify_stat_s": 0.0,
        "verify_read_s": 0.0,
        "delete_s": 0.0,
        "cleanup_flush_s": 0.0,
        "create_large_s": session["create_large_s"],
        "create_many_s": session["create_many_s"],
        "list_load_dir_s": session["list_load_dir_s"],
        "read_many_s": session["read_many_s"],
        "steady_s": session["steady_s"],
        "total_s": total_s,
        "small_read_bytes": session["read_size_bytes"],
        "large_read_bytes": session["read_size_bytes"],
        "load_file_count": session["file_count"],
        "load_file_size_bytes": session["file_size_bytes"],
        "load_read_count": session["read_iterations"],
        "load_read_size_bytes": session["read_size_bytes"],
        "load_read_total_bytes": session["read_total_bytes"],
    }


def _run_fmt_fixture(
    fixture: Fixture,
    HandlerBridge,
    format_volume,
    detect_adf,
    detect_iso,
    open_rdisk,
):
    try:
        create_image_s, _ = _timed(_prepare_format_image, fixture)
        inspect_s, inspect_info = _timed(
            _inspect_fixture, fixture, detect_adf, detect_iso, open_rdisk
        )
        inspect_meta2, adf_info, iso_info = inspect_info
        if not inspect_meta2.get("partition_found", True):
            raise RuntimeError(
                f"expected partition {fixture.partition!r} not found in inspect data"
            )
        if fixture.min_partition_start_byte is not None:
            parts = inspect_meta2.get("partitions", [])
            part_info = next(
                (part for part in parts if part.get("name") == fixture.partition),
                None,
            )
            if part_info is None:
                raise RuntimeError(
                    f"missing inspect partition data for {fixture.partition!r}"
                )
            start_byte = int(part_info.get("start_byte", 0))
            if start_byte < fixture.min_partition_start_byte:
                raise RuntimeError(
                    f"partition {fixture.partition} starts at {start_byte}, "
                    f"expected >= {fixture.min_partition_start_byte}"
                )
        if fixture.min_partition_size_byte is not None:
            parts = inspect_meta2.get("partitions", [])
            part_info = next(
                (part for part in parts if part.get("name") == fixture.partition),
                None,
            )
            if part_info is None:
                raise RuntimeError(
                    f"missing inspect partition data for {fixture.partition!r}"
                )
            size_byte = int(part_info.get("size_byte", 0))
            if size_byte < fixture.min_partition_size_byte:
                raise RuntimeError(
                    f"partition {fixture.partition} size is {size_byte}, "
                    f"expected >= {fixture.min_partition_size_byte}"
                )
        format_s, _ = _timed(
            format_volume,
            fixture.image,
            fixture.driver,
            None,
            fixture.partition,
            fixture.format_volname or "FmtVol",
            False,
        )
        init_s, bridge = _timed(
            HandlerBridge,
            fixture.image,
            fixture.driver,
            partition=fixture.partition,
            read_only=False,
            adf_info=adf_info,
            iso_info=iso_info,
        )
        try:
            session = _exercise_rw_session(bridge)
        finally:
            _shutdown_bridge(bridge)
        verify = _verify_rw_session(
            HandlerBridge, fixture, session["renamed_path"], session["payload"]
        )
        total_s = (
            create_image_s
            + inspect_s
            + format_s
            + init_s
            + session["list_root_s"]
            + session["mkdir_s"]
            + session["create_s"]
            + session["write_s"]
            + session["rename_s"]
            + session["flush_s"]
            + verify["remount_s"]
            + verify["verify_stat_s"]
            + verify["verify_read_s"]
            + verify["delete_s"]
            + verify["cleanup_flush_s"]
        )
        return {
            "fixture": fixture.key,
            "fs_name": fixture.fs_name,
            "image": str(fixture.image),
            "driver": str(fixture.driver),
            "partition": fixture.partition,
            "mode": fixture.mode,
            "image_kind": fixture.image_kind,
            "image_size_mb": fixture.image_size_mb,
            "status": "ok",
            "inspect": inspect_meta2,
            "root_count": len(session["root_entries"]),
            "root_names": session["root_names"],
            "lookup_path": session["renamed_path"],
            "small_read_path": session["renamed_path"],
            "large_read_path": session["renamed_path"],
            "create_image_s": create_image_s,
            "format_s": format_s,
            "inspect_s": inspect_s,
            "init_s": init_s,
            "list_root_s": session["list_root_s"],
            "stat_s": 0.0,
            "small_read_s": 0.0,
            "large_read_s": 0.0,
            "mkdir_s": session["mkdir_s"],
            "create_s": session["create_s"],
            "write_s": session["write_s"],
            "rename_s": session["rename_s"],
            "flush_s": session["flush_s"],
            "remount_s": verify["remount_s"],
            "verify_stat_s": verify["verify_stat_s"],
            "verify_read_s": verify["verify_read_s"],
            "delete_s": verify["delete_s"],
            "cleanup_flush_s": verify["cleanup_flush_s"],
            "create_large_s": 0.0,
            "create_many_s": 0.0,
            "list_load_dir_s": 0.0,
            "read_many_s": 0.0,
            "steady_s": 0.0,
            "total_s": total_s,
            "small_read_bytes": len(session["payload"]),
            "large_read_bytes": len(session["payload"]),
            "load_file_count": 0,
            "load_file_size_bytes": 0,
            "load_read_count": 0,
            "load_read_size_bytes": 0,
            "load_read_total_bytes": 0,
        }
    finally:
        _cleanup_generated_image(fixture)


def _run_fixture_worker(fixture_key: str):
    fixture = FIXTURES[fixture_key]
    if fixture.download_url:
        ensure_downloaded_fixture(
            fixture.image, fixture.download_url, fixture.fs_name
        )
    if fixture.seed_image is not None and fixture.seed_download_url:
        ensure_downloaded_fixture(
            fixture.seed_image, fixture.seed_download_url, f"{fixture.fs_name} seed"
        )
    missing = []
    if fixture.mode == "ro":
        if not fixture.image.exists():
            missing.append(f"image {fixture.image}")
    elif fixture.mode in ("rw", "load"):
        if fixture.seed_image is None:
            missing.append(f"seed image missing for {fixture.key}")
        elif not fixture.seed_image.exists():
            missing.append(f"seed image {fixture.seed_image}")
    elif fixture.mode == "fmt" and not fixture.create_args:
        missing.append(f"create args missing for {fixture.key}")
    if fixture.driver is not None and not fixture.driver.exists():
        missing.append(f"driver {fixture.driver}")
    if missing:
        if fixture.optional:
            return {
                "fixture": fixture.key,
                "fs_name": fixture.fs_name,
                "mode": fixture.mode,
                "status": "skip",
                "error": ", ".join(missing),
            }
        raise RuntimeError(", ".join(missing))
    HandlerBridge, format_volume, detect_adf, detect_iso, open_rdisk = _load_runtime()
    if fixture.mode == "rw":
        _prepare_rw_image(fixture)
    if fixture.mode == "load":
        _prepare_rw_image(fixture)
    if fixture.mode == "fmt":
        return _run_fmt_fixture(
            fixture,
            HandlerBridge,
            format_volume,
            detect_adf,
            detect_iso,
            open_rdisk,
        )

    inspect_s, inspect_info = _timed(
        _inspect_fixture, fixture, detect_adf, detect_iso, open_rdisk
    )
    inspect_meta, adf_info, iso_info = inspect_info
    if not inspect_meta.get("partition_found", True):
        raise RuntimeError(
            f"expected partition {fixture.partition!r} not found in inspect data"
        )
    if fixture.mode == "rw":
        return _run_rw_fixture(
            fixture, HandlerBridge, adf_info, iso_info, inspect_s, inspect_meta
        )
    if fixture.mode == "load":
        return _run_load_fixture(
            fixture, HandlerBridge, adf_info, iso_info, inspect_s, inspect_meta
        )
    return _run_ro_fixture(
        fixture, HandlerBridge, adf_info, iso_info, inspect_s, inspect_meta
    )


def _worker_main(args):
    try:
        result = _run_fixture_worker(args.worker)
    except SystemExit as exc:  # pragma: no cover - exercised via subprocess wrapper
        result = {
            "fixture": args.worker,
            "status": "error",
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    except Exception as exc:  # pragma: no cover - exercised via subprocess wrapper
        result = {
            "fixture": args.worker,
            "status": "error",
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    print(json.dumps(result, sort_keys=True))
    return 0


def _run_fixture_subprocess(script_path: Path, fixture: Fixture, timeout_s: float):
    proc = subprocess.run(
        [sys.executable, str(script_path), "--worker", fixture.key],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(
            f"{fixture.key}: worker produced no JSON output\nstderr:\n{proc.stderr}"
        )
    result = json.loads(lines[-1])
    if proc.returncode != 0:
        result.setdefault("status", "error")
        result.setdefault("error", f"worker exit code {proc.returncode}")
    if proc.stderr.strip():
        result["stderr"] = proc.stderr.strip()
        if (
            result.get("status") == "ok"
            and "[amifuse] FATAL: Handler crashed" in proc.stderr
        ):
            result["status"] = "error"
            result["error"] = "handler crashed"
    return result


TIMING_KEYS = (
    "create_image_s",
    "format_s",
    "inspect_s",
    "init_s",
    "list_root_s",
    "stat_s",
    "small_read_s",
    "large_read_s",
    "mkdir_s",
    "create_s",
    "write_s",
    "rename_s",
    "flush_s",
    "remount_s",
    "verify_stat_s",
    "verify_read_s",
    "delete_s",
    "cleanup_flush_s",
    "create_large_s",
    "create_many_s",
    "list_load_dir_s",
    "read_many_s",
    "steady_s",
    "total_s",
)


def _aggregate_fixture_runs(
    fixture: Fixture, run_results: List[Dict[str, object]]
) -> Dict[str, object]:
    skips = [result for result in run_results if result.get("status") == "skip"]
    if skips:
        first = skips[0]
        return {
            "fixture": fixture.key,
            "fs_name": fixture.fs_name,
            "mode": fixture.mode,
            "status": "skip",
            "error": first.get("error", "fixture unavailable"),
            "runs": len(run_results),
            "samples": run_results,
        }
    errors = [result for result in run_results if result.get("status") != "ok"]
    if errors:
        first = errors[0]
        return {
            "fixture": fixture.key,
            "fs_name": fixture.fs_name,
            "mode": fixture.mode,
            "status": first.get("status", "error"),
            "error": first.get("error", "unknown error"),
            "runs": len(run_results),
            "samples": run_results,
        }

    summary: Dict[str, object] = {
        "fixture": fixture.key,
        "fs_name": fixture.fs_name,
        "status": "ok",
        "runs": len(run_results),
        "samples": run_results,
    }

    for key in TIMING_KEYS:
        values = [float(result[key]) for result in run_results]
        summary[f"{key}_min"] = min(values)
        summary[f"{key}_median"] = statistics.median(values)
        summary[f"{key}_max"] = max(values)

    first = run_results[0]
    for key in (
        "mode",
        "partition",
        "image_kind",
        "image_size_mb",
        "inspect",
        "root_count",
        "root_names",
        "lookup_path",
        "small_read_path",
        "large_read_path",
        "small_read_bytes",
        "large_read_bytes",
        "load_file_count",
        "load_file_size_bytes",
        "load_read_count",
        "load_read_size_bytes",
        "load_read_total_bytes",
    ):
        summary[key] = first.get(key)

    return summary


def _format_seconds(value: float) -> str:
    return f"{value:.3f}s"


def _format_total_range(result: Dict[str, object]) -> str:
    return " / ".join(
        [
            _format_seconds(float(result["total_s_min"])),
            _format_seconds(float(result["total_s_median"])),
            _format_seconds(float(result["total_s_max"])),
        ]
    )


def _render_markdown(results: List[Dict[str, object]]) -> str:
    ro_results = [result for result in results if result.get("mode") == "ro"]
    rw_results = [result for result in results if result.get("mode") == "rw"]
    fmt_results = [result for result in results if result.get("mode") == "fmt"]
    load_results = [result for result in results if result.get("mode") == "load"]
    lines = [
        "# AmiFuse Matrix Run",
        "",
        f"- Date: {time.strftime('%Y-%m-%d')}",
        f"- Fixture root: `{FIXTURE_ROOT}`",
        f"- Worker timeout: `{DEFAULT_TIMEOUT:.0f}s`",
        f"- Runs per fixture: `{results[0]['runs'] if results else 0}`",
        "",
    ]
    if ro_results:
        lines.extend(
            [
                "## Read-only Smoke",
                "",
                "| FS | Status | Inspect med | Init med | Root med | Stat med | Small med | Large med | Flush med | Total min/med/max | Notes |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
    for result in ro_results:
        notes = []
        if result["status"] == "ok":
            notes.append(f"runs={result['runs']}")
            notes.append(f"root={result['root_count']}")
            if result.get("lookup_path"):
                notes.append(f"lookup={result['lookup_path']}")
            if result.get("small_read_path"):
                notes.append(f"small={result['small_read_path']}")
            if result.get("large_read_path"):
                notes.append(f"large={result['large_read_path']}")
            row = [
                result["fs_name"],
                "ok",
                _format_seconds(float(result["inspect_s_median"])),
                _format_seconds(float(result["init_s_median"])),
                _format_seconds(float(result["list_root_s_median"])),
                _format_seconds(float(result["stat_s_median"])),
                _format_seconds(float(result["small_read_s_median"])),
                _format_seconds(float(result["large_read_s_median"])),
                _format_seconds(float(result["flush_s_median"])),
                _format_total_range(result),
                "<br>".join(notes),
            ]
        else:
            notes.append(f"runs={result.get('runs', 0)}")
            notes.append(result.get("error", "unknown error"))
            row = [
                FIXTURES[result["fixture"]].fs_name,
                result["status"],
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "<br>".join(notes),
            ]
        lines.append("| " + " | ".join(row) + " |")
    if rw_results:
        lines.extend(
            [
                "",
                "## Writable Smoke",
                "",
                "| FS | Status | Inspect med | Init med | Root med | Mkdir med | Create med | Write med | Rename med | Flush med | Remount med | Verify stat med | Verify read med | Delete med | Cleanup flush med | Total min/med/max | Notes |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
    for result in rw_results:
        notes = []
        if result["status"] == "ok":
            notes.append(f"runs={result['runs']}")
            notes.append(f"root={result['root_count']}")
            if result.get("lookup_path"):
                notes.append(f"verify={result['lookup_path']}")
            row = [
                result["fs_name"],
                "ok",
                _format_seconds(float(result["inspect_s_median"])),
                _format_seconds(float(result["init_s_median"])),
                _format_seconds(float(result["list_root_s_median"])),
                _format_seconds(float(result["mkdir_s_median"])),
                _format_seconds(float(result["create_s_median"])),
                _format_seconds(float(result["write_s_median"])),
                _format_seconds(float(result["rename_s_median"])),
                _format_seconds(float(result["flush_s_median"])),
                _format_seconds(float(result["remount_s_median"])),
                _format_seconds(float(result["verify_stat_s_median"])),
                _format_seconds(float(result["verify_read_s_median"])),
                _format_seconds(float(result["delete_s_median"])),
                _format_seconds(float(result["cleanup_flush_s_median"])),
                _format_total_range(result),
                "<br>".join(notes),
            ]
        else:
            notes.append(f"runs={result.get('runs', 0)}")
            notes.append(result.get("error", "unknown error"))
            row = [
                FIXTURES[result["fixture"]].fs_name,
                result["status"],
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "<br>".join(notes),
            ]
        lines.append("| " + " | ".join(row) + " |")
    if fmt_results:
        lines.extend(
            [
                "",
                "## Format Smoke",
                "",
                "| FS | Status | Create img med | Inspect med | Format med | Init med | Root med | Mkdir med | Create med | Write med | Rename med | Flush med | Remount med | Verify stat med | Verify read med | Delete med | Cleanup flush med | Total min/med/max | Notes |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
    for result in fmt_results:
        notes = []
        if result["status"] == "ok":
            notes.append(f"runs={result['runs']}")
            notes.append(f"root={result['root_count']}")
            if result.get("partition"):
                notes.append(f"part={result['partition']}")
            if result.get("lookup_path"):
                notes.append(f"verify={result['lookup_path']}")
            row = [
                result["fs_name"],
                "ok",
                _format_seconds(float(result["create_image_s_median"])),
                _format_seconds(float(result["inspect_s_median"])),
                _format_seconds(float(result["format_s_median"])),
                _format_seconds(float(result["init_s_median"])),
                _format_seconds(float(result["list_root_s_median"])),
                _format_seconds(float(result["mkdir_s_median"])),
                _format_seconds(float(result["create_s_median"])),
                _format_seconds(float(result["write_s_median"])),
                _format_seconds(float(result["rename_s_median"])),
                _format_seconds(float(result["flush_s_median"])),
                _format_seconds(float(result["remount_s_median"])),
                _format_seconds(float(result["verify_stat_s_median"])),
                _format_seconds(float(result["verify_read_s_median"])),
                _format_seconds(float(result["delete_s_median"])),
                _format_seconds(float(result["cleanup_flush_s_median"])),
                _format_total_range(result),
                "<br>".join(notes),
            ]
        else:
            notes.append(f"runs={result.get('runs', 0)}")
            notes.append(result.get("error", "unknown error"))
            row = [
                FIXTURES[result["fixture"]].fs_name,
                result["status"],
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "<br>".join(notes),
            ]
        lines.append("| " + " | ".join(row) + " |")
    if load_results:
        lines.extend(
            [
                "",
                "## Load Benchmark",
                "",
                "| FS | Status | Inspect med | Init med | Root med | Mkdir med | Create large med | Create files med | List load dir med | Read loop med | Flush med | Steady min/med/max | Total min/med/max | Notes |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
    for result in load_results:
        notes = []
        if result["status"] == "ok":
            notes.append(f"runs={result['runs']}")
            notes.append(f"root={result['root_count']}")
            notes.append(
                f"create={result.get('load_file_count', 0)}x{int(result.get('load_file_size_bytes', 0))}B"
            )
            notes.append(
                f"read={result.get('load_read_count', 0)}x{int(result.get('load_read_size_bytes', 0)) // (1024 * 1024)}MiB"
            )
            if result.get("lookup_path"):
                notes.append(f"read-path={result['lookup_path']}")
            steady_range = " / ".join(
                [
                    _format_seconds(float(result["steady_s_min"])),
                    _format_seconds(float(result["steady_s_median"])),
                    _format_seconds(float(result["steady_s_max"])),
                ]
            )
            row = [
                result["fs_name"],
                "ok",
                _format_seconds(float(result["inspect_s_median"])),
                _format_seconds(float(result["init_s_median"])),
                _format_seconds(float(result["list_root_s_median"])),
                _format_seconds(float(result["mkdir_s_median"])),
                _format_seconds(float(result["create_large_s_median"])),
                _format_seconds(float(result["create_many_s_median"])),
                _format_seconds(float(result["list_load_dir_s_median"])),
                _format_seconds(float(result["read_many_s_median"])),
                _format_seconds(float(result["flush_s_median"])),
                steady_range,
                _format_total_range(result),
                "<br>".join(notes),
            ]
        else:
            notes.append(f"runs={result.get('runs', 0)}")
            notes.append(result.get("error", "unknown error"))
            row = [
                FIXTURES[result["fixture"]].fs_name,
                result["status"],
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "<br>".join(notes),
            ]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def _main(args):
    fixture_keys = args.fixtures or [
        key for key, fixture in FIXTURES.items() if fixture.default_run
    ]
    script_path = Path(__file__).resolve()
    results = []
    for key in fixture_keys:
        fixture = FIXTURES[key]
        run_results = []
        for _ in range(args.runs):
            try:
                result = _run_fixture_subprocess(script_path, fixture, args.timeout)
            except subprocess.TimeoutExpired:
                result = {
                    "fixture": key,
                    "status": "timeout",
                    "error": f"worker exceeded {args.timeout:.0f}s timeout",
                }
            run_results.append(result)
            if result.get("status") != "ok":
                break
        results.append(_aggregate_fixture_runs(fixture, run_results))

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        print(_render_markdown(results), end="")

    return 1 if any(result["status"] != "ok" for result in results) else 0


def _parse_args(argv: Optional[Iterable[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures",
        nargs="+",
        choices=sorted(FIXTURES),
        help="subset of fixtures to run",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="per-fixture worker timeout in seconds",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="number of repeated runs per fixture",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable output instead of markdown",
    )
    parser.add_argument(
        "--worker",
        choices=sorted(FIXTURES),
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None):
    args = _parse_args(argv)
    if args.worker:
        return _worker_main(args)
    return _main(args)


if __name__ == "__main__":
    raise SystemExit(main())
