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


REPO_ROOT = Path(__file__).resolve().parent.parent
AMITOOLS_ROOT = REPO_ROOT / "amitools"
FIXTURE_ROOT = Path.home() / "AmigaOS" / "AmiFuse"
DEFAULT_TIMEOUT = 60.0


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
    seed_image: Optional[Path] = None
    default_run: bool = True


FIXTURES: Dict[str, Fixture] = {
    "pfs3": Fixture(
        key="pfs3",
        fs_name="PFS3",
        image=FIXTURE_ROOT / "pfs.hdf",
        driver=FIXTURE_ROOT / "pfs3aio",
        partition="PDH0",
        mode="ro",
        image_kind="rdb-hdf",
        image_size_mb=8,
        expected_root=("Libs", "S", "foo.md", "plan.md"),
        lookup_path="/foo.md",
    ),
    "pfs3-rw": Fixture(
        key="pfs3-rw",
        fs_name="PFS3 rw",
        image=FIXTURE_ROOT / "generated" / "pfs3_rw.hdf",
        driver=FIXTURE_ROOT / "pfs3aio",
        partition="PDH0",
        mode="rw",
        image_kind="rdb-hdf",
        image_size_mb=8,
        seed_image=FIXTURE_ROOT / "pfs.hdf",
        default_run=False,
    ),
    "sfs": Fixture(
        key="sfs",
        fs_name="SFS",
        image=FIXTURE_ROOT / "sfs.hdf",
        driver=FIXTURE_ROOT / "SmartFilesystem",
        partition="SDH0",
        mode="ro",
        image_kind="rdb-hdf",
        image_size_mb=8,
    ),
    "sfs-rw": Fixture(
        key="sfs-rw",
        fs_name="SFS rw",
        image=FIXTURE_ROOT / "generated" / "sfs_rw.hdf",
        driver=FIXTURE_ROOT / "SmartFilesystem",
        partition="SDH0",
        mode="rw",
        image_kind="rdb-hdf",
        image_size_mb=8,
        seed_image=FIXTURE_ROOT / "sfs.hdf",
        default_run=False,
    ),
    "ffs": Fixture(
        key="ffs",
        fs_name="FFS",
        image=FIXTURE_ROOT / "Default.hdf",
        driver=FIXTURE_ROOT / "FastFileSystem",
        partition="QDH0",
        mode="ro",
        image_kind="rdb-hdf",
        image_size_mb=512,
    ),
    "ffs-rw": Fixture(
        key="ffs-rw",
        fs_name="FFS rw",
        image=FIXTURE_ROOT / "generated" / "ffs_rw.hdf",
        driver=FIXTURE_ROOT / "FastFileSystem",
        partition="QDH0",
        mode="rw",
        image_kind="rdb-hdf",
        image_size_mb=512,
        seed_image=FIXTURE_ROOT / "Default.hdf",
        default_run=False,
    ),
    "ofs": Fixture(
        key="ofs",
        fs_name="OFS",
        image=FIXTURE_ROOT / "ofs.adf",
        driver=FIXTURE_ROOT / "FastFileSystem",
        partition=None,
        mode="ro",
        image_kind="adf",
        image_size_mb=1,
        expected_root=("Docs", "OFS_README.txt"),
        lookup_path="/OFS_README.txt",
    ),
    "ofs-rw": Fixture(
        key="ofs-rw",
        fs_name="OFS rw",
        image=FIXTURE_ROOT / "generated" / "ofs_rw.adf",
        driver=FIXTURE_ROOT / "FastFileSystem",
        partition=None,
        mode="rw",
        image_kind="adf",
        image_size_mb=1,
        seed_image=FIXTURE_ROOT / "ofs.adf",
        default_run=False,
    ),
    "cdfs": Fixture(
        key="cdfs",
        fs_name="CDFileSystem",
        image=FIXTURE_ROOT / "AmigaOS3.2CD.iso",
        driver=FIXTURE_ROOT / "CDFileSystem",
        partition=None,
        mode="ro",
        image_kind="iso",
        image_size_mb=74,
        expected_root=("C", "Devs", "Libs", "System"),
        lookup_path="/System",
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
    from amifuse.fuse_fs import HandlerBridge
    from amifuse.rdb_inspect import detect_adf, detect_iso, open_rdisk

    return HandlerBridge, detect_adf, detect_iso, open_rdisk


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
            parts.append(
                {
                    "name": part_name,
                    "dos_type": f"0x{part.part_blk.dos_env.dos_type:08x}",
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
    shutdown = getattr(getattr(bridge, "vh", None), "shutdown", None)
    if shutdown is not None:
        shutdown()


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
        small_path = samples["small"]
        large_path = samples["large"]

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
            "total_s": total_s,
            "small_read_bytes": small_read_bytes,
            "large_read_bytes": large_read_bytes,
        }
    finally:
        _shutdown_bridge(bridge)


def _run_rw_fixture(fixture: Fixture, HandlerBridge, adf_info, iso_info, inspect_s, inspect_meta):
    rw_dir = "/AmiFuseRW"
    created_path = f"{rw_dir}/hello.txt"
    renamed_path = f"{rw_dir}/hello-renamed.txt"
    payload = b"AmiFuse writable smoke\n"
    payload += bytes((ord("A") + (idx % 26) for idx in range(8192)))

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
    finally:
        _shutdown_bridge(bridge)

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
        _, _ = _timed(_delete_path, verify_bridge, rw_dir)
        cleanup_flush_s, _ = _timed(verify_bridge.flush_volume)
        total_s = (
            inspect_s
            + init_s
            + list_root_s
            + mkdir_s
            + create_s
            + write_s
            + rename_s
            + flush_s
            + remount_s
            + verify_stat_s
            + verify_read_s
            + delete_s
            + cleanup_flush_s
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
            "lookup_path": renamed_path,
            "small_read_path": renamed_path,
            "large_read_path": renamed_path,
            "inspect_s": inspect_s,
            "init_s": init_s,
            "list_root_s": list_root_s,
            "stat_s": 0.0,
            "small_read_s": 0.0,
            "large_read_s": 0.0,
            "mkdir_s": mkdir_s,
            "create_s": create_s,
            "write_s": write_s,
            "rename_s": rename_s,
            "flush_s": flush_s,
            "remount_s": remount_s,
            "verify_stat_s": verify_stat_s,
            "verify_read_s": verify_read_s,
            "delete_s": delete_s,
            "cleanup_flush_s": cleanup_flush_s,
            "total_s": total_s,
            "small_read_bytes": len(payload),
            "large_read_bytes": len(payload),
        }
    finally:
        _shutdown_bridge(verify_bridge)


def _run_fixture_worker(fixture_key: str):
    fixture = FIXTURES[fixture_key]
    HandlerBridge, detect_adf, detect_iso, open_rdisk = _load_runtime()
    if fixture.mode == "rw":
        _prepare_rw_image(fixture)

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
    return _run_ro_fixture(
        fixture, HandlerBridge, adf_info, iso_info, inspect_s, inspect_meta
    )


def _worker_main(args):
    try:
        result = _run_fixture_worker(args.worker)
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
    return result


TIMING_KEYS = (
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
    "total_s",
)


def _aggregate_fixture_runs(
    fixture: Fixture, run_results: List[Dict[str, object]]
) -> Dict[str, object]:
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
