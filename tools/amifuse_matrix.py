#!/usr/bin/env python3
"""Run timed AmiFuse filesystem smoke checks against canonical fixtures."""

from __future__ import annotations

import argparse
import json
import logging
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


def _run_fixture_worker(fixture_key: str):
    fixture = FIXTURES[fixture_key]
    HandlerBridge, detect_adf, detect_iso, open_rdisk = _load_runtime()

    inspect_s, inspect_info = _timed(
        _inspect_fixture, fixture, detect_adf, detect_iso, open_rdisk
    )
    inspect_meta, adf_info, iso_info = inspect_info
    if not inspect_meta.get("partition_found", True):
        raise RuntimeError(
            f"expected partition {fixture.partition!r} not found in inspect data"
        )

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
            "flush_s": flush_s,
            "total_s": total_s,
            "small_read_bytes": small_read_bytes,
            "large_read_bytes": large_read_bytes,
        }
    finally:
        shutdown = getattr(getattr(bridge, "vh", None), "shutdown", None)
        if shutdown is not None:
            shutdown()


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


def _format_seconds(value: float) -> str:
    return f"{value:.3f}s"


def _render_markdown(results: List[Dict[str, object]]) -> str:
    lines = [
        "# AmiFuse Matrix Run",
        "",
        f"- Date: {time.strftime('%Y-%m-%d')}",
        f"- Fixture root: `{FIXTURE_ROOT}`",
        f"- Worker timeout: `{DEFAULT_TIMEOUT:.0f}s`",
        "",
        "| FS | Status | Inspect | Init | Root | Stat | Small Read | Large Read | Flush | Total | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        notes = []
        if result["status"] == "ok":
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
                _format_seconds(result["inspect_s"]),
                _format_seconds(result["init_s"]),
                _format_seconds(result["list_root_s"]),
                _format_seconds(result["stat_s"]),
                _format_seconds(result["small_read_s"]),
                _format_seconds(result["large_read_s"]),
                _format_seconds(result["flush_s"]),
                _format_seconds(result["total_s"]),
                "<br>".join(notes),
            ]
        else:
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
    return "\n".join(lines) + "\n"


def _main(args):
    fixture_keys = args.fixtures or list(FIXTURES)
    script_path = Path(__file__).resolve()
    results = []
    for key in fixture_keys:
        fixture = FIXTURES[key]
        try:
            result = _run_fixture_subprocess(script_path, fixture, args.timeout)
        except subprocess.TimeoutExpired:
            result = {
                "fixture": key,
                "status": "timeout",
                "error": f"worker exceeded {args.timeout:.0f}s timeout",
            }
        results.append(result)

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
