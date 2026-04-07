# Testing

This repo currently has two testing layers:

- top-level AmiFuse integration and smoke tests
- `amitools` unit / `pytask` / Amiga-side regression tests in the submodule

`PERFORMANCE.md` records timing policy and current benchmark tables.
It is not the main "how do I run the tests?" document. This file is.

## Fixtures

Top-level AmiFuse tests use images and drivers from:

`~/AmigaOS/AmiFuse/`

That directory is outside the repo on purpose. The fixture tree is now
split into:

- `~/AmigaOS/AmiFuse/drivers/`
- `~/AmigaOS/AmiFuse/fixtures/readonly/`
- `~/AmigaOS/AmiFuse/fixtures/downloaded/`
- `~/AmigaOS/AmiFuse/generated/`
- `~/AmigaOS/AmiFuse/bench/`
- `~/AmigaOS/AmiFuse/tmp/`
- `~/AmigaOS/AmiFuse/src/`

New scratch and generated images should live under:

`~/AmigaOS/AmiFuse/generated/`

Current canonical fixture set used by the matrix:

- `fixtures/readonly/pfs.hdf` with `drivers/pfs3aio`
- `fixtures/readonly/sfs.hdf` with `drivers/SmartFilesystem`
- `fixtures/readonly/Default.hdf` with `drivers/FastFileSystem`
- `fixtures/readonly/ofs.adf` with `drivers/FastFileSystem`
- `fixtures/downloaded/netbsdamiga92.hdf` with `drivers/BFFSFilesystem`
- `fixtures/readonly/AmigaOS3.2CD.iso` with `drivers/CDFileSystem`

The `BFFS` NetBSD fixture is fetched on demand from the compressed
aminet payload if
`fixtures/downloaded/netbsdamiga92.hdf` is missing.

`fixtures/readonly/Default.hdf` is also fetched on demand from the
compressed Google Drive upload if it is missing.

Additional explicit smoke coverage:

- `fixtures/readonly/AmigaOS3.2CD.iso` with
  `~/git/xcdfs/build/amiga/ODFileSystem`

## Quick Start

Fastest high-signal top-level checks:

```sh
python3 tools/amifuse_matrix.py
python3 tools/readme_smoke.py
python3 tools/image_format_smoke.py
```

If these pass, the current read-only matrix, documented CLI examples,
and image-format smoke coverage are working.

## Top-Level AmiFuse Tests

### 1. Read-only Matrix

Run:

```sh
python3 tools/amifuse_matrix.py
```

What it covers:

- image inspect
- handler startup
- root listing
- one known-path lookup
- one small-file read
- one larger-file read
- flush / shutdown path

Default fixtures:

- `pfs3`
- `sfs`
- `ffs`
- `ofs`
- `bffs`
- `cdfs`

Dedicated explicit fixture:

- `odfs`

Output modes:

- default: markdown table
- `--json`: machine-readable result objects

Useful options:

```sh
python3 tools/amifuse_matrix.py --fixtures pfs3 sfs
python3 tools/amifuse_matrix.py --fixtures odfs
python3 tools/amifuse_matrix.py --runs 1
python3 tools/amifuse_matrix.py --timeout 120
python3 tools/amifuse_matrix.py --json
```

How to read failures:

- `inspect` failure usually means image detection or partition parsing
- `init` failure usually means handler startup or bootstrap broke
- `root` / `stat` / `small` / `large` failures usually mean filesystem
  packet handling or read-path regressions
- timeout means the worker never reached completion and usually points
  to a stuck handler loop or missing reply

### 2. Writable Smoke Matrix

Run:

```sh
python3 tools/amifuse_matrix.py \
  --fixtures ofs-rw ffs-rw pfs3-rw sfs-rw \
  --runs 3
```

What it adds beyond read-only smoke:

- `mkdir`
- file create
- write
- rename
- remount
- post-remount verify
- delete

These tests use scratch copies under:

`~/AmigaOS/AmiFuse/generated/`

They should not mutate the canonical seed fixtures.

### 3. Format Smoke Matrix

Run:

```sh
python3 tools/amifuse_matrix.py \
  --fixtures ofs-fmt ffs-fmt pfs3-fmt sfs-fmt \
  --runs 3
```

What it covers:

- create a fresh image
- create an RDB
- add the target partition
- format the filesystem through AmiFuse
- mount it read-write
- run writable smoke
- remount and verify

These are the best regression tests for post-format behavior.

### 4. Large Image Smoke

Run:

```sh
python3 tools/amifuse_matrix.py --fixtures pfs3-4g --runs 1 --json
python3 tools/amifuse_matrix.py --fixtures pfs3-part-4g --runs 1 --json
```

What it covers:

- sparse image larger than `4GiB`
- partition starting beyond the `4GiB` boundary
- partition whose filesystem itself spans more than `4GiB`
- format, write, remount, read-back, cleanup

This is not part of the default matrix because it is slower and creates
an ephemeral multi-gigabyte image.

Current limitation:

- the large-partition case verifies format and normal file I/O on a
  `>4GiB` partition
- it does not yet verify file offsets beyond `4GiB` through DOS handle
  APIs, because the current seek/setsize packet path is still `32-bit`

## README / Web Example Smoke

Run:

```sh
python3 tools/readme_smoke.py
```

or:

```sh
make example-smoke
```

What it covers:

- `amifuse inspect`
- `amifuse inspect --full`
- `rdb-inspect`
- `rdb-inspect --full`
- `rdb-inspect --json`
- `rdb-inspect --extract-fs`
- `driver-info`
- documented mount examples through a fake FUSE shim

This does not require a live FUSE mount. It is intended to catch
documentation drift and bootstrap-path regressions.

The README runner now uses the reorganized fixture layout under
`drivers/`, `fixtures/readonly/`, and `generated/`.

## Image Format Smoke

Run:

```sh
python3 tools/image_format_smoke.py
```

or:

```sh
make image-format-smoke
```

What it covers:

- direct `RDB/HDF`
- `ADF`
- `ISO 9660`
- Emu68-style `MBR+RDB`
- Parceiro-style `MBR+RDB`

The runner verifies both:

- image detection / inspect path
- mount bootstrap path through `mount_fuse()`

It uses a fake FUSE shim, so it exercises the real AmiFuse startup path
without requiring a live OS mount.

## Performance

Use:

```sh
python3 tools/amifuse_matrix.py
python3 tools/amifuse_matrix.py \
  --fixtures ofs-rw ffs-rw pfs3-rw sfs-rw \
  --runs 3
python3 tools/amifuse_matrix.py \
  --fixtures ofs-fmt ffs-fmt pfs3-fmt sfs-fmt \
  --runs 3
```

Then compare the results with:

[PERFORMANCE.md](PERFORMANCE.md)

Important interpretation rules:

- only compare like-for-like fixture recipes
- prefer `min / median / max` over single samples
- very small timings are noisy; do not overreact to one bad run
- the historical `PFS` baseline for this rebased line is `0.6s`

There is also an older focused benchmark:

```sh
make bench-pfs
```

That compares this checkout to another checkout and is still useful, but
the matrix is the main current performance harness.

## Amitools Tests

The `amitools` submodule has its own test tree and README:

[amitools/test/README.md](amitools/test/README.md)

The important buckets there are:

- `test/unit`
- `test/pytask`
- `test/suite`

Typical runs from the submodule root:

```sh
cd amitools
python3 -m pytest -q test/unit
python3 -m pytest -q test/pytask
python3 -m pytest -q --auto-build --flavor gcc test/suite
```

Notes:

- `pytest` is required
- some suite tests build Amiga binaries first
- compiler-dependent failures may be toolchain issues, not runtime
  regressions in AmiFuse itself

For the rebased `amifuse-0.5` work, many compatibility fixes landed with
new `amitools` tests already. Top-level AmiFuse matrix failures should be
triaged separately from submodule unit/suite failures.

## Failure Triage

Start with this order:

1. `python3 tools/amifuse_matrix.py --runs 1 --json`
2. rerun only the failing fixture with `--fixtures ...`
3. if it is a writable or format failure, rerun the matching `-rw` or
   `-fmt` fixture only
4. if a documented command fails, run `python3 tools/readme_smoke.py`
5. if the failure looks below the AmiFuse boundary, move into
   `amitools` tests next

In practice:

- `read-only matrix` catches mount/read regressions
- `writable matrix` catches packet/write/remount regressions
- `format matrix` catches format and post-format regressions
- `readme smoke` catches CLI and docs drift
- `amitools` tests catch lower-level runtime semantics

## Pytest Test Suite

The repo has a structured pytest suite under `tests/` that runs without
external fixtures or a live FUSE mount.

### Quick Start

```sh
# all pytest tests (unit + integration, excludes smoke)
pytest tests/ -v --timeout=60

# unit tests only
pytest tests/unit/ -v --timeout=30

# integration tests only (no smoke)
pytest tests/integration/ -v -m "integration and not smoke" --timeout=60
```

### Test Architecture

Tests are organized into four layers:

| Layer | Directory | What It Covers |
|-------|-----------|----------------|
| **Unit** | `tests/unit/` | Pure logic, mocked dependencies, no I/O |
| **Integration** | `tests/integration/` | Cross-module with committed test fixtures |
| **Smoke** | `tests/integration/` (marker) | Wrappers for `tools/` scripts, external fixtures |
| **Legacy** | `tools/*.py` | Original matrix, readme, and format smoke scripts |

Unit and integration tests use committed fixtures under `tests/fixtures/`
and can run anywhere (local, CI, fresh clone). Smoke tests require the
external fixture tree at `~/AmigaOS/AmiFuse/` and are skipped when those
paths are absent.

### Committed Test Fixtures

The `tests/fixtures/` directory contains small images and handler
binaries checked into the repo:

| Path | Description |
|------|-------------|
| `fixtures/images/blank.adf` | Empty ADF floppy image |
| `fixtures/images/test_ofs.adf` | OFS floppy with known directory tree |
| `fixtures/images/test_ffs.adf` | FFS floppy with known directory tree |
| `fixtures/images/pfs3_test.hdf` | PFS3 hard-drive image with test data |
| `fixtures/images/pfs3_8mb.hdf` | 8 MB PFS3 image for write tests |
| `fixtures/handlers/pfs3aio` | PFS3 handler binary |
| `fixtures/icons/` | Reserved for `.info` icon fixtures (currently empty; icon-parser tests use synthetic data) |
| `fixtures/generate_adf.py` | Script to regenerate ADF fixtures |

These fixtures are generated once and committed so CI does not need
Amiga toolchains or large external downloads.

### Markers

| Marker | Description |
|--------|-------------|
| `integration` | Integration tests requiring real fixtures and machine68k |
| `smoke` | Smoke test wrappers for `tools/` scripts (requires external fixtures) |
| `slow` | Long-running tests |
| `fuse` | Requires FUSE/WinFSP kernel driver |
| `windows` | Windows-specific tests |
| `macos` | macOS-specific tests |
| `linux` | Linux-specific tests |

### CI

GitHub Actions runs on every push to `main` and on pull requests:

- **Unit tests:** 3 OS (Ubuntu, macOS, Windows) x 3 Python (3.11, 3.12, 3.13) = 9 jobs
- **Integration tests:** 3 OS x Python 3.13 = 3 jobs, depends on unit-tests
- **Smoke tests:** not in CI (require external fixtures)

Workflow file: `.github/workflows/ci.yml`

## Current Gaps

The following are still planned, not fully documented as standalone test
entry points yet:

- far-end file I/O coverage inside a filesystem that spans a partition
  larger than `4GiB`
- fuller long-run generated benchmark recipes
- ~~fixture-layout cleanup for `~/AmigaOS/AmiFuse/`~~ (committed test
  fixtures now live in `tests/fixtures/`; external fixtures still used
  by smoke tests)
