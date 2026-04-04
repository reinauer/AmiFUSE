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
It times these read-only smoke operations against canonical fixtures in
`~/AmigaOS/AmiFuse/`:

- inspect
- handler init
- root enumeration
- one known-path `stat`
- one small-file read
- one larger-file read
- flush/unmount preparation

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

| FS | Status | Inspect | Init | Root | Stat | Small Read | Large Read | Flush | Total | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `PFS3` | `ok` | `0.008s` | `0.120s` | `0.048s` | `0.014s` | `0.013s` | `0.011s` | `0.028s` | `0.241s` | `pfs.hdf`, `PDH0`, small=`/foo.md`, large=`/S/pci.db` |
| `SFS` | `ok` | `0.024s` | `0.157s` | `0.071s` | `0.000s` | `0.000s` | `0.000s` | `0.011s` | `0.264s` | `sfs.hdf`, `SDH0`, lookup=`/` |
| `FFS` | `ok` | `0.005s` | `0.571s` | `0.049s` | `0.002s` | `0.005s` | `0.004s` | `0.008s` | `0.645s` | `Default.hdf`, `QDH0`, small=`/CD0`, large=`/MMULib.lha` |
| `CDFileSystem` | `ok` | `0.001s` | `0.077s` | `0.019s` | `0.002s` | `0.003s` | `0.004s` | `0.000s` | `0.106s` | `AmigaOS3.2CD.iso`, small=`/CDVersion`, large=`/ADF/Backdrops3.2.adf` |

This is the first all-green broad post-rebase matrix run for the current
canonical fixture set.
