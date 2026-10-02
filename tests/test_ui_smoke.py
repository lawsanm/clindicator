"""Smoke tests: the tray menu and the Tk windows build and read back settings (no hardware, no tray icon)."""

import tkinter as tk

import pytest

from claude_rgb_status import config
from claude_rgb_status.core import Core
from claude_rgb_status.lighting import LightingController
from claude_rgb_status.state import Tracker


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_RGB_STATUS_HOME", str(tmp_path))
    c = Core(config.Config(device="none"), lighting=LightingController(device="none"), write_status=False)
    c.tracker = Tracker()
    for i, (sid, ev) in enumerate([("s-one", "UserPromptSubmit"), ("s-two", "Stop")]):
        c.tracker.apply({"ev": "SessionStart", "sid": sid, "cwd": f"D:\\p\\proj{i}", "t": 100 + i, "src": "startup"})
        c.tracker.apply({"ev": ev, "sid": sid, "cwd": f"D:\\p\\proj{i}", "t": 110 + i})
    c.tracker.apply({"ev": "PermissionRequest", "sid": "s-one", "tool": "Bash", "ih": "x", "t": 120})
    c._update_output(200.0)
    return c


def walk(menu, depth=0):
    """Render a pystray menu (and submenus) to text, evaluating every dynamic part."""
    lines = []
    for item in menu.items:
        if item.visible:
            lines.append("  " * depth + item.text)
            if item.submenu:
                lines += walk(item.submenu, depth + 1)
    return lines


def test_tray_menu_builds(core):
    from claude_rgb_status import tray as tray_mod

    t = tray_mod.Tray.__new__(tray_mod.Tray)  # skip creating the real tray icon
    t.core, t.ui = core, None
    lines = walk(tray_mod.Menu(t._items))
    text = "\n".join(lines)
    assert "Track session" in text and "Most recent session" in text
    assert "proj0 [s-one]" in text and "proj1 [s-two]" in text
    assert "Test colors" in text and "Quit" in text
    assert t._tooltip().startswith("Claude: ") and len(t._tooltip()) <= 127
    for state in config.STATES:
        assert tray_mod.icons.for_state(state, config.Config().styles[state].color, True, False).size == (64, 64)


def test_settings_and_diagnostics_windows(core):
    from claude_rgb_status.ui import DiagnosticsWindow, SettingsWindow

    root = tk.Tk()
    root.withdraw()
    try:
        settings = SettingsWindow(root, core)
        settings.withdraw()
        collected = settings._collect()
        assert collected.styles == core.cfg.styles and collected.device == core.cfg.device
        settings.rows["finished"][1].set(42)
        assert settings._collect().styles["finished"].intensity == 42
        settings._test("permission")
        assert core.inbox.get_nowait()[1] == "test"

        diag = DiagnosticsWindow(root, core)
        diag.withdraw()
        report = diag.render()
        assert "Receiver" in report and "Keyboard" in report and "proj0 [s-one]" in report
        assert "Waiting for permission" in report
    finally:
        root.destroy()
