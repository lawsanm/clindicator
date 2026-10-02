import pytest

from claude_rgb_status import config
from claude_rgb_status.rgb.base import Effect
from claude_rgb_status.rgb.legion4zone import REPORT_LEN, build_packet


def test_static_layout_matches_openrgb_l5p():
    effect = Effect(mode="static", colors=((1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12)), level=2, speed=1)
    p = build_packet(effect)
    assert len(p) == REPORT_LEN == 33
    assert p[0:2] == b"\xcc\x16"
    assert p[2] == 0x01 and p[3] == 1 and p[4] == 2
    assert list(p[5:17]) == list(range(1, 13))
    assert p[17:] == bytes(16)


def test_breath_keeps_colors_and_speed():
    p = build_packet(Effect.solid((255, 144, 0), mode="breath", level=1, speed=2))
    assert p[2] == 0x03 and p[3] == 2 and p[4] == 1
    assert p[5:8] == bytes((255, 144, 0)) and p[14:17] == bytes((255, 144, 0))


@pytest.mark.parametrize("direction,index", [("ltr", 18), ("rtl", 19)])
def test_wave_direction_flags(direction, index):
    p = build_packet(Effect(mode="wave", direction=direction, speed=3))
    assert p[2] == 0x04 and p[3] == 3
    assert p[index] == 1 and p[37 - index] == 0
    assert p[5:17] == bytes(12)  # colors are ignored by wave


def test_smooth_has_no_colors():
    p = build_packet(Effect(mode="smooth"))
    assert p[2] == 0x06 and p[5:17] == bytes(12)


def test_off_is_static_black():
    p = build_packet(Effect(mode="off"))
    assert p[2] == 0x01 and p[5:17] == bytes(12) and p[4] in (1, 2)


def test_values_are_clamped():
    p = build_packet(Effect.solid((300, -5, 128), level=9, speed=0))
    assert p[3] == 1 and p[4] == 2
    assert p[5:8] == bytes((255, 0, 128))


def test_rejects_unknown_mode():
    with pytest.raises(ValueError):
        build_packet(Effect(mode="rainbow-strobe"))


def test_default_state_effects_are_slow_and_valid():
    cfg = config.Config()
    for state in config.STATES:
        effect = config.effect_for_state(cfg, state)
        packet = build_packet(effect)
        assert packet[3] <= config.MAX_PULSE_SPEED
    assert config.effect_for_state(cfg, "permission").mode == "breath"
    assert config.effect_for_state(cfg, "question").mode == "breath"
    assert config.effect_for_state(cfg, "processing").mode == "static"
    # dim white really is dim
    assert max(config.effect_for_state(cfg, "idle").colors[0]) <= 60


def test_pulse_speed_cannot_exceed_slow_limit():
    cfg = config.from_dict({"pulse_speed": 4})
    assert cfg.pulse_speed == 1  # invalid -> default
