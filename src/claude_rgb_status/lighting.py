"""Keeps the selected keyboard showing the requested effect.

Runs on its own thread so a slow or stuck HID write never delays event handling.
Writes only when the effect changes (or after a reconnect / resume), coalesces
bursts, and reopens the device when a write fails (sleep, USB reset, unplug).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .rgb.base import AdapterError, DeviceInfo, Effect, RgbAdapter
from .rgb.null import NONE_DEVICE, NullAdapter

log = logging.getLogger(__name__)

MIN_WRITE_INTERVAL = 0.05
RETRY_INTERVAL = 3.0


def default_adapter_factory(allow_unverified: bool) -> RgbAdapter:
    from .rgb.legion4zone import Legion4ZoneAdapter

    return Legion4ZoneAdapter(allow_unverified=allow_unverified)


class LightingController(threading.Thread):
    def __init__(self, device: str = "auto", allow_unverified: bool = False,
                 adapter_factory: Callable[[bool], RgbAdapter] = default_adapter_factory):
        super().__init__(name="lighting", daemon=True)
        self._cv = threading.Condition()
        self._factory = adapter_factory
        self._device_setting = device
        self._allow_unverified = allow_unverified
        self._reconfigure = True
        self._force = False
        self._stopping = False
        self._final: Effect | None = None
        self._target: Effect | None = None
        self._target_label = ""
        self._applied: Effect | None = None
        self._next_attempt = 0.0
        self.adapter: RgbAdapter = NullAdapter()
        self.device: DeviceInfo | None = None
        self.connected = False
        self.last_error = ""
        self.writes = 0
        self.failures = 0
        self.last_write_at: float | None = None

    # ------------------------------------------------------------ public API

    def set_target(self, effect: Effect, label: str = "") -> None:
        with self._cv:
            if effect != self._target or label != self._target_label:
                self._target, self._target_label = effect, label
                self._cv.notify()

    def reapply(self) -> None:
        """Write the current effect again even if it didn't change (after sleep, or if
        other software overwrote the lighting)."""
        with self._cv:
            self._force = True
            self._cv.notify()

    def reopen(self) -> None:
        """Close and reopen the device, e.g. after resume when the old handle may be stale."""
        with self._cv:
            self._reconfigure = True
            self._cv.notify()

    def configure(self, device: str, allow_unverified: bool) -> None:
        with self._cv:
            if (device, allow_unverified) != (self._device_setting, self._allow_unverified):
                self._device_setting, self._allow_unverified = device, allow_unverified
                self._reconfigure = True
                self._cv.notify()

    def scan(self) -> list[DeviceInfo]:
        """Candidate devices for the settings window (doesn't touch the open device)."""
        try:
            found = self._factory(True).scan()
        except Exception as exc:  # scanning must never break the UI
            log.warning("device scan failed: %s", exc)
            found = []
        return found + [NONE_DEVICE]

    def shutdown(self, final_effect: Effect | None, timeout: float = 3.0) -> None:
        """Apply final_effect (the restore profile) and close the device."""
        with self._cv:
            self._final = final_effect
            self._stopping = True
            self._cv.notify()
        if self.is_alive():
            self.join(timeout)

    def status(self) -> dict:
        with self._cv:
            return {
                "adapter": self.adapter.name,
                "device": self.device.name if self.device else "",
                "device_id": self.device.id if self.device else "",
                "setting": self._device_setting,
                "connected": self.connected,
                "last_error": self.last_error,
                "writes": self.writes,
                "failures": self.failures,
                "last_write_at": self.last_write_at,
                "applied": self._applied.describe() if self._applied else "",
                "target": self._target_label,
            }

    # ------------------------------------------------------------ worker

    def _has_work(self) -> bool:
        if self._stopping or self._reconfigure or self._force:
            return True
        if not self.adapter.is_open:
            return time.monotonic() >= self._next_attempt
        return self._target is not None and self._target != self._applied

    def run(self) -> None:
        while True:
            with self._cv:
                if not self._has_work():
                    self._cv.wait(timeout=1.0)
                stopping, reconfigure, force = self._stopping, self._reconfigure, self._force
                self._reconfigure = self._force = False
                target = self._target
            if reconfigure:
                self._open()
            if stopping:
                self._finish()
                return
            if not self.adapter.is_open and time.monotonic() >= self._next_attempt:
                self._open()
            if target is not None and self.adapter.is_open and (force or target != self._applied):
                self._write(target)
                time.sleep(MIN_WRITE_INTERVAL)

    def _open(self) -> None:
        self.adapter.close()
        self._applied = None  # whatever was shown before must be written again
        self.connected = False
        try:
            if self._device_setting == "none":
                self.adapter = NullAdapter()
                self.device = self.adapter.open("none")
            else:
                self.adapter = self._factory(self._allow_unverified)
                self.device = self.adapter.open(self._device_setting)  # "auto" = first supported
            self.connected = True
            self.last_error = ""
            log.info("lighting output: %s", self.device.name)
        except (AdapterError, OSError) as exc:
            self.last_error = str(exc)
            self._next_attempt = time.monotonic() + RETRY_INTERVAL
            log.warning("keyboard unavailable: %s", exc)

    def _write(self, effect: Effect) -> bool:
        try:
            self.adapter.apply(effect)
        except (AdapterError, OSError) as exc:
            self.failures += 1
            self.connected = False
            self._applied = None
            self.last_error = str(exc)
            self._next_attempt = time.monotonic() + RETRY_INTERVAL
            log.warning("lighting write failed: %s", exc)
            return False
        self._applied = effect
        self.writes += 1
        self.last_write_at = time.time()
        return True

    def _finish(self) -> None:
        if self._final is not None:
            if not self.adapter.is_open:
                self._open()
            if self.adapter.is_open:
                self._write(self._final)
        self.adapter.close()
        self.connected = False
