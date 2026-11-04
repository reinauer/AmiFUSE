"""A handler's debug text reaches the terminal escaped (#361).

Handlers print names from the image through RawPutChar, a byte at a time, so
an escape sequence in a name must not reach the terminal that way either.
"""

import pytest

from amifuse.exec_library import AmiFuseExecLibrary


def _write(data):
    library = AmiFuseExecLibrary.__new__(AmiFuseExecLibrary)
    for value in data:
        library._raw_console_write(value)


@pytest.mark.parametrize("data, shown", [
    (b"plain text", "plain text"),
    (b"a\tb", "a\tb"),
    (b"\x1b[2J", "\\x1b[2J"),
    (b"\x9b2J", "\\x9b2J"),  # C1 CSI
    (b"\x7f", "\\x7f"),
    (b"\xfc", "\\xfc"),
    (b"\\x1b", "\\x5cx1b"),  # not to pass for an escaped ESC
    (b"a\0b", "ab"),  # amitools drops NUL
    (b"a\r\nb", "a\r\r\nb"),  # amitools puts CR before LF
])
def test_handler_text(capsys, data, shown):
    _write(data)

    assert capsys.readouterr().err == shown
