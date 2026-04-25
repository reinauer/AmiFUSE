# Testing

This repo currently has two testing layers:

- top-level AmiFuse integration and smoke tests
- `amitools` unit / `pytask` / Amiga-side regression tests in the submodule

`PERFORMANCE.md` records timing policy and current benchmark tables.
It is not the main "how do I run the tests?" document. This file is.

New to the project? See [CONTRIBUTING.md](CONTRIBUTING.md) for clone,
setup, and fixture resolution instructions.

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

The repo has a structured pytest test suite alongside the legacy tools/
scripts. Tests are organized into two layers:

| Layer | Location | Marker | Requires |
|-------|----------|--------|----------|
| Unit | `tests/unit/` | _(none)_ | Python + amifuse installed |
| Integration | `tests/integration/` | `integration` | machine68k + external fixtures |
| Mount | `tests/integration/` | `fuse` | FUSE backend (WinFSP/FUSE-T/fuse3) + external fixtures |

### Quick Start

```sh
# Unit tests (all platforms, no external dependencies)
pytest tests/unit/ -v --timeout=30

# Integration tests (needs fixtures + machine68k)
pytest tests/integration/ -v --timeout=60

# Mount tests only (needs FUSE backend + fixtures)
pytest tests/integration/ -m fuse -v --timeout=60

# All tests
pytest tests/ -v --timeout=60
```

### Fixture Resolution

Integration tests need handler binaries and disk images. They resolve
the fixture directory through a cascade:

1. `AMIFUSE_FIXTURE_ROOT` env var — point this at any directory containing
   `drivers/` (handler binaries) and `fixtures/readonly/` (disk images)
2. `../AmiFUSE-testing` sibling directory (relative to repo root)
3. `~/AmigaOS/AmiFuse` (default local path)
4. `None` → tests skip gracefully with a clear message

The fixture directory must contain at minimum:

```
<fixture-root>/
├── drivers/
│   ├── pfs3aio              # PFS3 handler binary
│   └── FastFileSystem        # FFS/OFS handler binary
└── fixtures/
    └── readonly/
        ├── pfs.hdf           # PFS3 hard drive image
        └── ofs.adf           # OFS floppy image
```

Set the env var to point at your fixture directory:

```sh
export AMIFUSE_FIXTURE_ROOT=/path/to/your/fixtures
pytest tests/integration/ -v
```

### Markers

| Marker | Meaning |
|--------|---------|
| `slow` | Slow tests (deselect with `-m "not slow"`) |
| `fuse` | Requires FUSE/WinFSP kernel driver |
| `integration` | Requires external fixtures and machine68k |
| `windows` / `macos` / `linux` | Platform-specific tests |

### CI (GitHub Actions)

The CI workflow (`.github/workflows/ci.yml`) runs four jobs:

| Job | Platforms | Python | What |
|-----|-----------|--------|------|
| `unit-tests` | Linux, macOS, Windows | 3.11, 3.12, 3.13 | `pytest tests/unit/` |
| `integration-tests` | Linux, macOS, Windows | 3.13 | `pytest tests/integration/` with external fixtures |
| `tools-smoke` | Linux, macOS, Windows | 3.13 | `amifuse_matrix.py` + `image_format_smoke.py` |
| `mount-tests` | Linux, macOS, Windows | 3.13 | `pytest tests/integration/ -m fuse` with FUSE backend |

The `mount-tests` job installs a platform-specific FUSE backend:

- **Linux:** fuse3 + libfuse-dev (fuse2 ABI for fusepy)
- **macOS:** FUSE-T (user-space, no kext required) with libfuse compatibility symlink
- **Windows:** WinFSP via chocolatey

Windows integration and tools-smoke jobs use the `machine68k-amifuse` fork
which includes pre-built wheels with fixes for opcode table over-read
([cnvogelg/machine68k#8](https://github.com/cnvogelg/machine68k/issues/8))
and JMP/CAS collision
([cnvogelg/machine68k#9](https://github.com/cnvogelg/machine68k/issues/9)).
This workaround can be removed once upstream machine68k merges the fixes.

## Current Gaps

The following are still planned, not fully documented as standalone test
entry points yet:

- far-end file I/O coverage inside a filesystem that spans a partition
  larger than `4GiB`
- fuller long-run generated benchmark recipes
- ~~fixture-layout cleanup for `~/AmigaOS/AmiFuse/`~~ (resolved:
  `AMIFUSE_FIXTURE_ROOT` env var + fixture cascade in `tests/fixtures/paths.py`)
- ~~Windows integration tests pending machine68k upstream fixes~~ (resolved:
  `machine68k-amifuse` fork with pre-built wheels)
