"""Hardware-neutral lighting description and the adapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

RGB = tuple[int, int, int]
ZONES = 4

# "breath" is the hardware's own slow fade in/out; "off" is all zones black.
MODES = ("static", "breath", "wave", "smooth", "off")


def parse_hex(value: str) -> RGB:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"expected #RRGGBB, got {value!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def to_hex(rgb: RGB) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def scale(rgb: RGB, percent: int) -> RGB:
    factor = max(0, min(100, percent)) / 100
    return tuple(round(c * factor) for c in rgb)  # type: ignore[return-value]


@dataclass(frozen=True)
class Effect:
    mode: str = "static"
    colors: tuple[RGB, RGB, RGB, RGB] = ((255, 255, 255),) * ZONES
    level: int = 2  # hardware brightness: 1 = low, 2 = high
    speed: int = 1  # 1 (slowest) .. 4 (fastest); breath / wave / smooth only
    direction: str = "ltr"  # wave only: "ltr" or "rtl"

    @classmethod
    def solid(cls, color: RGB, *, mode: str = "static", level: int = 2, speed: int = 1) -> Effect:
        return cls(mode=mode, colors=(color,) * ZONES, level=level, speed=speed)

    def describe(self) -> str:
        if self.mode in ("wave", "smooth"):
            extra = f" {self.direction}" if self.mode == "wave" else ""
            return f"{self.mode}{extra} speed {self.speed} level {self.level}"
        if self.mode == "off":
            return "off"
        unique = list(dict.fromkeys(self.colors))
        colors = to_hex(unique[0]) if len(unique) == 1 else "/".join(to_hex(c) for c in self.colors)
        speed = f" speed {self.speed}" if self.mode == "breath" else ""
        return f"{self.mode} {colors} level {self.level}{speed}"


class AdapterError(Exception):
    """The device is missing, busy or rejected a write."""


@dataclass(frozen=True)
class DeviceInfo:
    id: str  # stable identifier stored in the config
    name: str
    detail: str
    supported: bool
    note: str = ""


class RgbAdapter(ABC):
    """One family of devices. Only the device passed to open() is ever written."""

    name: str = "adapter"

    @abstractmethod
    def scan(self) -> list[DeviceInfo]: ...

    @abstractmethod
    def open(self, device_id: str) -> DeviceInfo: ...

    @abstractmethod
    def apply(self, effect: Effect) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...
