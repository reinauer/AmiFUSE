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

The workflow (`.github/workflows/ci.yml`) has five jobs, each run on
Linux, macOS, and Windows:

- **Installed wheel** -- Python 3.13. Builds the sdist, then the wheel
  from it, and installs the wheel with the declared minimum amitools
  release. Checks that both carry `LICENSE`, and checks the installed
  package from outside the checkout, including the bundled
  FastFileSystem.
- **Unit tests** -- Python 3.11--3.13. No external dependencies.
- **Integration tests** -- Python 3.13. Clones AmiFUSE-testing for
  fixtures. Exercises the full m68k emulation stack.
- **Tools smoke** -- Python 3.13. Same fixtures. Runs
  `amifuse_matrix.py` and `image_format_smoke.py` as CI-visible smoke
  checks.
- **Mount tests** -- Python 3.13. Same fixtures. Installs a FUSE
  backend (fuse3, FUSE-T, or WinFSP) and runs the `fuse`-marked
  integration tests.

Integration tests, tools smoke, and mount tests start once the unit
tests pass. On Windows they install the `machine68k-amifuse` fork,
which carries Windows fixes that upstream machine68k lacks
([cnvogelg/machine68k#8](https://github.com/cnvogelg/machine68k/issues/8),
[cnvogelg/machine68k#9](https://github.com/cnvogelg/machine68k/issues/9)).

## Pull Requests

- One logical change per PR
- All CI checks should pass (or failures should be pre-existing)
- Keep the scope focused -- small, reviewable diffs are easier to merge

## Platform-Specific Entry Points

Platform-specific entry points (e.g. Windows-only `gui-scripts`) must guard
at the top of their entry function with
`if not sys.platform.startswith("win"): raise SystemExit(...)` so they fail
cleanly when invoked on another platform.
