"""Unit tests for tools/amifuse_matrix.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_amifuse_matrix():
    tools_dir = Path(__file__).resolve().parents[2] / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    spec = importlib.util.spec_from_file_location(
        "test_amifuse_matrix_module",
        tools_dir / "amifuse_matrix.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_ok_result(matrix, fixture_key: str, **overrides):
    fixture = matrix.FIXTURES[fixture_key]
    result = {
        "fixture": fixture.key,
        "fs_name": fixture.fs_name,
        "status": "ok",
        "mode": fixture.mode,
        "partition": fixture.partition,
        "image_kind": fixture.image_kind,
        "image_size_mb": fixture.image_size_mb,
        "inspect": {"kind": "rdb", "partition_found": True},
        "root_count": 3,
        "root_names": ["AmiFuseLoad"],
        "lookup_path": "/AmiFuseLoad/bulk-read.bin",
        "small_read_path": "/AmiFuseLoad/bulk-read.bin",
        "large_read_path": "/AmiFuseLoad/bulk-read.bin",
        "small_read_bytes": fixture.load_read_size_bytes,
        "large_read_bytes": fixture.load_read_size_bytes,
        "load_file_count": fixture.load_file_count,
        "load_file_size_bytes": fixture.load_file_size_bytes,
        "load_read_count": fixture.load_read_count,
        "load_read_size_bytes": fixture.load_read_size_bytes,
        "load_read_total_bytes": fixture.load_read_count * fixture.load_read_size_bytes,
        "meta_dir_count": fixture.meta_dir_count,
        "meta_files_per_dir": fixture.meta_files_per_dir,
        "meta_total_files": fixture.meta_dir_count * fixture.meta_files_per_dir,
        "meta_file_size_bytes": fixture.meta_file_size_bytes,
    }
    for key in matrix.TIMING_KEYS:
        result[key] = 0.0
    result.update(
        {
            "inspect_s": 0.01,
            "init_s": 0.02,
            "list_root_s": 0.03,
            "mkdir_s": 0.01,
            "create_large_s": 0.05,
            "create_many_s": 0.80,
            "list_load_dir_s": 0.04,
            "read_many_s": 1.25,
            "mkdir_tree_s": 0.08,
            "stat_many_s": 0.75,
            "rename_many_s": 0.60,
            "list_meta_dirs_s": 0.05,
            "delete_many_s": 0.70,
            "flush_s": 0.01,
            "steady_s": 2.15,
            "total_s": 2.21,
        }
    )
    result.update(overrides)
    return result


def test_parse_args_accepts_load_fixture():
    matrix = _load_amifuse_matrix()

    args = matrix._parse_args(["--fixtures", "pfs3-load", "--runs", "1"])

    assert args.fixtures == ["pfs3-load"]
    assert args.runs == 1


def test_parse_args_accepts_meta_fixture():
    matrix = _load_amifuse_matrix()

    args = matrix._parse_args(["--fixtures", "ofs-meta", "--runs", "1"])

    assert args.fixtures == ["ofs-meta"]
    assert args.runs == 1


def test_aggregate_fixture_runs_keeps_load_metrics():
    matrix = _load_amifuse_matrix()
    fixture = matrix.FIXTURES["pfs3-load"]
    run_results = [
        _make_ok_result(matrix, "pfs3-load", create_many_s=0.90, read_many_s=1.40, steady_s=2.40, total_s=2.46),
        _make_ok_result(matrix, "pfs3-load", create_many_s=0.70, read_many_s=1.10, steady_s=1.90, total_s=1.96),
        _make_ok_result(matrix, "pfs3-load", create_many_s=0.80, read_many_s=1.25, steady_s=2.15, total_s=2.21),
    ]

    summary = matrix._aggregate_fixture_runs(fixture, run_results)

    assert summary["status"] == "ok"
    assert summary["mode"] == "load"
    assert summary["runs"] == 3
    assert summary["create_many_s_median"] == 0.80
    assert summary["read_many_s_median"] == 1.25
    assert summary["steady_s_median"] == 2.15
    assert summary["load_file_count"] == fixture.load_file_count
    assert summary["load_read_count"] == fixture.load_read_count


def test_aggregate_fixture_runs_keeps_meta_metrics():
    matrix = _load_amifuse_matrix()
    fixture = matrix.FIXTURES["ofs-meta"]
    run_results = [
        _make_ok_result(
            matrix,
            "ofs-meta",
            create_many_s=1.20,
            stat_many_s=0.90,
            rename_many_s=0.70,
            delete_many_s=0.85,
            steady_s=3.90,
            total_s=3.98,
        ),
        _make_ok_result(
            matrix,
            "ofs-meta",
            create_many_s=1.10,
            stat_many_s=0.80,
            rename_many_s=0.65,
            delete_many_s=0.80,
            steady_s=3.60,
            total_s=3.68,
        ),
        _make_ok_result(
            matrix,
            "ofs-meta",
            create_many_s=1.15,
            stat_many_s=0.85,
            rename_many_s=0.68,
            delete_many_s=0.82,
            steady_s=3.75,
            total_s=3.83,
        ),
    ]

    summary = matrix._aggregate_fixture_runs(fixture, run_results)

    assert summary["status"] == "ok"
    assert summary["mode"] == "meta"
    assert summary["runs"] == 3
    assert summary["create_many_s_median"] == 1.15
    assert summary["stat_many_s_median"] == 0.85
    assert summary["rename_many_s_median"] == 0.68
    assert summary["delete_many_s_median"] == 0.82
    assert summary["meta_dir_count"] == fixture.meta_dir_count
    assert summary["meta_total_files"] == fixture.meta_dir_count * fixture.meta_files_per_dir


def test_render_markdown_includes_load_section():
    matrix = _load_amifuse_matrix()
    fixture = matrix.FIXTURES["pfs3-load"]
    summary = matrix._aggregate_fixture_runs(
        fixture,
        [
            _make_ok_result(matrix, "pfs3-load", create_many_s=0.90, read_many_s=1.40, steady_s=2.40, total_s=2.46),
            _make_ok_result(matrix, "pfs3-load", create_many_s=0.70, read_many_s=1.10, steady_s=1.90, total_s=1.96),
            _make_ok_result(matrix, "pfs3-load", create_many_s=0.80, read_many_s=1.25, steady_s=2.15, total_s=2.21),
        ],
    )

    markdown = matrix._render_markdown([summary])

    assert "## Load Benchmark" in markdown
    assert "PFS3 load" in markdown
    assert "Read loop med" in markdown
    assert "create=256x256B" in markdown
    assert "read=3200x1MiB" in markdown


def test_render_markdown_includes_meta_section():
    matrix = _load_amifuse_matrix()
    fixture = matrix.FIXTURES["ofs-meta"]
    summary = matrix._aggregate_fixture_runs(
        fixture,
        [
            _make_ok_result(
                matrix,
                "ofs-meta",
                create_many_s=1.20,
                stat_many_s=0.90,
                rename_many_s=0.70,
                delete_many_s=0.85,
                steady_s=3.90,
                total_s=3.98,
            ),
            _make_ok_result(
                matrix,
                "ofs-meta",
                create_many_s=1.10,
                stat_many_s=0.80,
                rename_many_s=0.65,
                delete_many_s=0.80,
                steady_s=3.60,
                total_s=3.68,
            ),
            _make_ok_result(
                matrix,
                "ofs-meta",
                create_many_s=1.15,
                stat_many_s=0.85,
                rename_many_s=0.68,
                delete_many_s=0.82,
                steady_s=3.75,
                total_s=3.83,
            ),
        ],
    )

    markdown = matrix._render_markdown([summary])

    assert "## Metadata Benchmark" in markdown
    assert "OFS meta" in markdown
    assert "Stat files med" in markdown
    assert "files=512x256B" in markdown


def test_worker_main_reports_system_exit_as_error(monkeypatch, capsys):
    matrix = _load_amifuse_matrix()

    def fake_run_fixture_worker(_fixture_key: str):
        raise SystemExit("formatter crashed")

    monkeypatch.setattr(matrix, "_run_fixture_worker", fake_run_fixture_worker)

    rc = matrix._worker_main(type("Args", (), {"worker": "sfs-fmt"})())

    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    result = json.loads(out[-1])
    assert result["status"] == "error"
    assert result["fixture"] == "sfs-fmt"
    assert "formatter crashed" in result["error"]
