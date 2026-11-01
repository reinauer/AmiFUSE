"""Unit tests for tools/image_format_smoke.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_image_format_smoke():
    tools_dir = Path(__file__).resolve().parents[2] / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    spec = importlib.util.spec_from_file_location(
        "test_image_format_smoke_module",
        tools_dir / "image_format_smoke.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("output_args", [["--json"], []], ids=["json", "markdown"])
@pytest.mark.parametrize(
    ("status", "expected_exit"), [("ok", 0), ("error", 1)], ids=["ok", "error"]
)
def test_case_exit_code_follows_status(
    monkeypatch, capsys, status, expected_exit, output_args
):
    smoke = _load_image_format_smoke()
    case = smoke.CASES[0]

    def _fake_run_case(run_case, **kwargs):
        return smoke.FormatResult(
            key=run_case.key,
            label=run_case.label,
            status=status,
            inspect_kind=run_case.expected_kind,
            mount_kind=run_case.expected_kind,
            detail="stub",
        )

    monkeypatch.setattr(smoke, "_run_case", _fake_run_case)

    assert smoke.main(["--case", case.key, *output_args]) == expected_exit

    out = capsys.readouterr().out
    if output_args:
        assert json.loads(out)["status"] == status
    else:
        assert f"`{status}`" in out
