# Shared vamos disk sessions

Status: implemented by AmiFUSE PR #55 and
[amitools PR #2](https://github.com/reinauer/amitools/pull/2), merged on
2026-09-06 and initially published as `amitools-amifuse 0.8.0.post7`.
AmiFUSE requires `amitools-amifuse 0.8.0.post8` or later to include the
shared Windows image-buffering fix.
This records the implemented boundary and policies, replacing the original
staged migration plan.

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

Migrating the remaining image-detection or runtime-startup code would be
separate work. Neither is required to share the disk device implementation.

## Image ownership

Every handler-backed image session takes an exclusive host-image lock,
including read-only sessions. The lock covers the whole image, so separate
partitions cannot be opened by separate sessions concurrently. A mounted
image also cannot be opened through another handler-backed `ls` or `hash`
command. Read files through the existing mount, or finish the first session
before opening another.

This deliberately retains one owner per image across AmiFUSE and vamos.
Shared read-only sessions could be added as a separate locking policy;
they are not part of this migration. Host locks coordinate cooperating
applications and do not prevent unrelated tools from modifying the image.

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
`test_driver_runtime.py` checks shared locking and first/last-block I/O;
`test_handler_lifecycle.py` checks failure cleanup and exception precedence.

Handler integration and mount tests remain necessary to exercise real
filesystem binaries. The mount/hash comparison discovers a readable file
and computes its reference hash before mounting, respecting image ownership.
See [TESTING.md](../TESTING.md) for the handler, writable, and format matrices.
