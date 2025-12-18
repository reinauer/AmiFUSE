import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AMITOOLS_PATH = REPO_ROOT / "amitools"

# Prefer local checkout of amitools if it is not installed
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(AMITOOLS_PATH) not in sys.path:
    sys.path.insert(0, str(AMITOOLS_PATH))

from amitools.binfmt.BinFmt import BinFmt  # type: ignore  # noqa: E402
from amitools.binfmt.Relocate import Relocate  # type: ignore  # noqa: E402

def describe_driver(path: Path, base_addr: int = 0x100000, padding: int = 0):
    bin_img = BinFmt().load_image(str(path))
    if bin_img is None:
        raise FileNotFoundError(f"Unable to load binary image at {path}")

    relocator = Relocate(bin_img)
    sizes = relocator.get_sizes()
    addrs = relocator.get_seq_addrs(base_addr, padding=padding)

    # Relocate once to ensure the image is internally consistent.
    datas = relocator.relocate(addrs)

    segments = []
    for i, seg in enumerate(bin_img.get_segments()):
        data = datas[i]
        segments.append(
            {
                "id": i,
                "type": seg.get_type_name(),
                "size": seg.get_size(),
                "flags": seg.flags,
                "relocs_to": [
                    target.id for target in seg.get_reloc_to_segs()
                ],
                "first_bytes": data[:16].hex(),
            }
        )

    footprint = 0
    if addrs:
        last_seg = segments[-1]
        footprint = (addrs[-1] + last_seg["size"]) - addrs[0]

    return {
        "path": str(path),
        "segment_count": len(segments),
        "relocated_base": hex(addrs[0]) if addrs else None,
        "relocated_footprint_bytes": footprint,
        "segments": segments,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Inspect an Amiga filesystem binary and verify it relocates."
    )
    parser.add_argument("binary", type=Path, help="Path to the filesystem binary")
    parser.add_argument(
        "--base",
        type=lambda x: int(x, 0),
        default=0x100000,
        help="Base address to use when relocating (default: 0x100000)",
    )
    parser.add_argument(
        "--padding",
        type=int,
        default=0,
        help="Optional padding between segments when relocating",
    )
    args = parser.parse_args(argv)

    info = describe_driver(args.binary, base_addr=args.base, padding=args.padding)

    print(f"Binary: {info['path']}")
    print(
        f"Segments: {info['segment_count']}  "
        f"base={info['relocated_base']}  "
        f"footprint={info['relocated_footprint_bytes']} bytes"
    )
    for seg in info["segments"]:
        relocs = ",".join(map(str, seg["relocs_to"])) or "-"
        print(
            f"  #{seg['id']:02d} {seg['type']:<8} "
            f"size={seg['size']:>6} flags={seg['flags']:>2} "
            f"relocs->{relocs} first16={seg['first_bytes']}"
        )


if __name__ == "__main__":
    main()
