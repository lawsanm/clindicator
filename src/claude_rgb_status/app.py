"""Run the background tray app."""

from __future__ import annotations

import ctypes
import logging
import logging.handlers
import os
import signal
import threading

from . import DISPLAY_NAME, __version__, paths
from . import config as config_mod

log = logging.getLogger(__name__)
_ERROR_ALREADY_EXISTS = 183


def setup_logging() -> None:
    """Rotating log of status metadata only (events, states, device errors)."""
    paths.log_dir().mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        paths.log_dir() / "claude-rgb-status.log", maxBytes=512_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def message_box(text: str, title: str = DISPLAY_NAME) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)


def acquire_single_instance():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    name = f"Local\\ClaudeRGBStatus-{os.environ.get('USERNAME', 'user')}"
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle or ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        return None
    return handle  # keep a reference for the process lifetime


def run() -> int:
    paths.ensure_dirs()
    setup_logging()
    mutex = acquire_single_instance()
    if mutex is None:
        message_box(f"{DISPLAY_NAME} is already running (see the tray).")
        return 1

    import tkinter as tk

    from .core import Core
    from .tray import Tray
    from .ui import UI

    log.info("%s %s starting", DISPLAY_NAME, __version__)
    cfg = config_mod.load(paths.config_path())
    core = Core(cfg)
    try:
        core.start()
    except OSError as exc:
        log.exception("startup failed")
        message_box(f"{DISPLAY_NAME} could not start:\n{exc}")
        return 1

    root = tk.Tk()
    root.withdraw()
    root.title(DISPLAY_NAME)
    ui = UI(root, core)
    tray = Tray(core, ui)
    done = threading.Event()

    def shutdown() -> None:
        if done.is_set():
            return
        done.set()
        log.info("shutting down; restoring lighting")
        tray.stop()
        core.shutdown()

    ui.on_quit = shutdown
    core.on_quit_request = ui.quit
    # Logoff / shutdown: restore the lighting before Windows ends the process (best effort).
    root.protocol("WM_SAVE_YOURSELF", lambda: (shutdown(), root.quit()))
    signal.signal(signal.SIGINT, lambda *_: ui.quit())
    tray.start()
    tray.refresh()
    try:
        root.mainloop()
    finally:
        shutdown()
    return 0
