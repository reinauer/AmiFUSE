"""System tray mount manager for AmiFUSE."""

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_CONSOLE = 0x00000010
_ERROR_ALREADY_EXISTS = 183


class TrayApp:
    POLL_INTERVAL = 2.5  # seconds
    GRACE_PERIOD = 10.0  # seconds after last mount disappears before auto-exit

    def __init__(self):
        self._icon = None
        self._lock = threading.Lock()
        self._mounts = []
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._grace_start = None

    def run(self):
        import pystray
        from PIL import Image

        icon_path = Path(os.environ.get("APPDATA", "")) / "AmiFUSE" / "icons" / "tray.ico"
        if not icon_path.exists():
            logger.error("Icon not found: %s", icon_path)
            sys.exit(1)

        icon_image = Image.open(str(icon_path))
        self._icon = pystray.Icon(
            "AmiFUSE", icon_image, "AmiFUSE", menu=self._build_menu()
        )

        poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        poll_thread.start()

        self._icon.run()
        # Unmount after icon.run() returns, not in _quit callback
        self._unmount_all()

    def _poll_loop(self):
        from .platform import find_amifuse_mounts

        prev_set = set()
        while not self._stop_event.is_set():
            mounts = find_amifuse_mounts()
            # Compare (pid, mountpoint) tuples to detect mountpoint changes
            current_set = {(m["pid"], m.get("mountpoint", "")) for m in mounts}

            if current_set != prev_set:
                prev_set = current_set
                with self._lock:
                    self._mounts = mounts
                self._icon.menu = self._build_menu()

            # Auto-exit grace period
            if not mounts:
                if self._grace_start is None:
                    self._grace_start = time.monotonic()
                elif time.monotonic() - self._grace_start > self.GRACE_PERIOD:
                    self._icon.stop()
                    return
            else:
                self._grace_start = None

            # Event.wait allows instant wakeup after unmount or quit
            self._wake_event.wait(self.POLL_INTERVAL)
            self._wake_event.clear()

        # _quit was called — stop the icon from this thread (safe, not in callback)
        self._icon.stop()

    def _build_menu(self):
        import pystray

        with self._lock:
            mounts = list(self._mounts)

        items = []
        for mount in mounts:
            mountpoint = mount.get("mountpoint", "?")
            image = mount.get("image")
            image_name = Path(image).name if image else "unknown"

            label = f"{mountpoint} - {image_name}"
            logger.info("Building submenu for %s (image=%s)", label, image)
            # Factory functions, not lambdas (pystray rejects lambdas with default args)
            items.append(pystray.MenuItem(
                label,
                pystray.Menu(
                    pystray.MenuItem("Inspect", self._make_inspect_cb(mount)),
                    pystray.MenuItem("Unmount", self._make_unmount_cb(mount)),
                ),
            ))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Unmount All", self._unmount_all_cb))
        items.append(pystray.MenuItem("Exit", self._quit))

        return pystray.Menu(*items)

    def _make_unmount_cb(self, mount):
        def cb(icon, item):
            try:
                logger.info("Unmount callback fired for %s", mount.get("mountpoint"))
                self._unmount_single(mount)
            except Exception:
                logger.exception("Unmount callback failed for %s", mount.get("mountpoint"))
        return cb

    def _make_inspect_cb(self, mount):
        def cb(icon, item):
            try:
                logger.info("Inspect callback fired for %s", mount.get("mountpoint"))
                self._inspect(mount)
            except Exception:
                logger.exception("Inspect callback failed for %s", mount.get("mountpoint"))
        return cb

    def _unmount_single(self, mount):
        from .platform import kill_pids

        kill_pids([mount["pid"]], timeout=2.0)
        self._wake_event.set()

    def _unmount_all(self):
        with self._lock:
            pids = [m["pid"] for m in self._mounts]
        if not pids:
            return
        from .platform import kill_pids

        kill_pids(pids, timeout=2.0)

    def _unmount_all_cb(self, icon, item):
        self._unmount_all()
        self._wake_event.set()

    def _inspect(self, mount):
        logger.info("_inspect called with mount=%s", mount)
        image_path = mount.get("image")
        if not image_path:
            logger.warning("Cannot determine image path for mount %s", mount)
            return

        abs_image_path = str(Path(image_path).resolve())
        logger.info("Resolved image path: %s", abs_image_path)
        # sys.executable may be pythonw.exe or amifuse-tray.exe (GUI subsystem),
        # which suppresses console output.  Find python.exe in the same directory
        # so that inspect output is visible in the new console window.
        python_dir = Path(sys.executable).parent
        python_exe = str(python_dir / "python.exe")
        if not os.path.isfile(python_exe):
            python_exe = sys.executable  # fallback
        # Launch python directly with CREATE_NEW_CONSOLE to avoid cmd.exe
        # metacharacter injection. The -c script uses sys.argv for the path
        # (never interpolated into code) and waits for a keypress so the user
        # can read output before the console closes.
        wrapper = [
            python_exe, "-c",
            "import sys; from amifuse.fuse_fs import main; main(['inspect'] + sys.argv[1:]); input('\\nPress Enter to close...')",
            abs_image_path,
        ]
        logger.info("Inspect: sys.executable=%s python_exe=%s wrapper=%s",
                     sys.executable, python_exe, wrapper)
        subprocess.Popen(wrapper, creationflags=_CREATE_NEW_CONSOLE)

    def _quit(self, icon, item):
        # Signal the poll loop to call icon.stop() — calling it directly from
        # a pystray callback deadlocks the Win32 message pump.
        self._stop_event.set()
        self._wake_event.set()


_mutex_handle = None
_kernel32 = None


def _check_single_instance() -> bool:
    """Return True if this is the only instance, False if another exists."""
    global _mutex_handle, _kernel32
    import ctypes

    if _kernel32 is None:
        _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    _mutex_handle = _kernel32.CreateMutexW(None, False, "Local\\AmiFUSE_Tray_Mutex")
    if not _mutex_handle:
        return False
    return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS


def main():
    log_dir = Path(os.environ.get("APPDATA", "")) / "AmiFUSE"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_dir / "tray.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not _check_single_instance():
        logger.info("Another tray instance is already running.")
        sys.exit(0)

    logger.info("Starting AmiFUSE tray.")
    app = TrayApp()
    app.run()
    logger.info("Tray exited.")


if __name__ == "__main__":
    main()
