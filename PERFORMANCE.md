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

- `PFS3`: `fixtures/readonly/pfs.hdf` with `drivers/pfs3aio`
- `SFS`: `fixtures/readonly/sfs.hdf` with `drivers/SmartFilesystem`
- `FFS`: `fixtures/readonly/Default.hdf` with
  `drivers/FastFileSystem`
- `OFS`: `fixtures/readonly/ofs.adf` with `drivers/FastFileSystem`
- `BFFS`: `fixtures/downloaded/netbsdamiga92.hdf` with
  `drivers/BFFSFilesystem`
- `CDFileSystem`: `fixtures/readonly/AmigaOS3.2CD.iso` with
  `drivers/CDFileSystem`

If the NetBSD `BFFS` image is missing, the matrix downloads the
compressed aminet payload and decompresses it before running the `bffs`
fixture.

The `FFS` canonical image `fixtures/readonly/Default.hdf` also has an
on-demand compressed source, so the read-only and writable `FFS`
fixtures no longer require that image to be checked in locally.

There is also a dedicated `ODFileSystem` smoke fixture using:

- `fixtures/readonly/AmigaOS3.2CD.iso`
- `~/git/xcdfs/build/amiga/ODFileSystem`

It is not part of the default read-only matrix yet, because it is still
the most experimental ISO handler path.

## Latest Read-only Run

Run:

```sh
python3 tools/amifuse_matrix.py
```

Date: `2026-04-06`

| FS | Status | Inspect med | Init med | Root med | Stat med | Small med | Large med | Flush med | Total min / med / max | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `PFS3` | `ok` | `0.005s` | `0.077s` | `0.009s` | `0.001s` | `0.005s` | `0.002s` | `0.002s` | `0.099s / 0.100s / 0.103s` | `runs=3`, `pfs.hdf`, `PDH0`, small=`/foo.md`, large=`/S/pci.db` |
| `SFS` | `ok` | `0.007s` | `0.055s` | `0.007s` | `0.002s` | `0.003s` | `0.003s` | `0.001s` | `0.076s / 0.077s / 0.081s` | `runs=3`, `sfs.hdf`, `SDH0`, lookup=`/Prefs`, small=`/Prefs/Asl`, large=`/System/Installer` |
| `FFS` | `ok` | `0.002s` | `0.528s` | `0.031s` | `0.001s` | `0.001s` | `0.001s` | `0.000s` | `0.562s / 0.564s / 0.568s` | `runs=3`, `Default.hdf`, `QDH0`, small=`/CD0`, large=`/MMULib.lha` |
| `OFS` | `ok` | `0.000s` | `0.014s` | `0.001s` | `0.000s` | `0.001s` | `0.020s` | `0.000s` | `0.037s / 0.037s / 0.038s` | `runs=3`, `ofs.adf`, small=`/OFS_README.txt`, large=`/Docs/OFS_LARGE.bin` |
| `BFFS` | `ok` | `0.002s` | `0.188s` | `0.019s` | `0.002s` | `0.001s` | `0.001s` | `0.000s` | `0.212s / 0.212s / 0.218s` | `runs=3`, `netbsdamiga92.hdf`, `netbsd-root`, lookup=`/bin/cat`, small=`/.cshrc`, large=`/netbsd` |
| `CDFileSystem` | `ok` | `0.000s` | `0.059s` | `0.007s` | `0.001s` | `0.001s` | `0.001s` | `0.000s` | `0.067s / 0.069s / 0.071s` | `runs=3`, `AmigaOS3.2CD.iso`, small=`/CDVersion`, large=`/ADF/Backdrops3.2.adf` |

This is the current all-green aggregated read-only matrix for the
expanded canonical fixture set. The earlier single-run table overstated
drift, especially for `PFS3`, because its totals were too noisy to
compare from one sample.

`BFFS` now uses the NetBSD fixture directly in the default read-only
matrix. The key compatibility fix there was making AmiFuse-generated
BSTRs safe for handlers that temporarily treat counted strings as
NUL-terminated C strings.

## Latest Writable Run

Run:

```sh
python3 tools/amifuse_matrix.py --fixtures ofs-rw ffs-rw pfs3-rw sfs-rw --runs 3
```

Date: `2026-04-06`

The writable smoke tests use scratch copies under
`~/AmigaOS/AmiFuse/generated/`, seeded from the canonical fixtures.

| FS | Status | Inspect med | Init med | Root med | Mkdir med | Create med | Write med | Rename med | Flush med | Remount med | Verify stat med | Verify read med | Delete med | Cleanup flush med | Total min / med / max | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `OFS rw` | `ok` | `0.000s` | `0.016s` | `0.001s` | `0.001s` | `0.001s` | `0.005s` | `0.001s` | `0.000s` | `0.011s` | `0.001s` | `0.003s` | `0.001s` | `0.000s` | `0.041s / 0.042s / 0.043s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `FFS rw` | `ok` | `0.003s` | `0.532s` | `0.018s` | `0.002s` | `0.001s` | `0.001s` | `0.001s` | `0.000s` | `0.530s` | `0.004s` | `0.001s` | `0.001s` | `0.000s` | `1.088s / 1.095s / 1.128s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `PFS3 rw` | `ok` | `0.005s` | `0.076s` | `0.009s` | `0.005s` | `0.007s` | `0.002s` | `0.004s` | `0.001s` | `0.072s` | `0.003s` | `0.003s` | `0.005s` | `0.001s` | `0.194s / 0.196s / 0.196s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `SFS rw` | `ok` | `0.007s` | `0.054s` | `0.008s` | `0.004s` | `0.003s` | `0.001s` | `0.008s` | `0.001s` | `0.050s` | `0.004s` | `0.004s` | `0.004s` | `0.001s` | `0.146s / 0.146s / 0.147s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |

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

Date: `2026-04-06`

The format smoke tests create fresh generated RDB images under
`~/AmigaOS/AmiFuse/generated/`, format them through AmiFuse, then mount
them read-write, create and rename a file, remount, verify the contents,
delete the test file, and flush again.

| FS | Status | Create img med | Inspect med | Format med | Init med | Root med | Mkdir med | Create med | Write med | Rename med | Flush med | Remount med | Verify stat med | Verify read med | Delete med | Cleanup flush med | Total min / med / max | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `OFS fmt` | `ok` | `0.044s` | `0.005s` | `0.019s` | `0.014s` | `0.001s` | `0.002s` | `0.001s` | `0.003s` | `0.001s` | `0.000s` | `0.015s` | `0.001s` | `0.003s` | `0.001s` | `0.000s` | `0.107s / 0.114s / 0.114s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `FFS fmt` | `ok` | `0.049s` | `0.001s` | `0.018s` | `0.012s` | `0.001s` | `0.001s` | `0.001s` | `0.001s` | `0.001s` | `0.000s` | `0.012s` | `0.001s` | `0.004s` | `0.001s` | `0.000s` | `0.099s / 0.103s / 0.129s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `PFS3 fmt` | `ok` | `0.040s` | `0.003s` | `0.032s` | `0.067s` | `0.003s` | `0.004s` | `0.004s` | `0.003s` | `0.004s` | `0.001s` | `0.066s` | `0.004s` | `0.003s` | `0.005s` | `0.001s` | `0.237s / 0.240s / 0.263s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `SFS fmt` | `ok` | `0.041s` | `0.003s` | `0.036s` | `0.041s` | `0.002s` | `0.004s` | `0.003s` | `0.002s` | `0.005s` | `0.001s` | `0.037s` | `0.003s` | `0.006s` | `0.004s` | `0.001s` | `0.187s / 0.187s / 0.190s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |

One runtime quirk matters here: `SFS` crashes if the formatter bridge is
driven through a post-format uninhibit cycle after `ACTION_FORMAT`
already succeeded, while classic DOS filesystems still need that
uninhibit before the next mount sees a usable freshly formatted volume.

## Large Image Smoke

Run:

```sh
python3 tools/amifuse_matrix.py --fixtures pfs3-4g --runs 1 --json
python3 tools/amifuse_matrix.py --fixtures pfs3-part-4g --runs 1 --json
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

## Large Partition Smoke

Run:

```sh
python3 tools/amifuse_matrix.py --fixtures pfs3-part-4g --runs 1 --json
```

Date: `2026-04-04`

This case creates a sparse `~6GiB` image with a `PFS3` partition that
itself spans `4,386,816,000` bytes, formats it, mounts it read-write,
performs the normal writable smoke sequence, remounts, verifies the
written data, and removes the image afterward.

| FS | Status | Image size | Partition size | Partition start | Create img | Inspect | Format | Init | Root | Mkdir | Create | Write | Rename | Flush | Remount | Verify stat | Verify read | Delete | Cleanup flush | Total |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `PFS3 partition >4G` | `ok` | `~6GiB sparse` | `4,386,816,000` | `1,032,192` | `0.130s` | `0.002s` | `3.056s` | `0.125s` | `0.009s` | `0.084s` | `0.028s` | `0.010s` | `0.016s` | `0.027s` | `0.232s` | `0.012s` | `0.011s` | `0.069s` | `0.004s` | `3.814s` |

This closes the "filesystem spans a partition larger than `4GiB`" smoke
gap. One limitation remains: the current DOS file-handle seek/setsize
packet path is still `32-bit`, so this case does not yet verify file
offsets beyond `4GiB` within that large partition.
