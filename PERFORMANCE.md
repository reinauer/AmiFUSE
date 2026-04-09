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

## Current Matrix

The integration runner is [`tools/amifuse_matrix.py`](/Users/stepan/git/AmiFuse/tools/amifuse_matrix.py).
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
python3 tools/amifuse_matrix.py --json
```

Date: `2026-04-06`

| FS | Status | Total min / med / max | Notes |
| --- | --- | --- | --- |
| `PFS3` | `ok` | `0.098s / 0.100s / 0.100s` | `runs=3`, `pfs.hdf`, `PDH0` |
| `SFS` | `ok` | `0.075s / 0.075s / 0.076s` | `runs=3`, `sfs.hdf`, `SDH0` |
| `FFS` | `ok` | `0.571s / 0.574s / 0.576s` | `runs=3`, `Default.hdf`, `QDH0` |
| `OFS` | `ok` | `0.037s / 0.037s / 0.037s` | `runs=3`, `ofs.adf` |
| `BFFS` | `ok` | `0.200s / 0.213s / 0.215s` | `runs=3`, `netbsdamiga92.hdf`, `netbsd-root` |
| `CDFileSystem` | `ok` | `0.069s / 0.069s / 0.069s` | `runs=3`, `AmigaOS3.2CD.iso` |

## Latest Writable Run

Run:

```sh
python3 tools/amifuse_matrix.py --fixtures ofs-rw ffs-rw pfs3-rw sfs-rw --runs 3 --json
```

Date: `2026-04-06`

The writable smoke tests use scratch copies under
`~/AmigaOS/AmiFuse/generated/`, seeded from the canonical fixtures.

| FS | Status | Total min / med / max | Notes |
| --- | --- | --- | --- |
| `OFS rw` | `ok` | `0.039s / 0.044s / 0.079s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `FFS rw` | `ok` | `1.146s / 1.151s / 1.169s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `PFS3 rw` | `ok` | `0.196s / 0.196s / 0.198s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `SFS rw` | `ok` | `0.148s / 0.151s / 0.180s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |

## Latest Format Run

Run:

```sh
python3 tools/amifuse_matrix.py --fixtures ofs-fmt ffs-fmt pfs3-fmt sfs-fmt --runs 3 --json
```

Date: `2026-04-06`

The format smoke tests create fresh generated RDB images under
`~/AmigaOS/AmiFuse/generated/`, format them through AmiFuse, then run
the writable smoke sequence on the fresh volume.

| FS | Status | Total min / med / max | Notes |
| --- | --- | --- | --- |
| `OFS fmt` | `ok` | `0.106s / 0.107s / 0.108s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `FFS fmt` | `ok` | `0.100s / 0.100s / 0.102s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `PFS3 fmt` | `ok` | `0.235s / 0.237s / 0.248s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |
| `SFS fmt` | `ok` | `0.179s / 0.181s / 0.182s` | `runs=3`, verify=`/AmiFuseRW/hello-renamed.txt` |

## Latest Load Run

Run:

```sh
python3 tools/amifuse_matrix.py --fixtures pfs3-load sfs-load ffs-load --runs 3 --json
```

Date: `2026-04-06`

The load benchmark uses a single mount per run, creates one `1MiB`
file, creates `256` small files, lists the populated directory, then
reads the `1MiB` file `1600` times. `steady` excludes inspect/init and
captures the runtime-heavy portion.

| FS | Status | Steady min / med / max | Total min / med / max | Notes |
| --- | --- | --- | --- | --- |
| `PFS3 load` | `ok` | `10.516s / 10.542s / 10.554s` | `10.610s / 10.637s / 10.649s` | `runs=3`, `PDH0`, `1.6GiB` total reread |
| `SFS load` | `ok` | `6.203s / 6.220s / 6.418s` | `6.293s / 6.297s / 6.491s` | `runs=3`, `SDH0`, `1.6GiB` total reread |
| `FFS load` | `ok` | `8.409s / 8.513s / 8.620s` | `8.968s / 9.081s / 9.181s` | `runs=3`, `QDH0`, `1.6GiB` total reread |

## Large Image Smoke

Run:

```sh
python3 tools/amifuse_matrix.py --fixtures pfs3-4g pfs3-part-4g --runs 1 --json
```

Date: `2026-04-06`

These cases are intentionally separate from the default matrix. They
exercise large-offset and large-partition image I/O without keeping
persistent multi-gigabyte fixtures on disk.

| FS | Status | Image size | Partition detail | Total | Notes |
| --- | --- | --- | --- | --- | --- |
| `PFS3 >4G` | `ok` | `5GiB sparse` | start=`4,644,864,000` | `0.239s` | small `PFS3` partition above `4GiB` |
| `PFS3 partition >4G` | `ok` | `~6GiB sparse` | size=`4,386,816,000`, start=`1,032,192` | `0.619s` | large `PFS3` partition smoke |
