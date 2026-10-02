"""Tray icon, tooltip, menu and desktop notifications (pystray)."""

from __future__ import annotations

import logging
import threading
import winsound
from dataclasses import replace

import pystray
from pystray import Menu, MenuItem

from . import DISPLAY_NAME, autostart, icons
from .config import STATE_LABELS, STATES
from .state import ERROR, FINISHED, PERMISSION, QUESTION

log = logging.getLogger(__name__)

SOUNDS = {
    PERMISSION: "SystemExclamation",
    QUESTION: "SystemExclamation",
    ERROR: "SystemHand",
    FINISHED: "SystemAsterisk",
}


class Tray:
    def __init__(self, core, ui):
        self.core = core
        self.ui = ui
        self.icon = pystray.Icon("ClaudeRGBStatus", icon=self._image(), title=DISPLAY_NAME,
                                 menu=Menu(self._items))
        core.on_change.append(self.refresh)
        core.on_notify = self.notify
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._thread = threading.Thread(target=self.icon.run, name="tray", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            log.debug("tray stop failed", exc_info=True)

    # ------------------------------------------------------------ rendering

    def _image(self):
        cfg = self.core.cfg
        state = self.core.shown_state
        style = cfg.styles[state]
        return icons.for_state(state, style.color, style.pulse, self.core.paused)

    def _tooltip(self) -> str:
        snap = self.core.snapshot()["display"]
        text = f"Claude: {snap['label']}"
        if self.core.paused:
            text = "Claude RGB Status: paused"
        if snap["session_label"]:
            text += f"\n{snap['session_label']}"
        if snap["detail"]:
            text += f"\n{snap['detail']}"
        return text[:127]  # Windows tooltip limit

    def refresh(self) -> None:
        try:
            self.icon.icon = self._image()
            self.icon.title = self._tooltip()
            self.icon.update_menu()
        except Exception:
            log.debug("tray refresh failed", exc_info=True)

    def notify(self, state: str, title: str, message: str) -> None:
        cfg = self.core.cfg
        if cfg.notifications:
            try:
                self.icon.notify(message[:250], f"Claude: {title}")
            except Exception:
                log.debug("notification failed", exc_info=True)
        if cfg.sound and state in SOUNDS:
            winsound.PlaySound(SOUNDS[state], winsound.SND_ALIAS | winsound.SND_ASYNC)

    # ------------------------------------------------------------ menu

    def _post(self, command, *args):
        return lambda: self.core.post(command, *args)

    def _set(self, **changes):
        return lambda: self.core.post("update_config", replace(self.core.cfg, **changes))

    def _toggle(self, name: str):
        return lambda: self.core.post("update_config", replace(self.core.cfg, **{name: not getattr(self.core.cfg, name)}))

    def _left_click(self) -> None:
        if self.core.shown_state in (FINISHED, ERROR):
            self.core.post("ack")
        else:
            self.ui.open("diagnostics")

    def _toggle_autostart(self) -> None:
        try:
            autostart.disable() if autostart.is_enabled() else autostart.enable()
        except OSError as exc:
            log.error("autostart change failed: %s", exc)
        self.refresh()

    def _quit(self) -> None:
        self.ui.quit()

    def _items(self):
        snap = self.core.snapshot()
        disp = snap["display"]
        cfg = self.core.cfg
        state = disp["state"]
        yield MenuItem(f"{'Paused · ' if snap['paused'] else ''}{disp['label']}", None, enabled=False)
        if disp["session_label"]:
            yield MenuItem(f"   {disp['session_label']}", None, enabled=False)
        if disp["detail"]:
            yield MenuItem(f"   {disp['detail'][:70]}", None, enabled=False)
        yield Menu.SEPARATOR
        yield MenuItem("Acknowledge" if state in (FINISHED, ERROR) else "Show diagnostics", self._left_click,
                       default=True)
        yield MenuItem("Clear stuck request", self._post("clear_pending"), visible=state in (PERMISSION, QUESTION))
        yield MenuItem("Resume lighting" if snap["paused"] else "Pause lighting", self._post("pause"))
        yield Menu.SEPARATOR
        yield MenuItem("Track session", Menu(lambda: self._track_items(snap)))
        yield MenuItem("Test colors", Menu(
            *[MenuItem(STATE_LABELS[s], self._post("test", s)) for s in STATES],
            MenuItem("Restore profile (pause/quit)", self._post("test", "restore")),
        ))
        yield MenuItem("Re-apply lighting", self._post("reapply"))
        yield Menu.SEPARATOR
        yield MenuItem("Desktop notifications", self._toggle("notifications"), checked=lambda _: cfg.notifications)
        yield MenuItem("Sound", self._toggle("sound"), checked=lambda _: cfg.sound)
        yield MenuItem("Start at login", self._toggle_autostart, checked=lambda _: autostart.is_enabled())
        yield Menu.SEPARATOR
        yield MenuItem("Settings…", lambda: self.ui.open("settings"))
        yield MenuItem("Diagnostics…", lambda: self.ui.open("diagnostics"))
        yield MenuItem("Quit", self._quit)

    def _track_items(self, snap: dict):
        cfg = self.core.cfg
        yield MenuItem("Most recent session", self._set(tracking="latest", pinned_session=""),
                       checked=lambda _: cfg.tracking == "latest", radio=True)
        yield MenuItem("All sessions (requests first)", self._set(tracking="aggregate", pinned_session=""),
                       checked=lambda _: cfg.tracking == "aggregate", radio=True)
        yield Menu.SEPARATOR
        live = [s for s in snap["sessions"] if s["live"]]
        if not live:
            yield MenuItem("No active Claude Code sessions", None, enabled=False)
        for s in live:
            sid = s["sid"]
            yield MenuItem(
                f"{s['label']}  —  {STATE_LABELS[s['state']]}",
                self._set(tracking="pinned", pinned_session=sid),
                checked=lambda _, sid=sid: cfg.tracking == "pinned" and cfg.pinned_session == sid,
                radio=True,
            )
