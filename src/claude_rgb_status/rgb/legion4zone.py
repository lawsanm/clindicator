"""Lenovo Legion / LOQ / IdeaPad Gaming 4-zone RGB keyboards (ITE controller, USB HID).

Protocol: one 33-byte HID feature report on the collection with usage page 0xFF89,
usage 0x00CC. This is the packet OpenRGB's Lenovo4ZoneUSBController sends to the
2020-2023 models and that L5P-Keyboard-RGB uses for 2020-2024 models, including
the 2024 "regular + slim" PID 0xC994:

    [0]  0xCC  report id      [1]  0x16  command
    [2]  effect (1 static, 3 breath, 4 wave, 6 smooth)
    [3]  speed 1..4           [4]  brightness 1..2
    [5:17] zone 0..3 RGB      [17] 0x00
    [18] wave left->right     [19] wave right->left      [20:33] zero

The controller cannot report its current lighting, so the app restores a
configured profile instead of the previous state.
"""

from __future__ import annotations

from .base import ZONES, AdapterError, DeviceInfo, Effect, RgbAdapter

VENDOR_ID = 0x048D  # Integrated Technology Express (ITE)
USAGE_PAGE = 0xFF89
USAGE = 0x00CC
REPORT_LEN = 33

# Product IDs with this exact interface, from OpenRGB's Lenovo 4-zone detector and
# L5P-Keyboard-RGB's device table. Other PIDs that expose the same interface are
# listed as unverified and are never written unless explicitly allowed.
KNOWN_PIDS: dict[int, str] = {
    0xC955: "Legion 2020",
    0xC963: "IdeaPad Gaming 2021",
    0xC965: "Legion 2021",
    0xC973: "IdeaPad Gaming 2022",
    0xC975: "Legion 2022",
    0xC983: "LOQ 2023",
    0xC984: "Legion 2023",
    0xC985: "Legion Pro 2023",
    0xC993: "LOQ 2024",
    0xC994: "Legion / Legion Slim 2024",
    0xC995: "Legion Pro 2024",
}

_EFFECT_CODES = {"static": 0x01, "breath": 0x03, "wave": 0x04, "smooth": 0x06, "off": 0x01}


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def build_packet(effect: Effect) -> bytes:
    if effect.mode not in _EFFECT_CODES:
        raise ValueError(f"unsupported mode {effect.mode!r}")
    if len(effect.colors) != ZONES:
        raise ValueError("expected 4 zone colors")
    packet = bytearray(REPORT_LEN)
    packet[0] = 0xCC
    packet[1] = 0x16
    packet[2] = _EFFECT_CODES[effect.mode]
    packet[3] = _clamp(effect.speed, 1, 4)
    packet[4] = _clamp(effect.level, 1, 2)
    if effect.mode in ("static", "breath"):
        for zone, rgb in enumerate(effect.colors):
            packet[5 + zone * 3 : 8 + zone * 3] = bytes(_clamp(c, 0, 255) for c in rgb)
    elif effect.mode == "wave":
        packet[18 if effect.direction != "rtl" else 19] = 0x01
    # "off": static with every zone black (the protocol has no brightness 0).
    return bytes(packet)


def _matches_interface(info) -> bool:
    return (
        info.vendor_id == VENDOR_ID
        and info.usage_page == USAGE_PAGE
        and info.usage == USAGE
        and info.feature_report_len == REPORT_LEN
    )


class Legion4ZoneAdapter(RgbAdapter):
    name = "Lenovo 4-zone keyboard (HID)"

    def __init__(self, allow_unverified: bool = False):
        self.allow_unverified = allow_unverified
        self._device = None
        self._info: DeviceInfo | None = None

    def _info_for(self, iface) -> DeviceInfo:
        model = KNOWN_PIDS.get(iface.product_id)
        return DeviceInfo(
            id=iface.instance_id,
            name=f"Lenovo 4-zone keyboard ({model or 'unverified model'})",
            detail=f"{iface.vendor_id:04X}:{iface.product_id:04X} rev {iface.version:04X} "
            f"'{iface.product}' page {iface.usage_page:04X} usage {iface.usage:04X}",
            supported=model is not None or self.allow_unverified,
            note="" if model else "PID not in the verified list",
        )

    def _interfaces(self) -> list:
        from . import hidwin

        return [i for i in hidwin.list_interfaces(VENDOR_ID) if _matches_interface(i)]

    def scan(self) -> list[DeviceInfo]:
        return [self._info_for(i) for i in self._interfaces()]

    def open(self, device_id: str) -> DeviceInfo:
        from . import hidwin

        self.close()
        interfaces = self._interfaces()
        if device_id == "auto":  # first supported keyboard
            interfaces = [i for i in interfaces if self._info_for(i).supported][:1]
        for iface in interfaces:
            if device_id not in ("auto", iface.instance_id):
                continue
            if iface.product_id not in KNOWN_PIDS and not self.allow_unverified:
                raise AdapterError(f"{iface.instance_id} has an unverified PID {iface.product_id:04X}")
            device = hidwin.HidDevice(iface.path)
            try:
                device.open()
            except OSError as exc:
                raise AdapterError(f"cannot open keyboard: {exc}") from exc
            self._device = device
            self._info = self._info_for(iface)
            return self._info
        raise AdapterError("no supported keyboard found" if device_id == "auto" else f"keyboard {device_id} not present")

    def apply(self, effect: Effect) -> None:
        if self._device is None:
            raise AdapterError("no keyboard open")
        try:
            self._device.set_feature(build_packet(effect))
        except OSError as exc:
            # Typical after sleep or a USB reset: the handle is stale.
            self.close()
            raise AdapterError(f"write failed: {exc}") from exc

    def close(self) -> None:
        if self._device is not None:
            self._device.close()
            self._device = None

    @property
    def is_open(self) -> bool:
        return self._device is not None

    @property
    def device_info(self) -> DeviceInfo | None:
        return self._info
