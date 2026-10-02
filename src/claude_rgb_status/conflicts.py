"""Detect other software that also drives the keyboard lighting. We only warn; we never stop it."""

from __future__ import annotations

from .winproc import list_processes

# (lower-case substring of the exe name, what it is, why it matters)
KNOWN = (
    ("lenovogamingsystemaddin", "Lenovo Vantage (gaming add-in)",
     "can re-apply its own keyboard lighting, e.g. after sleep, Fn+Space or a lighting change in Vantage"),
    ("legionspace", "Lenovo Legion Space", "can set keyboard lighting"),
    ("legionzone", "Lenovo Legion Zone", "can set keyboard lighting"),
    ("lenovolegiontoolkit", "Lenovo Legion Toolkit", "can set keyboard lighting"),
    ("openrgb", "OpenRGB", "can set keyboard lighting"),
    ("signalrgb", "SignalRGB", "can set keyboard lighting"),
    ("legion-kb-rgb", "L5P-Keyboard-RGB", "drives the same 4-zone controller"),
    ("legion_kb_rgb", "L5P-Keyboard-RGB", "drives the same 4-zone controller"),
)


def detect(processes: list[tuple[int, int, str]] | None = None) -> list[str]:
    names = {exe.lower() for _, _, exe in (processes if processes is not None else list_processes())}
    found = []
    for needle, product, why in KNOWN:
        if any(needle in name for name in names) and not any(product in f for f in found):
            found.append(f"{product} is running: it {why}. Claude RGB Status does not disable it.")
    return found


def lighting_pids(processes: list[tuple[int, int, str]] | None = None) -> set[int]:
    """PIDs of known lighting software; a new PID means it (re)started and may have
    re-applied its own lighting."""
    procs = processes if processes is not None else list_processes()
    return {pid for pid, _, exe in procs if any(needle in exe.lower() for needle, _, _ in KNOWN)}
