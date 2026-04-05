"""Unit tests for amifuse.startup_runner module."""

import inspect
import pytest


class TestProcessStackBase:
    """Verify pr_StackBase initialization in _create_process().

    Source inspection tests: fragile by design, to be replaced with
    functional tests when integration test infrastructure is available (Phase 5).
    """

    def test_pr_stackbase_is_stack_upper(self):
        """Verify pr_StackBase is set to the stack upper bound (top of stack)."""
        from amifuse.startup_runner import HandlerLauncher
        source = inspect.getsource(HandlerLauncher._create_process)
        assert 'pr_StackBase", stack.get_upper()' in source
        assert 'pr_StackBase", stack.get_lower()' not in source
