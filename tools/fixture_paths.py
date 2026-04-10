#!/usr/bin/env python3
"""Shared fixture path definitions for AmiFuse test tooling."""

from __future__ import annotations

import gzip
import html
import lzma
import os
import re
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse


_env_root = os.environ.get("AMIFUSE_FIXTURE_ROOT")
FIXTURE_ROOT = Path(_env_root) if _env_root else Path.home() / "AmigaOS" / "AmiFuse"
DRIVERS_DIR = FIXTURE_ROOT / "drivers"
FIXTURES_DIR = FIXTURE_ROOT / "fixtures"
READONLY_DIR = FIXTURES_DIR / "readonly"
DOWNLOADED_DIR = FIXTURES_DIR / "downloaded"
GENERATED_DIR = FIXTURE_ROOT / "generated"
BENCH_DIR = FIXTURE_ROOT / "bench"
TMP_DIR = FIXTURE_ROOT / "tmp"
SRC_DIR = FIXTURE_ROOT / "src"
ODFS_DRIVER = DRIVERS_DIR / "ODFileSystem"
NETBSD_AMIGA_92_URL = "https://aminet.net/misc/os/netbsdamiga92.hdf.gz"
DEFAULT_HDF_URL = (
    "https://drive.google.com/file/d/1B72e2zHbeSKuWgNUfEPcy5IYYJj_TCr0/view"
)
PARCEIRO_FULL_URL = (
    "https://drive.google.com/file/d/1GXV3vGWOkuK_uMsQyo9TbGRsTfPllo5x/view"
)

_GZIP_MAGIC = b"\x1f\x8b"
_XZ_MAGIC = b"\xfd7zXZ\x00"


def _normalize_download_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc == "drive.google.com":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "file" and parts[1] == "d":
            file_id = parts[2]
            return f"https://drive.google.com/uc?export=download&id={file_id}"
        query = parse_qs(parsed.query)
        file_ids = query.get("id")
        if file_ids:
            return (
                "https://drive.google.com/uc?export=download&id="
                f"{file_ids[0]}"
            )
    return url


def _looks_like_html(data: bytes) -> bool:
    prefix = data.lstrip()[:128].lower()
    return prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html")


def _detect_payload_kind(path: Path) -> str:
    with path.open("rb") as fh:
        data = fh.read(512)
    if not data:
        return "empty"
    if data.startswith(_GZIP_MAGIC):
        return "gzip"
    if data.startswith(_XZ_MAGIC):
        return "xz"
    if _looks_like_html(data):
        return "html"
    return "raw"


def _decompress_file(src: Path, dst: Path, kind: str) -> None:
    if kind == "gzip":
        opener = gzip.open
    elif kind == "xz":
        opener = lzma.open
    else:
        raise ValueError(f"unsupported compression kind: {kind}")
    with opener(src, "rb") as in_fh, dst.open("wb") as out_fh:
        while True:
            chunk = in_fh.read(1024 * 1024)
            if not chunk:
                break
            out_fh.write(chunk)


def _repair_existing_fixture(path: Path) -> bool:
    if not path.exists():
        return False
    kind = _detect_payload_kind(path)
    if kind not in ("gzip", "xz"):
        return kind == "raw"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".repair", dir=path.parent
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        _decompress_file(path, tmp_path, kind)
        tmp_path.replace(path)
        return True
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _extract_drive_confirm_url(text: str, base_url: str) -> str | None:
    form_match = re.search(
        r'<form[^>]+action="([^"]+)"[^>]*>(.*?)</form>',
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not form_match:
        return None
    action = html.unescape(form_match.group(1))
    body = form_match.group(2)
    params = {}
    for name, value in re.findall(
        r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"',
        body,
        re.IGNORECASE,
    ):
        params[name] = html.unescape(value)
    if not params:
        return None
    return f"{urljoin(base_url, action)}?{urlencode(params)}"


def _download_fixture_raw(path: Path, url: str, label: str) -> Path:
    url = _normalize_download_url(url)
    for _ in range(3):
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".part", dir=path.parent
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "AmiFuse fixture fetch"}
            )
            with urllib.request.urlopen(req) as response, tmp_path.open("wb") as out_fh:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    out_fh.write(chunk)
                final_url = response.geturl()
            kind = _detect_payload_kind(tmp_path)
            if kind == "html":
                text = tmp_path.read_text(errors="replace")
                confirm_url = _extract_drive_confirm_url(text, final_url)
                tmp_path.unlink(missing_ok=True)
                if confirm_url:
                    url = confirm_url
                    continue
                raise RuntimeError(
                    f"downloaded {label} fixture from {final_url}, got HTML page"
                )
            return tmp_path
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    raise RuntimeError(f"failed to confirm download for {label} fixture from {url}")


def ensure_downloaded_fixture(path: Path, url: str, label: str) -> Path:
    """Download or repair a fixture so the target path contains usable data."""
    if _repair_existing_fixture(path):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    tmp_path = _download_fixture_raw(path, url, label)
    try:
        kind = _detect_payload_kind(tmp_path)
        if kind in ("gzip", "xz"):
            fd, out_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".out", dir=path.parent
            )
            os.close(fd)
            out_path = Path(out_name)
            try:
                _decompress_file(tmp_path, out_path, kind)
                tmp_path.unlink(missing_ok=True)
                out_path.replace(path)
            except Exception:
                out_path.unlink(missing_ok=True)
                raise
        else:
            tmp_path.replace(path)
        return path
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"failed to materialize {label} fixture at {path}: {exc}"
        ) from exc
