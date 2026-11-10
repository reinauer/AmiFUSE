"""Quiet runtime logging must preserve errors and instruction tracing."""
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from amitools.vamos.log import loggers, log_exec, log_machine
from amifuse import vamos_runner


@pytest.fixture(autouse=True)
def restore_levels():
    levels = [(logger, logger.level) for logger in loggers]
    yield
    for logger, level in levels:
        logger.setLevel(level)


@pytest.mark.parametrize("debug", [False, True])
def test_warning_policy_keeps_errors(caplog, debug):
    vamos_runner._configure_vamos_logging(debug)
    log_exec.warning("teardown warning")
    log_exec.error("runtime failure")
    assert ("teardown warning" in caplog.text) == debug
    assert "runtime failure" in caplog.text


def test_trace_keeps_machine_info_without_enabling_teardown_warnings(monkeypatch):
    runtime = vamos_runner.VamosHandlerRuntime()
    runtime.machine = object()
    runtime.trace_mgr = SimpleNamespace(setup_cpu_instr_trace=Mock())
    monkeypatch.setattr(vamos_runner, "_ensure_vamos_logging",
                        lambda levels: vamos_runner._configure_vamos_logging(True))
    runtime.enable_trace()
    assert log_machine.isEnabledFor(logging.INFO)
    assert not log_exec.isEnabledFor(logging.WARNING)
    assert log_exec.isEnabledFor(logging.ERROR)
    runtime.trace_mgr.setup_cpu_instr_trace.assert_called_once_with(False)
