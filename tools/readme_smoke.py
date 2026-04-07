#!/usr/bin/env python3
"""Exercise documented README/web examples without live FUSE mounts."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from fixture_paths import DEFAULT_HDF_URL, DRIVERS_DIR, FIXTURE_ROOT, GENERATED_DIR
from fixture_paths import READONLY_DIR, ensure_downloaded_fixture

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ExampleResult:
    name: str
    mode: str
    status: str
    command: str
    details: str = ""
    extra: Optional[Dict[str, object]] = None


def _run_subprocess(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _cli_example(name: str, command: List[str], expect: Callable[[subprocess.CompletedProcess[str]], str]) -> ExampleResult:
    proc = _run_subprocess(*command)
    if proc.returncode != 0:
        return ExampleResult(
            name=name,
            mode="cli",
            status="error",
            command=" ".join(command),
            details=(proc.stderr.strip() or proc.stdout.strip() or f"exit={proc.returncode}"),
        )
    try:
        details = expect(proc)
    except Exception as exc:
        return ExampleResult(
            name=name,
            mode="cli",
            status="error",
            command=" ".join(command),
            details=f"{exc}\n{traceback.format_exc()}",
        )
    return ExampleResult(name=name, mode="cli", status="ok", command=" ".join(command), details=details)


def _load_mount_runtime():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    module = importlib.import_module("amifuse.fuse_fs")
    return module


def _mount_example_runner() -> Callable:
    invocations: List[Dict[str, object]] = []
    module = _load_mount_runtime()

    def _fake_fuse(fs, mountpoint, **kwargs):
        root_names = [entry["name"] for entry in fs.bridge.list_dir_path("/")]
        invocation = {
            "mountpoint": mountpoint,
            "kwargs": kwargs,
            "volume_name": fs.bridge.volume_name(),
            "root_names": root_names,
            "icons_enabled": getattr(fs, "_icons_enabled", False),
        }
        invocations.append(invocation)
        fs.bridge.close()
        return invocation

    module.FUSE = _fake_fuse

    def run(name: str, command: str, check: Callable[[Dict[str, object], object], str], **kwargs) -> ExampleResult:
        try:
            with open(os.devnull, "w") as null_out, open(os.devnull, "w") as null_err:
                with contextlib.redirect_stdout(null_out), contextlib.redirect_stderr(null_err):
                    module.mount_fuse(**kwargs)
            if not invocations:
                raise RuntimeError("fake FUSE was not invoked")
            info = invocations.pop(0)
            details = check(info, module)
            return ExampleResult(name=name, mode="mount", status="ok", command=command, details=details, extra=info)
        except BaseException as exc:
            return ExampleResult(
                name=name,
                mode="mount",
                status="error",
                command=command,
                details=f"{exc}\n{traceback.format_exc()}",
            )

    return run


def _render(results: List[ExampleResult]) -> str:
    lines = [
        "# README Example Smoke",
        "",
        f"- Fixture root: `{FIXTURE_ROOT}`",
        "",
        "| Example | Mode | Status | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for result in results:
        detail = result.details.replace("\n", "<br>")
        lines.append(f"| `{result.command}` | `{result.mode}` | `{result.status}` | {detail} |")
    return "\n".join(lines) + "\n"


def _cli_results() -> List[ExampleResult]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="amifuse_readme_"))
    extract_out = tmp_dir / "pfs3.bin"
    results = [
        _cli_example(
            "module_inspect",
            [sys.executable, "-m", "amifuse", "inspect", str(READONLY_DIR / "pfs.hdf")],
            lambda proc: "PDH0" if "PDH0" in proc.stdout and "PFS3" in proc.stdout else (_raise("inspect output missing PDH0/PFS3")),
        ),
        _cli_example(
            "module_inspect_full",
            [sys.executable, "-m", "amifuse", "inspect", "--full", str(READONLY_DIR / "pfs.hdf")],
            lambda proc: "full inspect ok" if "blk_longs=" in proc.stdout and "fs_block_size=" in proc.stdout else (_raise("full inspect output missing partition detail lines")),
        ),
        _cli_example(
            "rdb_inspect_summary",
            [sys.executable, "-m", "amifuse.rdb_inspect", str(READONLY_DIR / "pfs.hdf")],
            lambda proc: "filesystem summary ok" if "FileSystem #0" in proc.stdout and "PFS3" in proc.stdout else (_raise("rdb-inspect summary missing filesystem info")),
        ),
        _cli_example(
            "rdb_inspect_full",
            [sys.executable, "-m", "amifuse.rdb_inspect", "--full", str(READONLY_DIR / "pfs.hdf")],
            lambda proc: "full rdb inspect ok" if "blk_longs=" in proc.stdout and "fs_block_size=" in proc.stdout else (_raise("rdb-inspect --full missing partition detail lines")),
        ),
        _cli_example(
            "rdb_inspect_json",
            [sys.executable, "-m", "amifuse.rdb_inspect", "--json", str(READONLY_DIR / "pfs.hdf")],
            lambda proc: _expect_rdb_json(proc.stdout),
        ),
        _cli_example(
            "rdb_extract_fs",
            [
                sys.executable,
                "-m",
                "amifuse.rdb_inspect",
                "--extract-fs",
                "0",
                "--out",
                str(extract_out),
                str(READONLY_DIR / "pfs.hdf"),
            ],
            lambda proc: _expect_extracted_fs(extract_out),
        ),
        _cli_example(
            "driver_info",
            [sys.executable, "-m", "amifuse.driver_info", str(DRIVERS_DIR / "pfs3aio")],
            lambda proc: "driver-info ok" if "Segments:" in proc.stdout and "#00 CODE" in proc.stdout else (_raise("driver-info output missing segment summary")),
        ),
    ]
    if extract_out.exists():
        extract_out.unlink()
    tmp_dir.rmdir()
    return results


def _raise(msg: str):
    raise RuntimeError(msg)


def _expect_rdb_json(stdout: str) -> str:
    data = json.loads(stdout)
    parts = data.get("partitions", [])
    if not any(part.get("name") == "PDH0" for part in parts):
        raise RuntimeError("json output missing PDH0 partition")
    return "json output ok"


def _expect_extracted_fs(path: Path) -> str:
    if not path.exists():
        raise RuntimeError("extracted filesystem file missing")
    size = path.stat().st_size
    if size <= 0:
        raise RuntimeError("extracted filesystem file is empty")
    return f"extracted {size} bytes"


def _mount_results() -> List[ExampleResult]:
    ensure_downloaded_fixture(
        READONLY_DIR / "Default.hdf",
        DEFAULT_HDF_URL,
        "Default.hdf",
    )
    run = _mount_example_runner()
    tmp = REPO_ROOT / "run" / "readme-smoke"
    tmp.mkdir(parents=True, exist_ok=True)
    write_image = GENERATED_DIR / "readme_ofs_write.adf"
    write_image.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(READONLY_DIR / "ofs.adf", write_image)
    results = [
        run(
            "mount_embedded_driver",
            "amifuse mount disk.hdf",
            lambda info, module: "embedded driver mount ok" if "Prefs" in info["root_names"] else (_raise("embedded mount root listing missing Prefs")),
            image=READONLY_DIR / "Default.hdf",
            driver=None,
            mountpoint=tmp / "embedded",
            block_size=None,
            partition=None,
        ),
        run(
            "mount_explicit_driver",
            "amifuse mount pfs.hdf --driver pfs3aio",
            lambda info, module: "explicit driver mount ok" if "Libs" in info["root_names"] else (_raise("explicit driver mount root listing missing Libs")),
            image=READONLY_DIR / "pfs.hdf",
            driver=DRIVERS_DIR / "pfs3aio",
            mountpoint=tmp / "pfs3",
            block_size=None,
            partition=None,
        ),
        run(
            "mount_partition_name",
            "amifuse mount multi-partition.hdf --partition DH0",
            lambda info, module: (
                "partition-by-name ok"
                if module.get_partition_name(READONLY_DIR / "Default.hdf", None, "QDH1") == "QDH1"
                and info["volume_name"] == "Work"
                and "Smartfilesystem" in info["root_names"]
                else (_raise("partition-by-name example did not resolve QDH1"))
            ),
            image=READONLY_DIR / "Default.hdf",
            driver=DRIVERS_DIR / "FastFileSystem",
            mountpoint=tmp / "part-name",
            block_size=None,
            partition="QDH1",
        ),
        run(
            "mount_partition_index",
            "amifuse mount multi-partition.hdf --partition 2",
            lambda info, module: (
                "partition-by-index ok"
                if module.get_partition_name(READONLY_DIR / "Default.hdf", None, "1") == "QDH1"
                and info["volume_name"] == "Work"
                and "Smartfilesystem" in info["root_names"]
                else (_raise("partition-by-index example did not resolve index 1"))
            ),
            image=READONLY_DIR / "Default.hdf",
            driver=DRIVERS_DIR / "FastFileSystem",
            mountpoint=tmp / "part-index",
            block_size=None,
            partition="1",
        ),
        run(
            "mount_explicit_mountpoint",
            "amifuse mount disk.hdf --mountpoint ./mnt",
            lambda info, module: (
                f"mountpoint={info['mountpoint']}"
                if str(info["mountpoint"]).endswith("linux-mnt")
                else (_raise("explicit mountpoint example did not use provided mountpoint"))
            ),
            image=READONLY_DIR / "pfs.hdf",
            driver=DRIVERS_DIR / "pfs3aio",
            mountpoint=tmp / "linux-mnt",
            block_size=None,
            partition=None,
        ),
        run(
            "mount_adf_explicit_driver",
            "amifuse mount workbench.adf --driver L/FastFileSystem",
            lambda info, module: "adf mount ok" if "OFS_README.txt" in info["root_names"] else (_raise("ADF mount root listing missing OFS_README.txt")),
            image=READONLY_DIR / "ofs.adf",
            driver=DRIVERS_DIR / "FastFileSystem",
            mountpoint=tmp / "ofs-adf",
            block_size=None,
            partition=None,
        ),
        run(
            "mount_icons",
            "amifuse mount disk.hdf --icons",
            lambda info, module: _expect_icons_mount(info),
            image=READONLY_DIR / "Default.hdf",
            driver=None,
            mountpoint=tmp / "icons",
            block_size=None,
            partition=None,
            icons=True,
        ),
        run(
            "mount_write_mode",
            "amifuse mount disk.hdf --write",
            lambda info, module: "write mode ok" if info["kwargs"].get("ro") is False else (_raise("write mode did not disable read-only FUSE option")),
            image=write_image,
            driver=DRIVERS_DIR / "FastFileSystem",
            mountpoint=tmp / "write",
            block_size=None,
            partition=None,
            write=True,
        ),
    ]
    if write_image.exists():
        write_image.unlink()
    return results


def _expect_icons_mount(info: Dict[str, object]) -> str:
    kwargs = info["kwargs"]
    if not info.get("icons_enabled"):
        raise RuntimeError("icons example did not enable icon mode")
    if sys.platform.startswith("darwin"):
        if not any(isinstance(value, str) and value.endswith(".icns") for value in kwargs.values()):
            raise RuntimeError("icons example did not pass a generated volume icon to FUSE")
    return "icons example ok"


def main(argv: Optional[Iterable[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    args = parser.parse_args(list(argv) if argv is not None else None)

    results = _cli_results() + _mount_results()
    if args.json:
        print(json.dumps([result.__dict__ for result in results], indent=2, sort_keys=True))
    else:
        print(_render(results), end="")
    return 1 if any(result.status != "ok" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
