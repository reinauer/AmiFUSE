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
- `CDFileSystem`: `AmigaOS3.2CD.iso` with `CDFileSystem`

`OFS` and `BFFS` are intentionally left for the next pass, where the
fixtures will be generated or extracted in a more controlled way.

## Latest Run

Run:

```sh
python3 tools/amifuse_matrix.py
```

Date: `2026-04-03`

| FS | Status | Inspect med | Init med | Root med | Stat med | Small med | Large med | Flush med | Total min / med / max | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `PFS3` | `ok` | `0.005s` | `0.039s` | `0.016s` | `0.002s` | `0.007s` | `0.022s` | `0.002s` | `0.084s / 0.092s / 0.097s` | `runs=3`, `pfs.hdf`, `PDH0`, small=`/foo.md`, large=`/S/pci.db` |
| `SFS` | `ok` | `0.008s` | `0.074s` | `0.013s` | `0.000s` | `0.000s` | `0.000s` | `0.004s` | `0.096s / 0.099s / 0.102s` | `runs=3`, `sfs.hdf`, `SDH0`, lookup=`/` |
| `FFS` | `ok` | `0.002s` | `0.552s` | `0.032s` | `0.001s` | `0.002s` | `0.002s` | `0.000s` | `0.587s / 0.593s / 0.596s` | `runs=3`, `Default.hdf`, `QDH0`, small=`/CD0`, large=`/MMULib.lha` |
| `CDFileSystem` | `ok` | `0.000s` | `0.063s` | `0.016s` | `0.001s` | `0.002s` | `0.002s` | `0.000s` | `0.083s / 0.086s / 0.087s` | `runs=3`, `AmigaOS3.2CD.iso`, small=`/CDVersion`, large=`/ADF/Backdrops3.2.adf` |

This is the first all-green aggregated matrix run for the current
canonical fixture set. The earlier single-run table overstated drift,
especially for `PFS3`, because its totals were too noisy to compare from
one sample.
