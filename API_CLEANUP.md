# Disk Image Support for vamos

## Goal

Move generic disk-image access and Amiga device emulation out of AmiFUSE
and into amitools/vamos.

The result should let any Amiga program running under vamos access an HDF
through a normal Amiga device interface. AmiFUSE should consume that same
interface instead of maintaining a private vamos runtime and private disk
device implementation.

The initial end-to-end target is:

```sh
vamos --disk tests/.tmp/stress.hdf \
  pfsdoctor DH0 CHECK NONINTERACTIVE
```

This will allow the pfs3aio stress test to verify its final HDF with the
Amiga pfsdoctor binary without booting a complete emulator.

## Current Architecture

amitools already contains the basic disk-image primitives:

- `amitools/fs/blkdev/BlkDevFactory.py`
- `amitools/fs/blkdev/HDFBlockDevice.py`
- `amitools/fs/blkdev/RawBlockDevice.py`
- `amitools/fs/rdb/RDisk.py`

AmiFUSE currently contains most of the generic vamos integration:

- `amifuse/driver_runtime.py` maps disk images to block I/O.
- `amifuse/scsi_device.py` implements `scsi.device` commands.
- `amifuse/bootstrap.py` constructs `DosEnvec`,
  `FileSysStartupMsg`, and `DeviceNode` structures.
- `amifuse/rdb_inspect.py` contains additional image and partition
  detection.
- `amifuse/vamos_runner.py` duplicates part of the normal vamos startup
  sequence so AmiFUSE can register its device implementation.

This division makes raw disk access an AmiFUSE feature even though it is
not specific to FUSE. It also prevents other programs running under vamos,
such as pfsdoctor, from using the same implementation.

## Proposed Boundary

### Move to amitools/vamos

amitools/vamos should own:

- Parsing disk-image configuration.
- Opening HDF, RDB, ADF, and other supported image formats.
- Partition discovery and geometry.
- A generic disk backend with block reads, writes, flushing, and
  write protection.
- Native `scsi.device` emulation.
- TD64, NSD, Direct SCSI, and geometry commands supported by the current
  AmiFUSE implementation.
- Creation of `DosEnvec`, `FileSysStartupMsg`, and DOS device nodes.
- Registration of real device entries in the vamos DOS list.
- Disk ownership, exclusive access, cleanup, and error reporting.

Generic image-detection code from `amifuse/rdb_inspect.py` should be
upstreamed where it extends `BlkDevFactory`. AmiFUSE-specific reporting or
user-interface code should remain in AmiFUSE.

The current AmiFUSE classes should not necessarily be moved unchanged.
Their responsibilities should be separated into reusable block-device,
Amiga-device, and DOS-device layers.

### Keep in AmiFUSE

AmiFUSE should continue to own:

- Loading and starting filesystem handlers.
- DOS packet forwarding between a handler and FUSE operations.
- FUSE mount and unmount lifecycle.
- File and metadata translation required by the host filesystem.
- Platform-specific integration and the AmiFUSE command-line interface.

Once the common interface exists, AmiFUSE should create a normal vamos disk
session rather than registering a private `scsi.device` and duplicating the
vamos startup sequence.

## Command-Line Interface

`--volume` and `--disk` should have clearly different meanings:

```text
-V, --volume    map a host directory as an AmigaDOS volume
--disk          expose a raw disk image through an Amiga device
```

The first implementation can support one repeated option with sensible
defaults:

```sh
vamos --disk disk.hdf program arguments...
```

An RDB image should expose its partitions using their RDB names where
possible. Options for explicit device names, units, partitions, and write
protection can be added without changing the basic model. For example:

```sh
vamos --disk disk1.hdf --disk disk2.hdf program
```

A structured disk specification or configuration-file representation will
eventually be preferable to a large set of paired command-line options.
The exact syntax should be chosen while integrating with the existing
amitools configuration parsers.

`--disk` should not automatically load or start a filesystem handler. A raw
disk is useful independently of a mounted filesystem, particularly for
diagnostic and repair tools.

## Runtime Layers

### Disk image layer

This layer opens the host image and exposes:

- Logical block size and total size.
- Block reads and writes.
- Flush and close operations.
- Read-only and exclusive-open state.
- Partition offsets and geometry.

It should extend the existing amitools block-device classes rather than
introducing another unrelated image abstraction.

### Amiga device layer

This layer presents the disk backend as `scsi.device` to programs running
under vamos. It translates Amiga I/O requests to backend operations and
returns normal Amiga error codes.

The implementation must preserve the behavior already tested by AmiFUSE,
including boundary checks, TD64, NSD, Direct SCSI, and write protection.

Support for `trackdisk.device` can be added later if it is useful for ADF
images. It is not required for the first HDF and pfsdoctor milestone.

### DOS device layer

This layer creates and maintains actual DOS device entries. The existing
vamos DOS list currently creates host volumes and assigns, but does not
fully model disk devices.

The implementation needs to:

- Add `DosListDeviceStruct` entries.
- Allocate and populate `DosEnvec` and `FileSysStartupMsg`.
- Make `LockDosList()` and `FindDosEntry()` return the disk device.
- Make `MakeDosEntry()` and `AddDosEntry()` update the maintained list.
- Associate a DOS device with the correct image, partition, and unit.

This layer should not require a filesystem handler to be running merely to
describe a device.

### Disk session layer

A disk session owns all images used by one vamos invocation. It opens them
before the Amiga process starts, registers the required devices, and flushes
and closes them after the process exits.

Read-only mode should be available and should be the default for diagnostic
use until the write path is explicitly requested.

## Inhibit Semantics

pfsdoctor calls `Inhibit()` before accessing a volume. A pfsdoctor check
does not otherwise require a filesystem handler if the DOS device and raw
device interfaces are available.

For an exclusively opened disk session with no filesystem handler attached,
`Inhibit()` can safely succeed as a no-op. There is no handler or FUSE client
that can concurrently modify the image. This state should be represented
explicitly rather than pretending that a handler was suspended.

When AmiFUSE has attached a handler, `Inhibit()` must retain the real packet
forwarding behavior used today.

## pfsdoctor Requirements

Disk support alone is not enough to make pfsdoctor a reliable automated
checker.

### Headless operation

The current Amiga binary attempts to open `asl.library` even when invoked
with `NONINTERACTIVE`. Stock vamos therefore fails before it reaches the
disk scan.

A deterministic headless pfsdoctor build should compile out ASL requesters
and any interactive recovery paths that are invalid in a test. This is
preferable to providing a fake requester implementation that could hide an
unexpected interactive code path.

### Exit status

The current pfsdoctor main path ignores the result of `StandardScan()` and
eventually returns success. Automated use requires defined exit statuses,
including a nonzero result for:

- Filesystem errors reported by the scan.
- Fatal scan or I/O failures.
- Invalid command-line or device configuration.

Output can remain useful for diagnostics, but the test must not depend only
on parsing human-readable text.

## Migration Plan

### Phase 1: Read-only disk and device support

- Add disk configuration parsing to vamos.
- Reuse or extend `BlkDevFactory` for image opening.
- Move the generic block backend and `scsi.device` implementation into
  amitools.
- Add DOS device entries and startup structures.
- Support a read-only, exclusively opened HDF.
- Add unit tests for geometry, boundaries, device commands, and partition
  offsets.

The acceptance test is a small Amiga program that can find `DH0`, open
`scsi.device`, query geometry, and read known blocks.

### Phase 2: pfsdoctor vertical slice

- Add the required `Inhibit()` behavior for an exclusive unmounted disk.
- Produce a headless pfsdoctor binary.
- Give pfsdoctor reliable exit statuses.
- Run pfsdoctor against known-clean and deliberately corrupted fixtures.

The clean fixture must return zero. Each corrupted fixture must return a
nonzero status and an expected diagnostic.

### Phase 3: Writable disks

- Enable writes only when explicitly requested.
- Verify write protection and complete I/O boundary handling.
- Flush all modified data before process exit.
- Test TD64, NSD, Direct SCSI, partial-block rejection, and injected I/O
  failures.

### Phase 4: Refactor AmiFUSE

- Replace AmiFUSE's private block backend with the amitools disk session.
- Replace its private `scsi.device` registration.
- Remove duplicated vamos startup code where the public interface now
  covers it.
- Keep handler startup, packet forwarding, and FUSE behavior in AmiFUSE.
- Run the existing AmiFUSE test suite before removing compatibility code.

### Phase 5: pfs3aio stress-test integration

- Create and fill an HDF over multiple mutation passes.
- Run pfsdoctor through vamos against the final image.
- Require both content verification and a clean pfsdoctor result.
- Preserve the failed HDF and operation log when either check fails.

## Verification

The combined test coverage should include:

- Block reads at the beginning and end of a partition.
- Rejection of reads and writes outside the partition.
- Read-only images rejecting all writes.
- Native, TD64, NSD, and Direct SCSI access.
- RDB partition discovery and non-zero partition offsets.
- DOS device enumeration through normal AmigaDOS APIs.
- Correct cleanup after normal and abnormal process termination.
- Clean and corrupted pfsdoctor fixtures.
- AmiFUSE behavior before and after switching to the shared interface.
- A full pfs3aio stress run followed by content and metadata checks.

## Non-goals for the First Change

The initial amitools change should not attempt to:

- Move FUSE operations or packet bridging into amitools.
- Automatically mount every discovered filesystem.
- Implement every possible Amiga disk device.
- Add writable operation before the read-only path is proven.
- Combine the amitools, AmiFUSE, pfsdoctor, and stress-test changes into one
  large patch.

Keeping these steps separate makes the public interface testable before
AmiFUSE begins depending on it.
