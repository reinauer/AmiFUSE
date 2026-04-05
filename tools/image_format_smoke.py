#!/usr/bin/env python3
"""Exercise AmiFuse image-format detection and mount paths."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import subprocess
import sys
import traceback
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from fixture_paths import DOWNLOADED_DIR, DRIVERS_DIR, FIXTURE_ROOT, READONLY_DIR
from fixture_paths import PARCEIRO_FULL_URL, ensure_downloaded_fixture

REPO_ROOT = Path(__file__).resolve().parent.parent
AMITOOLS_ROOT = REPO_ROOT / "amitools"


@dataclass(frozen=True)
class FormatCase:
    key: str
    label: str
    image: Path
    driver: Optional[Path]
    partition: Optional[str]
    expected_kind: str
    expected_scheme: Optional[str]
    expected_root: tuple[str, ...]
    read_path: str
    download_url: Optional[str] = None


@dataclass
class FormatResult:
    key: str
    label: str
    status: str
    inspect_kind: str
    mount_kind: str
    detail: str
    extra: Optional[Dict[str, object]] = None


CASES: List[FormatCase] = [
    FormatCase(
        key="direct-rdb-pfs3",
        label="Direct RDB HDF",
        image=READONLY_DIR / "pfs.hdf",
        driver=None,
        partition="PDH0",
        expected_kind="rdb",
        expected_scheme=None,
        expected_root=("Libs", "foo.md"),
        read_path="/foo.md",
    ),
    FormatCase(
        key="adf-ofs",
        label="ADF OFS",
        image=READONLY_DIR / "ofs.adf",
        driver=DRIVERS_DIR / "FastFileSystem",
        partition=None,
        expected_kind="adf",
        expected_scheme=None,
        expected_root=("Docs", "OFS_README.txt"),
        read_path="/OFS_README.txt",
    ),
    FormatCase(
        key="iso-cdfs",
        label="ISO 9660",
        image=READONLY_DIR / "AmigaOS3.2CD.iso",
        driver=DRIVERS_DIR / "CDFileSystem",
        partition=None,
        expected_kind="iso",
        expected_scheme=None,
        expected_root=("C", "System", "CDVersion"),
        read_path="/CDVersion",
    ),
    FormatCase(
        key="emu68-pfs3",
        label="Emu68 MBR+RDB PFS3",
        image=READONLY_DIR / "mbr.hdf",
        driver=None,
        partition="PDH0",
        expected_kind="rdb",
        expected_scheme="emu68",
        expected_root=("SysInfo", "foo.md"),
        read_path="/foo.md",
    ),
    FormatCase(
        key="emu68-ffs",
        label="Emu68 MBR+RDB FFS",
        image=READONLY_DIR / "emu68k.hdf",
        driver=None,
        partition="UDH0",
        expected_kind="rdb",
        expected_scheme="emu68",
        expected_root=("AWeb_APL",),
        read_path="/AWeb_APL/README",
    ),
    FormatCase(
        key="parceiro-pfs3",
        label="Parceiro MBR+RDB PFS3",
        image=DOWNLOADED_DIR / "parceiro-full.img",
        driver=None,
        partition="SD0",
        expected_kind="rdb",
        expected_scheme="parceiro",
        expected_root=("Launcher", "Parceiro", "c"),
        read_path="/Launcher",
        download_url=PARCEIRO_FULL_URL,
    ),
]


def _ensure_import_path():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    if str(AMITOOLS_ROOT) not in sys.path:
        sys.path.insert(0, str(AMITOOLS_ROOT))


def _install_fake_fuse():
    if "fuse" in sys.modules:
        del sys.modules["fuse"]

    fake_fuse = types.ModuleType("fuse")

    class _DummyOperations:
        pass

    class _DummyLoggingMixIn:
        pass

    class _DummyFuseError(RuntimeError):
        pass

    fake_fuse.FuseOSError = _DummyFuseError
    fake_fuse.LoggingMixIn = _DummyLoggingMixIn
    fake_fuse.Operations = _DummyOperations
    sys.modules["fuse"] = fake_fuse
    return fake_fuse


def _load_runtime():
    _ensure_import_path()
    fake_fuse = _install_fake_fuse()
    from amifuse.rdb_inspect import detect_adf, detect_iso, open_rdisk
    import amifuse.fuse_fs as fuse_fs

    fuse_fs = importlib.reload(fuse_fs)
    return fake_fuse, fuse_fs, detect_adf, detect_iso, open_rdisk


def _inspect_case(case: FormatCase, detect_adf, detect_iso, open_rdisk):
    adf_info = detect_adf(case.image)
    if adf_info is not None:
        if case.expected_kind != "adf":
            raise RuntimeError(f"expected {case.expected_kind}, got adf")
        return {
            "kind": "adf",
            "dos_type": f"0x{adf_info.dos_type:08x}",
            "total_blocks": adf_info.total_blocks,
        }

    iso_info = detect_iso(case.image)
    if iso_info is not None:
        if case.expected_kind != "iso":
            raise RuntimeError(f"expected {case.expected_kind}, got iso")
        return {
            "kind": "iso",
            "volume_id": iso_info.volume_id.rstrip("\x00"),
            "total_blocks": iso_info.total_blocks,
        }

    blkdev = None
    rdisk = None
    try:
        blkdev, rdisk, mbr_ctx = open_rdisk(case.image)
        part_names = [str(part.part_blk.drv_name) for part in rdisk.parts]
        if case.partition and case.partition not in part_names:
            raise RuntimeError(
                f"partition {case.partition!r} missing from inspect data: {part_names}"
            )
        scheme = getattr(mbr_ctx, "scheme", None) if mbr_ctx else None
        if case.expected_kind != "rdb":
            raise RuntimeError(f"expected {case.expected_kind}, got rdb")
        if scheme != case.expected_scheme:
            raise RuntimeError(
                f"expected scheme {case.expected_scheme!r}, got {scheme!r}"
            )
        return {
            "kind": "rdb",
            "mbr_scheme": scheme,
            "partition_count": len(part_names),
            "partitions": part_names,
            "warning_count": len(getattr(rdisk, "rdb_warnings", [])),
        }
    finally:
        if rdisk is not None:
            rdisk.close()
        if blkdev is not None:
            blkdev.close()


def _cleanup_bridge(bridge):
    shutdown = getattr(getattr(bridge, "vh", None), "shutdown", None)
    if shutdown is not None:
        shutdown()
    backend = getattr(bridge, "backend", None)
    if backend is not None:
        backend.close()


def _mount_case(case: FormatCase, fake_fuse, fuse_fs):
    invocation: Dict[str, object] = {}

    def _fake_mount(fs, mountpoint, **kwargs):
        root_names = [entry["name"] for entry in fs.bridge.list_dir_path("/")]
        missing = [name for name in case.expected_root if name not in root_names]
        if missing:
            raise RuntimeError(
                f"root listing missing expected entries: {', '.join(missing)}"
            )
        data = fs.bridge.read_file(case.read_path, 64, 0)
        invocation.update(
            {
                "volume": str(fs.bridge.volume_name()),
                "root_sample": root_names[:12],
                "read_path": case.read_path,
                "read_bytes": len(data),
            }
        )
        _cleanup_bridge(fs.bridge)

    fake_fuse.FUSE = _fake_mount
    fuse_fs.FUSE = _fake_mount
    mountpoint = REPO_ROOT / "run" / "image-format-smoke" / case.key
    mountpoint.parent.mkdir(parents=True, exist_ok=True)

    with open(os.devnull, "w") as null_out, open(os.devnull, "w") as null_err:
        with contextlib.redirect_stdout(null_out), contextlib.redirect_stderr(null_err):
            fuse_fs.mount_fuse(
                image=case.image,
                driver=case.driver,
                mountpoint=mountpoint,
                block_size=None,
                partition=case.partition,
            )

    if not invocation:
        raise RuntimeError("fake FUSE was not invoked")
    return invocation


def _run_case(case: FormatCase) -> FormatResult:
    try:
        if case.download_url:
            ensure_downloaded_fixture(case.image, case.download_url, case.label)
        fake_fuse, fuse_fs, detect_adf, detect_iso, open_rdisk = _load_runtime()
        inspect_info = _inspect_case(case, detect_adf, detect_iso, open_rdisk)
        mount_info = _mount_case(case, fake_fuse, fuse_fs)
        detail = (
            f"kind={inspect_info['kind']}"
            f", read={mount_info['read_path']} ({mount_info['read_bytes']} bytes)"
            f", root={', '.join(mount_info['root_sample'])}"
        )
        if inspect_info["kind"] == "rdb" and inspect_info.get("mbr_scheme"):
            detail += f", scheme={inspect_info['mbr_scheme']}"
        if inspect_info["kind"] == "iso":
            detail += f", volume={inspect_info['volume_id']}"
        return FormatResult(
            key=case.key,
            label=case.label,
            status="ok",
            inspect_kind=str(inspect_info["kind"]),
            mount_kind=str(inspect_info["kind"]),
            detail=detail,
            extra={"inspect": inspect_info, "mount": mount_info},
        )
    except BaseException as exc:
        return FormatResult(
            key=case.key,
            label=case.label,
            status="error",
            inspect_kind=case.expected_kind,
            mount_kind=case.expected_kind,
            detail=f"{exc}\n{traceback.format_exc()}",
        )


def _run_cases(cases: Iterable[FormatCase]) -> List[FormatResult]:
    results: List[FormatResult] = []
    for case in cases:
        proc = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--case",
                case.key,
                "--json",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if not proc.stdout.strip():
            detail = proc.stderr.strip() or f"subprocess exit={proc.returncode}"
            results.append(
                FormatResult(
                    key=case.key,
                    label=case.label,
                    status="error",
                    inspect_kind=case.expected_kind,
                    mount_kind=case.expected_kind,
                    detail=detail,
                )
            )
            continue
        payload = json.loads(proc.stdout)
        results.append(FormatResult(**payload))
    return results


def _render_markdown(results: List[FormatResult]) -> str:
    lines = [
        "# Image Format Smoke",
        "",
        f"- Fixture root: `{FIXTURE_ROOT}`",
        "",
        "| Case | Status | Kind | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for result in results:
        detail = result.detail.replace("\n", "<br>")
        lines.append(
            f"| `{result.key}` | `{result.status}` | `{result.inspect_kind}` | {detail} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Exercise AmiFuse image-format detection and mount paths."
    )
    parser.add_argument(
        "--case",
        choices=[case.key for case in CASES],
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    args = parser.parse_args(argv)

    if args.case:
        case = next(case for case in CASES if case.key == args.case)
        result = _run_case(case)
        if args.json:
            print(json.dumps(result.__dict__, indent=2))
        else:
            print(_render_markdown([result]), end="")
        return 0

    results = _run_cases(CASES)
    if args.json:
        print(json.dumps([result.__dict__ for result in results], indent=2))
    else:
        print(_render_markdown(results), end="")
    return 0 if all(result.status == "ok" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
