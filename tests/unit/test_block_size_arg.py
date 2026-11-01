"""--block-size takes only a power of two from 512 to 32768.

Argument parsing refuses any other value with exit status 2, before a
subcommand runs, for every amifuse subcommand that has the option and for
rdb-inspect.
"""
import argparse

import pytest

import amifuse.fuse_fs as fuse_fs
import amifuse.rdb_inspect as rdb_inspect

VALID = [512, 1024, 2048, 4096, 8192, 16384, 32768]
NOT_POWER_OF_TWO = "must be a power of two from 512 to 32768"
ZERO = "0 is not a block size; leave the option out to detect it"
INVALID = [("0", ZERO), ("-512", NOT_POWER_OF_TWO),
           ("256", NOT_POWER_OF_TWO), ("1000", NOT_POWER_OF_TWO)]

# Each subcommand with --block-size, its required arguments, and the
# function main() hands the parsed arguments to.
SUBCOMMANDS = [
    (["inspect", "x.hdf"], "cmd_inspect"),
    (["mount", "x.hdf"], "cmd_mount"),
    (["format", "x.hdf", "DH0"], "cmd_format"),
    (["ls", "x.hdf"], "cmd_ls"),
    (["verify", "x.hdf"], "cmd_verify"),
    (["hash", "x.hdf", "--file", "f"], "cmd_hash"),
    (["read", "x.hdf", "--file", "f"], "cmd_read"),
    (["write", "x.hdf", "--file", "f", "--in", "g"], "cmd_write"),
]
SUBCOMMAND_IDS = [argv[0] for argv, _ in SUBCOMMANDS]


@pytest.mark.parametrize("size", VALID)
def test_block_size_arg_accepts_supported_sizes(size):
    assert rdb_inspect.block_size_arg(str(size)) == size


@pytest.mark.parametrize("text, message", INVALID + [
    ("65536", NOT_POWER_OF_TWO), ("511", NOT_POWER_OF_TWO),
    ("abc", NOT_POWER_OF_TWO), ("", NOT_POWER_OF_TWO),
])
def test_block_size_arg_refuses_other_values(text, message):
    with pytest.raises(argparse.ArgumentTypeError, match=message):
        rdb_inspect.block_size_arg(text)


def _record(monkeypatch, module, name):
    calls = []
    monkeypatch.setattr(module, name, lambda *args, **kwargs: calls.append(args))
    return calls


@pytest.mark.parametrize("text, message", INVALID)
@pytest.mark.parametrize("argv, func", SUBCOMMANDS, ids=SUBCOMMAND_IDS)
def test_amifuse_refuses_block_size(monkeypatch, capsys, argv, func, text,
                                    message):
    calls = _record(monkeypatch, fuse_fs, func)
    with pytest.raises(SystemExit) as exc_info:
        fuse_fs.main(argv + [f"--block-size={text}"])
    assert exc_info.value.code == 2
    assert f"argument --block-size: {message}" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("size", VALID + [None])
@pytest.mark.parametrize("argv, func", SUBCOMMANDS, ids=SUBCOMMAND_IDS)
def test_amifuse_accepts_block_size(monkeypatch, argv, func, size):
    calls = _record(monkeypatch, fuse_fs, func)
    option = [] if size is None else ["--block-size", str(size)]
    fuse_fs.main(argv + option)
    assert [args.block_size for (args,) in calls] == [size]


@pytest.mark.parametrize("text, message", INVALID)
def test_rdb_inspect_refuses_block_size(monkeypatch, capsys, text, message):
    calls = _record(monkeypatch, rdb_inspect, "print_inspect_report")
    with pytest.raises(SystemExit) as exc_info:
        rdb_inspect.main(["x.hdf", f"--block-size={text}"])
    assert exc_info.value.code == 2
    assert f"argument --block-size: {message}" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("size", VALID + [None])
def test_rdb_inspect_accepts_block_size(monkeypatch, size):
    calls = _record(monkeypatch, rdb_inspect, "print_inspect_report")
    option = [] if size is None else ["--block-size", str(size)]
    rdb_inspect.main(["x.hdf"] + option)
    assert [args[1] for args in calls] == [size]
