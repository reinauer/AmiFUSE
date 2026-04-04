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
It runs repeated smoke checks against canonical fixtures in
`~/AmigaOS/AmiFuse/` and times:

- inspect
- handler init
- root enumeration
- one known-path `stat`
- one small-file read
- one larger-file read
- flush/unmount preparation

Writable fixture runs add:

- directory create
- file create
- file write
- rename
- remount
- post-remount verify
- delete
- cleanup flush

Format fixture runs add:

- image creation
- filesystem format
- first post-format mount
- writable smoke on the fresh volume
- remount verification after format

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

## Latest Read-only Run

Run:

```sh
python3 tools/amifuse_matrix.py
```

Date: `2026-04-04`

| FS | Status | Inspect med | Init med | Root med | Stat med | Small med | Large med | Flush med | Total min / med / max | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `PFS3` | `ok` | `0.008s` | `0.054s` | `0.023s` | `0.003s` | `0.013s` | `0.021s` | `0.003s` | `0.114s / 0.127s / 0.137s` | `runs=3`, `pfs.hdf`, `PDH0`, small=`/foo.md`, large=`/S/pci.db` |
| `SFS` | `ok` | `0.011s` | `0.084s` | `0.018s` | `0.003s` | `0.017s` | `0.011s` | `0.011s` | `0.143s / 0.162s / 0.171s` | `runs=3`, `sfs.hdf`, `SDH0`, lookup=`/Prefs`, small=`/Prefs/Asl`, large=`/System/Installer` |
| `FFS` | `ok` | `0.003s` | `0.558s` | `0.042s` | `0.002s` | `0.003s` | `0.004s` | `0.001s` | `0.621s / 0.621s / 0.624s` | `runs=3`, `Default.hdf`, `QDH0`, small=`/CD0`, large=`/MMULib.lha` |
| `OFS` | `ok` | `0.000s` | `0.024s` | `0.006s` | `0.002s` | `0.002s` | `0.078s` | `0.001s` | `0.105s / 0.113s / 0.115s` | `runs=3`, `ofs.adf`, small=`/OFS_README.txt`, large=`/Docs/OFS_LARGE.bin` |
| `CDFileSystem` | `ok` | `0.000s` | `0.069s` | `0.018s` | `0.002s` | `0.004s` | `0.004s` | `0.001s` | `0.097s / 0.100s / 0.109s` | `runs=3`, `AmigaOS3.2CD.iso`, small=`/CDVersion`, large=`/ADF/Backdrops3.2.adf` |

This is the current all-green aggregated read-only matrix for the
expanded canonical fixture set. The earlier single-run table overstated
drift, especially for `PFS3`, because its totals were too noisy to
compare from one sample.

## Latest Writable Run

Run:

```sh
python3 tools/amifuse_matrix.py --fixtures ofs-rw ffs-rw pfs3-rw sfs-rw --runs 3
```

Date: `2026-04-04`

The writable smoke tests use scratch copies under
`~/AmigaOS/AmiFuse/generated/`, seeded from the canonical fixtures.

| FS | Status | Inspect med | Init med | Root med | Mkdir med | Create med | Write med | Rename med | Flush med | Remount med | Verify stat med | Verify read med | Delete med | Cleanup flush med | Total min / med / max | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `OFS rw` | `ok` | `0.000s` | `0.023s` | `0.006s` | `0.005s` | `0.003s` | `0.014s` | `0.004s` | `0.001s` | `0.017s` | `0.005s` | `0.009s` | `0.003s` | `0.001s` | `0.090s / 0.091s / 0.093s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `FFS rw` | `ok` | `0.005s` | `0.552s` | `0.068s` | `0.007s` | `0.003s` | `0.003s` | `0.004s` | `0.001s` | `0.547s` | `0.006s` | `0.003s` | `0.011s` | `0.001s` | `1.195s / 1.196s / 1.222s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `PFS3 rw` | `ok` | `0.008s` | `0.053s` | `0.022s` | `0.010s` | `0.014s` | `0.005s` | `0.011s` | `0.004s` | `0.109s` | `0.008s` | `0.007s` | `0.017s` | `0.003s` | `0.260s / 0.289s / 0.299s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `SFS rw` | `ok` | `0.012s` | `0.084s` | `0.019s` | `0.008s` | `0.006s` | `0.003s` | `0.010s` | `0.001s` | `0.109s` | `0.010s` | `0.022s` | `0.010s` | `0.002s` | `0.296s / 0.301s / 0.302s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |

This is the first all-green writable smoke matrix across `OFS`, `FFS`,
`PFS3`, and `SFS`. Bringing `SFS` into this matrix exposed and fixed a
real post-startup compatibility bug: child processes were not preserving
their own blocked wait state or register set, so they never resumed when
port traffic arrived after startup.

## Latest Format Run

Run:

```sh
python3 tools/amifuse_matrix.py --fixtures ofs-fmt ffs-fmt pfs3-fmt sfs-fmt --runs 3
```

Date: `2026-04-04`

The format smoke tests create fresh generated RDB images under
`~/AmigaOS/AmiFuse/generated/`, format them through AmiFuse, then mount
them read-write, create and rename a file, remount, verify the contents,
delete the test file, and flush again.

| FS | Status | Create img med | Inspect med | Format med | Init med | Root med | Mkdir med | Create med | Write med | Rename med | Flush med | Remount med | Verify stat med | Verify read med | Delete med | Cleanup flush med | Total min / med / max | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `OFS fmt` | `ok` | `0.078s` | `0.001s` | `0.038s` | `0.021s` | `0.003s` | `0.006s` | `0.003s` | `0.009s` | `0.005s` | `0.001s` | `0.021s` | `0.005s` | `0.010s` | `0.003s` | `0.001s` | `0.206s / 0.206s / 0.208s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `FFS fmt` | `ok` | `0.077s` | `0.001s` | `0.040s` | `0.022s` | `0.003s` | `0.005s` | `0.004s` | `0.003s` | `0.004s` | `0.001s` | `0.022s` | `0.005s` | `0.005s` | `0.004s` | `0.001s` | `0.197s / 0.197s / 0.207s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `PFS3 fmt` | `ok` | `0.082s` | `0.001s` | `0.059s` | `0.037s` | `0.005s` | `0.009s` | `0.008s` | `0.008s` | `0.031s` | `0.002s` | `0.063s` | `0.007s` | `0.008s` | `0.018s` | `0.002s` | `0.339s / 0.339s / 0.346s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `SFS fmt` | `ok` | `0.080s` | `0.001s` | `0.061s` | `0.059s` | `0.007s` | `0.010s` | `0.007s` | `0.004s` | `0.013s` | `0.002s` | `0.080s` | `0.009s` | `0.009s` | `0.010s` | `0.002s` | `0.351s / 0.356s / 0.357s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |

One runtime quirk matters here: `SFS` crashes if the formatter bridge is
driven through a post-format uninhibit cycle after `ACTION_FORMAT`
already succeeded, while classic DOS filesystems still need that
uninhibit before the next mount sees a usable freshly formatted volume.

## Large Image Smoke

Run:

```sh
python3 tools/amifuse_matrix.py --fixtures pfs3-4g --runs 1 --json
```

Date: `2026-04-04`

This is the first ephemeral `>4GB` smoke case. It creates a sparse `5GiB`
RDB image, places a small `PFS3` partition at byte `4,644,864,000`, formats
that partition, writes deterministic data, remounts, reads it back, verifies
it, and then removes the image immediately after the run.

| FS | Status | Image size | Partition start | Create img | Inspect | Format | Init | Root | Mkdir | Create | Write | Rename | Flush | Remount | Verify stat | Verify read | Delete | Cleanup flush | Total |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `PFS3 >4G` | `ok` | `5GiB sparse` | `4,644,864,000` | `0.090s` | `0.001s` | `0.078s` | `0.062s` | `0.007s` | `0.016s` | `0.020s` | `0.015s` | `0.019s` | `0.002s` | `0.074s` | `0.012s` | `0.024s` | `0.011s` | `0.002s` | `0.434s` |

This case is intentionally not part of the default matrix run. It is meant to
exercise large-offset image I/O without keeping a persistent multi-gigabyte
fixture on disk.
