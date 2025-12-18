# amifuse

Mount Amiga filesystem images on macOS/Linux using native AmigaOS filesystem handlers via FUSE.

amifuse runs actual Amiga filesystem drivers (like PFS3) through m68k CPU emulation, allowing you to read Amiga hard disk images without relying on reverse-engineered implementations.

## Requirements

- **macOS**: [macFUSE](https://osxfuse.github.io/) (or FUSE for Linux)
- **fusepy**: `pip install fusepy`
- **Python 3.10+**
- **amitools**: Plus a patch included in this repository
- A **filesystem**: e.g.  [pfs3aio](https://aminet.net/package/disk/misc/pfs3aio)
## Usage

```bash
python3 -m amifuse.fuse_fs \
    --driver pfs3aio \
    --image /path/to/disk.hdf \
    --mountpoint ./mnt
```

### Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `--driver` | Yes | Path to the Amiga filesystem handler binary (e.g., `pfs3aio`) |
| `--image` | Yes | Path to the Amiga hard disk image file |
| `--mountpoint` | Yes | Directory where the filesystem will be mounted |
| `--block-size` | No | Override block size (default: auto-detect or 512) |
| `--volname` | No | Override the volume name shown in Finder |
| `--debug` | No | Enable debug logging of FUSE operations |

### Example

```bash
# Create mount point
mkdir -p ./mnt

# Mount a PFS3 formatted disk image
python3 -m amifuse.fuse_fs \
    --driver pfs3aio \
    --image ~/Documents/FS-UAE/Hard\ Drives/pfs.hdf \
    --mountpoint ./mnt

# Browse the filesystem
ls ./mnt
find ./mnt -type f

# Unmount when done (Ctrl+C in the terminal, or:)
umount ./mnt
```

## Supported Filesystems

Currently tested with:
- **PFS3** (Professional File System 3) via `pfs3aio` handler

Other Amiga filesystem handlers may work but have not been tested.

## Notes

- The filesystem is mounted **read-only**
- The mount runs in the foreground; press Ctrl+C to unmount
- macOS Finder/Spotlight indexing is automatically disabled to improve performance
- First directory traversal may be slow as the handler processes each path; subsequent accesses are cached

## Troubleshooting

**Slow Filesystem access**
Yes, this code is incredibly slow. Please help me make it faster.

**"Mountpoint is already a mount"**
```bash
umount -f ./mnt
```

**High CPU usage**
This can happen when Finder or Spotlight are indexing the mount. The filesystem automatically rejects macOS metadata queries, but initial indexing attempts may still occur.

**Permission denied**
Ensure macFUSE is installed and your user has permission to use FUSE mounts.
