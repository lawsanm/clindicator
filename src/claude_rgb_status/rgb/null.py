"""Tray-only mode: accepts effects and writes nothing."""

from __future__ import annotations

from .base import DeviceInfo, Effect, RgbAdapter

NONE_DEVICE = DeviceInfo(id="none", name="None (tray and notifications only)", detail="", supported=True)


class NullAdapter(RgbAdapter):
    name = "No keyboard output"

    def __init__(self) -> None:
        self._open = False
        self.last_effect: Effect | None = None

    def scan(self) -> list[DeviceInfo]:
        return [NONE_DEVICE]

    def open(self, device_id: str) -> DeviceInfo:
        self._open = True
        return NONE_DEVICE

    def apply(self, effect: Effect) -> None:
        self.last_effect = effect

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open
