# Performance

## Policy

Performance numbers are only comparable within the same fixture recipe.
Ad hoc timings from a hand-maintained demo disk are useful for smoke
checks, but long-run regression tracking should move toward generated
fixtures with controlled:

- filesystem type
- image size
- file count
- directory count
- file size distribution
- fill percentage
- read-only vs read-write mode

For the current rebased `amifuse-0.5` line, the historical PFS
traversal baseline remains `0.6s`.

## Current Matrix

The first integration runner is [`tools/amifuse_matrix.py`](/Users/stepan/git/AmiFuse-codex/tools/amifuse_matrix.py).
It runs repeated read-only smoke checks against canonical fixtures in
`~/AmigaOS/AmiFuse/` and times:

- inspect
- handler init
- root enumeration
- one known-path `stat`
- one small-file read
- one larger-file read
- flush/unmount preparation

The harness now defaults to `3` runs per fixture and reports:

- per-operation median times
- total time as `min / median / max`

The initial canonical set is:

- `PFS3`: `pfs.hdf` with `pfs3aio`
- `SFS`: `sfs.hdf` with `SmartFilesystem`
- `FFS`: `Default.hdf` with `FastFileSystem`
- `OFS`: `ofs.adf` with `FastFileSystem`
- `CDFileSystem`: `AmigaOS3.2CD.iso` with `CDFileSystem`

`BFFS` is intentionally left for the next pass, where the fixture will
be extracted or generated in a more controlled way.

## Latest Run

Run:

```sh
python3 tools/amifuse_matrix.py
```

Date: `2026-04-04`

| FS | Status | Inspect med | Init med | Root med | Stat med | Small med | Large med | Flush med | Total min / med / max | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `PFS3` | `ok` | `0.008s` | `0.054s` | `0.025s` | `0.003s` | `0.015s` | `0.019s` | `0.003s` | `0.127s / 0.138s / 0.139s` | `runs=3`, `pfs.hdf`, `PDH0`, small=`/foo.md`, large=`/S/pci.db` |
| `SFS` | `ok` | `0.012s` | `0.086s` | `0.017s` | `0.000s` | `0.000s` | `0.000s` | `0.007s` | `0.118s / 0.121s / 0.125s` | `runs=3`, `sfs.hdf`, `SDH0`, lookup=`/` |
| `FFS` | `ok` | `0.003s` | `0.555s` | `0.043s` | `0.002s` | `0.015s` | `0.004s` | `0.001s` | `0.618s / 0.621s / 0.646s` | `runs=3`, `Default.hdf`, `QDH0`, small=`/CD0`, large=`/MMULib.lha` |
| `OFS` | `ok` | `0.000s` | `0.025s` | `0.006s` | `0.002s` | `0.002s` | `0.085s` | `0.001s` | `0.118s / 0.119s / 0.125s` | `runs=3`, `ofs.adf`, small=`/OFS_README.txt`, large=`/Docs/OFS_LARGE.bin` |
| `CDFileSystem` | `ok` | `0.000s` | `0.071s` | `0.018s` | `0.002s` | `0.003s` | `0.003s` | `0.000s` | `0.099s / 0.099s / 0.105s` | `runs=3`, `AmigaOS3.2CD.iso`, small=`/CDVersion`, large=`/ADF/Backdrops3.2.adf` |

This is the first all-green aggregated matrix run for the expanded
canonical fixture set, now including an `OFS` floppy image. The earlier
single-run table overstated drift, especially for `PFS3`, because its
totals were too noisy to compare from one sample.
