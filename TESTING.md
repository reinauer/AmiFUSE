# Testing

This repo currently has two testing layers:

- top-level AmiFuse integration and smoke tests
- `amitools` unit / `pytask` / Amiga-side regression tests in the submodule

`PERFORMANCE.md` records timing policy and current benchmark tables.
It is not the main "how do I run the tests?" document. This file is.

## Fixtures

Top-level AmiFuse tests use images and drivers from:

`~/AmigaOS/AmiFuse/`

That directory is outside the repo on purpose. New scratch and generated
images should also live there, usually under:

`~/AmigaOS/AmiFuse/generated/`

Current canonical fixture set used by the matrix:

- `pfs.hdf` with `pfs3aio`
- `sfs.hdf` with `SmartFilesystem`
- `Default.hdf` with `FastFileSystem`
- `ofs.adf` with `FastFileSystem`
- `netbsdamiga92.hdf` with `BFFSFilesystem`
- `AmigaOS3.2CD.iso` with `CDFileSystem`

## Quick Start

Fastest high-signal top-level checks:

```sh
python3 tools/amifuse_matrix.py
python3 tools/readme_smoke.py
```

If both pass, the current read-only matrix and the documented CLI
examples are working.

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

Output modes:

- default: markdown table
- `--json`: machine-readable result objects

Useful options:

```sh
python3 tools/amifuse_matrix.py --fixtures pfs3 sfs
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
```

What it covers:

- sparse image larger than `4GiB`
- partition starting beyond the `4GiB` boundary
- format, write, remount, read-back, cleanup

This is not part of the default matrix because it is slower and creates
an ephemeral multi-gigabyte image.

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

[PERFORMANCE.md](/Users/stepan/git/AmiFuse-codex/PERFORMANCE.md)

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

[amitools/test/README.md](/Users/stepan/git/AmiFuse-codex/amitools/test/README.md)

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

## Current Gaps

The following are still planned, not fully documented as standalone test
entry points yet:

- dedicated image-format coverage for `MBR+RDB` variants
- a test where the filesystem itself spans a partition larger than `4GiB`
- fuller long-run generated benchmark recipes
- fixture-layout cleanup for `~/AmigaOS/AmiFuse/`
