"""Keep successful commands printable through legacy text encodings."""

import sys


def configure_text_output():
    # Preserve the stream's encoding and binary buffer. Only names that
    # cannot be represented need escaping in the human-readable output.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
