"""User configuration: colors, effects, device choice and behavior.

Stored as JSON in %APPDATA%\\ClaudeRGBStatus\\config.json. Loading is tolerant:
unknown keys are ignored and invalid values fall back to defaults, so a bad edit
never stops the app from starting.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .rgb.base import MODES, ZONES, Effect, parse_hex, scale

log = logging.getLogger(__name__)

STATES = ("processing", "permission", "question", "finished", "error", "idle", "unknown")
STATE_LABELS = {
    "processing": "Processing",
    "permission": "Waiting for permission",
    "question": "Waiting for your input",
    "finished": "Finished responding",
    "error": "Error needs attention",
    "idle": "Connected, idle",
    "unknown": "Disconnected / unknown",
}
# Slow speeds only: the keyboard's faster breath settings get close to flashing.
MAX_PULSE_SPEED = 2
TRACKING_MODES = ("latest", "pinned", "aggregate")


@dataclass
class StateStyle:
    color: str
    intensity: int = 100  # percent of the color's RGB values (dims the color)
    pulse: bool = False  # hardware breath effect instead of a static color


def default_styles() -> dict[str, StateStyle]:
    return {
        "processing": StateStyle("#0040FF"),
        "permission": StateStyle("#FF9000", pulse=True),
        "question": StateStyle("#9000FF", pulse=True),
        "finished": StateStyle("#00FF30"),
        "error": StateStyle("#FF0000"),
        "idle": StateStyle("#FFFFFF", intensity=20),
        "unknown": StateStyle("#FF5000", intensity=25),
    }


@dataclass
class RestoreProfile:
    """Lighting applied when the app is paused or quits (the keyboard can't report its own state)."""

    mode: str = "static"
    colors: list[str] = field(default_factory=lambda: ["#FFFFFF"] * ZONES)
    level: int = 2
    speed: int = 2
    direction: str = "ltr"


@dataclass
class Config:
    device: str = "auto"  # "auto", "none", or a device id from the adapter
    level: int = 2  # hardware brightness for status colors: 1 low, 2 high
    pulse_speed: int = 1  # 1 slowest .. MAX_PULSE_SPEED
    styles: dict[str, StateStyle] = field(default_factory=default_styles)
    restore: RestoreProfile = field(default_factory=RestoreProfile)
    tracking: str = "latest"  # latest | pinned | aggregate
    pinned_session: str = ""
    no_session: str = "unknown"  # "unknown" shows dim orange, "restore" shows the restore profile
    attention_delay_ms: int = 300  # permission/question must persist this long before lighting up
    question_hint: bool = True  # mention "may contain a question" when a reply ends with '?'
    notifications: bool = True
    notify_states: list[str] = field(default_factory=lambda: ["permission", "question", "error"])
    sound: bool = False
    reapply_after_resume: bool = True
    allow_unverified_devices: bool = False
    udp_port: int = 47823


def _valid_color(value: object) -> bool:
    try:
        parse_hex(str(value))
        return True
    except ValueError:
        return False


def _coerce_style(raw: object, default: StateStyle) -> StateStyle:
    if not isinstance(raw, dict):
        return default
    color = raw.get("color", default.color)
    intensity = raw.get("intensity", default.intensity)
    pulse = raw.get("pulse", default.pulse)
    return StateStyle(
        color=str(color).upper() if _valid_color(color) else default.color,
        intensity=max(0, min(100, int(intensity))) if isinstance(intensity, (int, float)) else default.intensity,
        pulse=pulse if isinstance(pulse, bool) else default.pulse,
    )


def _coerce_restore(raw: object) -> RestoreProfile:
    default = RestoreProfile()
    if not isinstance(raw, dict):
        return default
    colors = raw.get("colors", default.colors)
    if isinstance(colors, str):
        colors = [colors] * ZONES
    if not (isinstance(colors, list) and len(colors) == ZONES and all(_valid_color(c) for c in colors)):
        colors = default.colors
    mode = raw.get("mode", default.mode)
    level = raw.get("level", default.level)
    speed = raw.get("speed", default.speed)
    direction = raw.get("direction", default.direction)
    return RestoreProfile(
        mode=mode if mode in MODES else default.mode,
        colors=[str(c).upper() for c in colors],
        level=level if level in (1, 2) else default.level,
        speed=speed if speed in (1, 2, 3, 4) else default.speed,
        direction=direction if direction in ("ltr", "rtl") else default.direction,
    )


def from_dict(raw: dict) -> Config:
    cfg = Config()
    defaults = default_styles()
    styles = raw.get("styles") if isinstance(raw.get("styles"), dict) else {}
    cfg.styles = {s: _coerce_style(styles.get(s), defaults[s]) for s in STATES}
    cfg.restore = _coerce_restore(raw.get("restore"))

    def pick(name, valid):
        if name in raw and valid(raw[name]):
            setattr(cfg, name, raw[name])

    pick("device", lambda v: isinstance(v, str) and v)
    pick("level", lambda v: v in (1, 2))
    pick("pulse_speed", lambda v: v in range(1, MAX_PULSE_SPEED + 1))
    pick("tracking", lambda v: v in TRACKING_MODES)
    pick("pinned_session", lambda v: isinstance(v, str))
    pick("no_session", lambda v: v in ("unknown", "restore"))
    pick("attention_delay_ms", lambda v: isinstance(v, int) and 0 <= v <= 5000)
    pick("question_hint", lambda v: isinstance(v, bool))
    pick("notifications", lambda v: isinstance(v, bool))
    pick("notify_states", lambda v: isinstance(v, list) and all(s in STATES for s in v))
    pick("sound", lambda v: isinstance(v, bool))
    pick("reapply_after_resume", lambda v: isinstance(v, bool))
    pick("allow_unverified_devices", lambda v: isinstance(v, bool))
    pick("udp_port", lambda v: isinstance(v, int) and 1024 <= v <= 65535)
    return cfg


def to_dict(cfg: Config) -> dict:
    data = asdict(cfg)
    # Keep a stable, readable key order.
    return {f.name: data[f.name] for f in fields(Config)}


def load(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config()
    except (OSError, ValueError) as exc:
        log.warning("config %s unreadable (%s); using defaults", path, exc)
        return Config()
    if not isinstance(raw, dict):
        return Config()
    return from_dict(raw)


def save(cfg: Config, path: Path) -> None:
    write_json_atomic(path, to_dict(cfg))


def write_json_atomic(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def effect_for_state(cfg: Config, state: str) -> Effect:
    style = cfg.styles.get(state) or default_styles()[state]
    rgb = scale(parse_hex(style.color), style.intensity)
    mode = "breath" if style.pulse else "static"
    return Effect.solid(rgb, mode=mode, level=cfg.level, speed=min(cfg.pulse_speed, MAX_PULSE_SPEED))


def restore_effect(cfg: Config) -> Effect:
    r = cfg.restore
    colors = tuple(parse_hex(c) for c in r.colors)
    return Effect(mode=r.mode, colors=colors, level=r.level, speed=r.speed, direction=r.direction)  # type: ignore[arg-type]
