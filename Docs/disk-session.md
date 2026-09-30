# Shared vamos disk sessions

Status: implemented by
[amitools PR #2](https://github.com/reinauer/amitools/pull/2) (merged
2026-09-06, first published as `amitools-amifuse 0.8.0.post7`) and AmiFUSE
PR #55 (merged 2026-09-16). AmiFUSE needs `amitools-amifuse 0.8.0.post8`
or later for the shared Windows image-buffering fix; `pyproject.toml`
declares the current minimum. This records the implemented boundary and
policies, replacing the staged migration plan formerly in `API_CLEANUP.md`.

## Ownership and integration

Vamos owns `DiskSession`, host-image locking, canonical DOS structures,
and the shared `scsi.device` implementation. Standalone programs can use
`vamos --disk` for raw device access without starting a filesystem handler.
This also lets pfsdoctor inspect the final image from pfs3aio stress tests
without booting a complete emulator.

AmiFUSE retains image detection, its `BlockDeviceBackend`, handler loading
and startup, FUSE lifecycle, and DOS packet forwarding. It registers its
backend as unowned unit 0 in the shared session: the session routes I/O,
while AmiFUSE remains responsible for closing the backend. The
`amifuse.scsi_device` module re-exports the shared device and structures
for callers such as the pfs3aio fault-injection harness.

For RDB images, the backend also confines handler I/O to the selected
partition, or to the first partition when none is selected. Reads, and
writes on writable sessions, outside that partition complete with an I/O
error and leave the image unchanged. ADF and ISO sessions address the whole
image.

Migrating the remaining image-detection or runtime-startup code would be
separate work. Neither is required to share the disk device implementation.

## Image ownership

Every handler-backed session locks the host storage it uses, including
read-only sessions. For an RDB partition in a regular image file, the lock
covers only that partition's byte range: sessions on different,
nonoverlapping partitions can run concurrently, including with `--write`,
and read-only sessions can share a partition. A writable session excludes
all other access to its partition. Physical disks, ADFs, ISOs and
`vamos --disk` sessions keep one exclusive lock on the whole device or
image, so they also exclude partition sessions. On platforms without
byte-range locks, such as 32-bit Linux and the BSDs, every session keeps
one exclusive lock on the whole image, while handler I/O stays confined to
its partition.

While a writable session holds a partition, read its files through that
mount, and finish the session before running another handler-backed
command (`ls`, `hash`, `read`, `write`, `verify` or `format`) against it.
When another session holds a conflicting lock on an image file, `ls`,
`hash`, `read`, `write` and `verify` exit with an error naming the image or
partition; with `--json` they report the error code `IMAGE_IN_USE`. On a
Windows physical disk they keep the amitools lock error, reported as
`HANDLER_ERROR` with `--json`. `mount` and `format` print a plain error.
Host locks coordinate cooperating applications and do not prevent
unrelated tools from modifying the image.

Constructor failures, including `SystemExit`, release acquired resources.
Cleanup attempts every resource even when an earlier teardown fails. A
startup or open error remains the primary exception; normal explicit
shutdown reports the first cleanup error after attempting the rest.

## Device policies

AmiFUSE selects two compatibility policies on its runtime's `DiskSession`:

- Read-only writes acknowledge the requested transfer without changing the
  backend, preserving the historical handling of filesystem journal replay.
- Unsupported commands return success, preserving the historical handler
  compatibility behavior.

Other device semantics follow the shared implementation deliberately:

| Request | Behavior |
| --- | --- |
| `TD_FORMAT`, TD64 and NSD format | Write the requested buffer on writable images; acknowledge without writing on read-only images. |
| `TD_GETGEOMETRY` | Require a complete 32-byte output buffer. |
| `HD_SCSICMD` | Require a complete 30-byte `SCSICmd` structure. |
| Read/write, including TD64 and NSD | Reject unaligned offsets or lengths rather than truncating transfers. |
| SCSI READ(10) with a short data buffer | Return CHECK CONDITION rather than reporting a truncated read as successful. |
| Copied I/O requests | Preserve routing through `io_Unit` while the originating device open remains valid. Requests after close remain invalid. |

In particular, format requests now perform writes on writable images;
the old AmiFUSE device acknowledged them as a no-op. This is intentional
and covered explicitly, rather than treated as an incidental dependency
behavior. Read-only format requests still leave the image unchanged.

The bootstrap uses the canonical amitools DOS structures. Its scalar
startup field holds a BPTR value, and the global-vector BPTR uses
`0xFFFFFFFF` for the non-BCPL sentinel, matching the Amiga DOS layout.

## Verification

`tests/unit/test_runtime_disk_policy.py` exercises the factory registered
by the actual AmiFUSE runtime setup with a real `DiskSession` and emulated
request memory. It covers the policies and request validation above.
`test_driver_runtime.py` checks image and partition locking, confinement
of I/O to the selected partition, first/last-block I/O, and the
classification of lock conflicts;
`test_handler_lifecycle.py` checks failure cleanup and exception precedence.

Handler integration and mount tests remain necessary to exercise real
filesystem binaries. The mount/hash comparison discovers a readable file
and computes its reference hash before mounting.
See [TESTING.md](../TESTING.md) for the handler, writable, and format matrices.
