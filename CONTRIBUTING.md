# Contributing

Thanks for your interest in AmiFUSE. This guide covers the minimum
needed to clone, build, test, and submit changes.

## Prerequisites

- **Python 3.11+** for development (`pyproject.toml` declares `>=3.9`
  for runtime compatibility, but CI tests 3.11 through 3.13)
- **Git** with submodule support
- Optional: [macFUSE](https://osxfuse.github.io/) /
  [FUSE for Linux](https://github.com/libfuse/libfuse) /
  [WinFSP](https://winfsp.dev/) (only needed for live mount testing)

## Clone and Setup

```sh
git clone --recurse-submodules https://github.com/reinauer/AmiFUSE.git
cd AmiFUSE
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e "./amitools[vamos]"
pip install "pytest>=7.0" "pytest-cov>=4.0" "pytest-timeout>=2.0"
pip install -e "." --no-deps
```

## Running Tests

```sh
# Quick check (all platforms, no fixtures needed)
pytest tests/unit/ -v --timeout=30
```

See [TESTING.md](TESTING.md) for integration tests, tools smoke, and
the full test matrix.

## Test Fixtures

Integration tests resolve the fixture directory through a cascade:

| Priority | Source | How |
|----------|--------|-----|
| 1 | `AMIFUSE_FIXTURE_ROOT` env var | Point at any directory with `drivers/` and `fixtures/readonly/` |
| 2 | `../AmiFUSE-testing` sibling checkout | Clone [AmiFUSE-testing](https://github.com/reinauer/AmiFUSE-testing) next to AmiFUSE |
| 3 | `~/AmigaOS/AmiFuse` | Default local path |
| -- | None found | Integration tests skip gracefully |

The fixture directory should contain:

```
<fixture-root>/
├── drivers/
│   ├── pfs3aio
│   ├── SmartFilesystem
│   ├── FastFileSystem
│   ├── BFFSFilesystem
│   └── ...
└── fixtures/
    └── readonly/
        ├── pfs.hdf
        ├── sfs.hdf
        ├── ofs.adf
        └── ...
```

## How CI Works

The workflow (`.github/workflows/ci.yml`) has three layers:

- **Unit tests** -- All platforms (Linux, macOS, Windows) x Python
  3.11--3.13. No external dependencies.
- **Integration tests** -- Linux + macOS only. Clones AmiFUSE-testing
  for fixtures. Exercises the full m68k emulation stack.
- **Tools smoke** -- Linux + macOS only. Same fixtures. Runs
  `amifuse_matrix.py` and `image_format_smoke.py` as CI-visible smoke
  checks.

Windows is excluded from integration and smoke jobs pending upstream
machine68k fixes
([cnvogelg/machine68k#8](https://github.com/cnvogelg/machine68k/issues/8),
[cnvogelg/machine68k#9](https://github.com/cnvogelg/machine68k/issues/9)).

## Pull Requests

- One logical change per PR
- All CI checks should pass (or failures should be pre-existing)
- Keep the scope focused -- small, reviewable diffs are easier to merge
