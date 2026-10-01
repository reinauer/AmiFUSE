"""File probes compatible with the published amitools dependency floor."""

import os

try:
    from amitools.util.Win32Disk import image_size, is_windows_disk, open_image
except ModuleNotFoundError as exc:
    if exc.name != "amitools.util.Win32Disk":
        raise

    # Physical disks require the newer source checkout. Ordinary images keep
    # working with amitools-amifuse 0.8.0.post8, including installed wheels.
    def is_windows_disk(path):
        return False

    def open_image(path):
        return open(path, "rb")

    def image_size(path):
        return os.path.getsize(path)
