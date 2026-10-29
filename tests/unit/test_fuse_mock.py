"""Unit tests for the fuse_mock fixture in tests/conftest.py."""

import importlib.util
import sys


def test_fuse_fs_imports_fresh_under_fuse_mock(fuse_mock):
    """Every name fuse_fs imports from a stubbed module is in the stub.

    In a full run an earlier test has usually imported amifuse.fuse_fs
    before fuse_mock stubs its dependencies, which hides a missing name; a
    test file run on its own then fails with ImportError. Running the module
    code again against the stubs catches that in every run. The fresh copy
    is never put in sys.modules, so other tests keep the module they have.
    """
    spec = importlib.util.find_spec("amifuse.fuse_fs")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    stub = sys.modules["amifuse.startup_runner"]
    assert module.HandlerLauncher is stub.HandlerLauncher
