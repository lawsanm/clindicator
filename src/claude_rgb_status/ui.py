"""Settings and Diagnostics windows (tkinter, main thread only).

Other threads call UI.open()/UI.quit(), which only enqueue a request.
"""

from __future__ import annotations

import copy
import os
import queue
import time
import tkinter as tk
from tkinter import colorchooser, ttk

from . import DISPLAY_NAME, autostart, hooks_install, paths
from .config import MAX_PULSE_SPEED, STATE_LABELS, STATES, Config, effect_for_state, restore_effect
from .rgb.base import MODES


def _ago(seconds) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    return f"{seconds / 60:.0f} min ago"


class UI:
    def __init__(self, root: tk.Tk, core):
        self.root = root
        self.core = core
        self.requests: queue.Queue[str] = queue.Queue()
        self.windows: dict[str, tk.Toplevel] = {}
        self.on_quit = None
        root.after(100, self._poll)

    def open(self, name: str) -> None:
        self.requests.put(name)

    def quit(self) -> None:
        self.requests.put("quit")

    def _poll(self) -> None:
        try:
            while True:
                name = self.requests.get_nowait()
                if name == "quit":
                    if self.on_quit:
                        self.on_quit()
                    self.root.quit()
                    return
                self._show(name)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _show(self, name: str) -> None:
        win = self.windows.get(name)
        if win is not None and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_force()
            return
        cls = SettingsWindow if name == "settings" else DiagnosticsWindow
        win = cls(self.root, self.core)
        self.windows[name] = win
        win.lift()
        win.focus_force()


class ColorButton(tk.Button):
    def __init__(self, master, color: str, title: str):
        super().__init__(master, width=6, relief="groove", command=self._pick)
        self.title = title
        self.set(color)

    def set(self, color: str) -> None:
        self.color = color.upper()
        self.configure(bg=self.color, activebackground=self.color, text="")

    def _pick(self) -> None:
        result = colorchooser.askcolor(color=self.color, title=self.title, parent=self)
        if result and result[1]:
            self.set(result[1])


class SettingsWindow(tk.Toplevel):
    def __init__(self, root, core):
        super().__init__(root)
        self.core = core
        self.title(f"{DISPLAY_NAME} — Settings")
        self.resizable(False, False)
        self.cfg: Config = copy.deepcopy(core.cfg)
        self.devices = core.lighting.scan()
        pad = {"padx": 6, "pady": 3}
        body = ttk.Frame(self, padding=10)
        body.grid(sticky="nsew")

        # Keyboard
        box = ttk.LabelFrame(body, text="Keyboard", padding=8)
        box.grid(row=0, column=0, sticky="ew", **pad)
        self.device_choices = [("auto", "Automatic (first supported keyboard)")]
        self.device_choices += [(d.id, f"{d.name}{'' if d.supported else ' — not verified'}") for d in self.devices]
        self.device_var = tk.StringVar()
        current = next((label for key, label in self.device_choices if key == self.cfg.device), self.device_choices[0][1])
        self.device_var.set(current)
        ttk.Combobox(box, textvariable=self.device_var, state="readonly", width=60,
                     values=[label for _, label in self.device_choices]).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(box, text="Only the selected keyboard is ever written to.").grid(row=1, column=0, columnspan=4, sticky="w")
        ttk.Label(box, text="Hardware brightness:").grid(row=2, column=0, sticky="w")
        self.level_var = tk.IntVar(value=self.cfg.level)
        ttk.Radiobutton(box, text="High", variable=self.level_var, value=2).grid(row=2, column=1, sticky="w")
        ttk.Radiobutton(box, text="Low", variable=self.level_var, value=1).grid(row=2, column=2, sticky="w")
        ttk.Label(box, text="Pulse speed:").grid(row=3, column=0, sticky="w")
        self.speed_var = tk.IntVar(value=self.cfg.pulse_speed)
        for speed in range(1, MAX_PULSE_SPEED + 1):
            ttk.Radiobutton(box, text=("Slowest", "Slow")[speed - 1], variable=self.speed_var,
                            value=speed).grid(row=3, column=speed, sticky="w")

        # Status colors
        box = ttk.LabelFrame(body, text="Status colors (intensity dims the color; pulse = slow hardware breathing)",
                             padding=8)
        box.grid(row=1, column=0, sticky="ew", **pad)
        self.rows = {}
        for i, state in enumerate(STATES):
            style = self.cfg.styles[state]
            ttk.Label(box, text=STATE_LABELS[state], width=24).grid(row=i, column=0, sticky="w")
            color = ColorButton(box, style.color, STATE_LABELS[state])
            color.grid(row=i, column=1, padx=4)
            intensity = tk.IntVar(value=style.intensity)
            ttk.Scale(box, from_=5, to=100, variable=intensity, length=160,
                      command=lambda value, var=intensity: var.set(round(float(value)))
                      ).grid(row=i, column=2, padx=4)
            ttk.Label(box, textvariable=intensity, width=4).grid(row=i, column=3)
            pulse = tk.BooleanVar(value=style.pulse)
            ttk.Checkbutton(box, text="Pulse", variable=pulse).grid(row=i, column=4, padx=4)
            ttk.Button(box, text="Test", width=6, command=lambda s=state: self._test(s)).grid(row=i, column=5)
            self.rows[state] = (color, intensity, pulse)

        # Restore profile
        box = ttk.LabelFrame(body, text="Restore profile (shown when paused or closed — the keyboard "
                                        "can't report the lighting Vantage had set)", padding=8)
        box.grid(row=2, column=0, sticky="ew", **pad)
        r = self.cfg.restore
        self.restore_mode = tk.StringVar(value=r.mode)
        ttk.Label(box, text="Effect").grid(row=0, column=0, sticky="w")
        ttk.Combobox(box, textvariable=self.restore_mode, values=MODES, state="readonly", width=8).grid(row=0, column=1)
        ttk.Label(box, text="Color").grid(row=0, column=2, padx=(10, 0))
        self.restore_color = ColorButton(box, r.colors[0], "Restore color")
        self.restore_color.grid(row=0, column=3, padx=4)
        self.restore_level = tk.IntVar(value=r.level)
        ttk.Label(box, text="Level").grid(row=0, column=4, padx=(10, 0))
        ttk.Spinbox(box, from_=1, to=2, textvariable=self.restore_level, width=3, state="readonly").grid(row=0, column=5)
        self.restore_speed = tk.IntVar(value=r.speed)
        ttk.Label(box, text="Speed").grid(row=0, column=6, padx=(10, 0))
        ttk.Spinbox(box, from_=1, to=4, textvariable=self.restore_speed, width=3, state="readonly").grid(row=0, column=7)
        self.restore_dir = tk.StringVar(value=r.direction)
        ttk.Combobox(box, textvariable=self.restore_dir, values=("ltr", "rtl"), state="readonly",
                     width=4).grid(row=0, column=8, padx=6)
        ttk.Button(box, text="Test", width=6, command=lambda: self._test("restore")).grid(row=0, column=9)
        if len(set(r.colors)) > 1:
            ttk.Label(box, text="(per-zone colors from config.json are replaced by this color when saved)"
                      ).grid(row=1, column=0, columnspan=10, sticky="w")

        # Behavior
        box = ttk.LabelFrame(body, text="Behavior", padding=8)
        box.grid(row=3, column=0, sticky="ew", **pad)
        self.tracking = tk.StringVar(value=self.cfg.tracking)
        ttk.Label(box, text="Show:").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(box, text="Most recent session", variable=self.tracking, value="latest").grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(box, text="All sessions, requests first", variable=self.tracking,
                        value="aggregate").grid(row=0, column=2, sticky="w")
        if self.cfg.tracking == "pinned":
            ttk.Radiobutton(box, text="Pinned session (tray menu)", variable=self.tracking,
                            value="pinned").grid(row=0, column=3, sticky="w")
        self.no_session = tk.StringVar(value=self.cfg.no_session)
        ttk.Label(box, text="No session:").grid(row=1, column=0, sticky="w")
        ttk.Radiobutton(box, text="Dim orange", variable=self.no_session, value="unknown").grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(box, text="Restore profile", variable=self.no_session, value="restore").grid(row=1, column=2, sticky="w")
        self.delay = tk.IntVar(value=self.cfg.attention_delay_ms)
        ttk.Label(box, text="Request delay (ms):").grid(row=2, column=0, sticky="w")
        ttk.Spinbox(box, from_=0, to=5000, increment=100, textvariable=self.delay, width=6).grid(row=2, column=1, sticky="w")
        self.hint = tk.BooleanVar(value=self.cfg.question_hint)
        ttk.Checkbutton(box, text='Say "reply may end with a question" when the last line ends with "?"',
                        variable=self.hint).grid(row=3, column=0, columnspan=4, sticky="w")
        self.notifications = tk.BooleanVar(value=self.cfg.notifications)
        ttk.Checkbutton(box, text="Desktop notifications for:", variable=self.notifications).grid(row=4, column=0, sticky="w")
        self.notify_states = {}
        for i, state in enumerate(("permission", "question", "error", "finished")):
            var = tk.BooleanVar(value=state in self.cfg.notify_states)
            ttk.Checkbutton(box, text=state.capitalize(), variable=var).grid(row=4, column=1 + i, sticky="w")
            self.notify_states[state] = var
        self.sound = tk.BooleanVar(value=self.cfg.sound)
        ttk.Checkbutton(box, text="Play a sound with notifications", variable=self.sound).grid(row=5, column=0, columnspan=2, sticky="w")
        self.reapply = tk.BooleanVar(value=self.cfg.reapply_after_resume)
        ttk.Checkbutton(box, text="Re-apply lighting after sleep", variable=self.reapply).grid(row=5, column=2, columnspan=2, sticky="w")
        self.autostart = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(box, text="Start at login", variable=self.autostart).grid(row=6, column=0, columnspan=2, sticky="w")

        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, sticky="e", **pad)
        ttk.Button(buttons, text="Reset to defaults", command=self._defaults).grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=1, padx=4)
        ttk.Button(buttons, text="Save", command=self._save).grid(row=0, column=2, padx=4)

    def _collect(self) -> Config:
        cfg = copy.deepcopy(self.cfg)
        label = self.device_var.get()
        cfg.device = next((key for key, text in self.device_choices if text == label), "auto")
        cfg.level = int(self.level_var.get())
        cfg.pulse_speed = int(self.speed_var.get())
        for state, (color, intensity, pulse) in self.rows.items():
            style = cfg.styles[state]
            style.color, style.intensity, style.pulse = color.color, int(float(intensity.get())), bool(pulse.get())
        cfg.restore.mode = self.restore_mode.get()
        cfg.restore.colors = [self.restore_color.color] * 4
        cfg.restore.level = int(self.restore_level.get())
        cfg.restore.speed = int(self.restore_speed.get())
        cfg.restore.direction = self.restore_dir.get()
        cfg.tracking = self.tracking.get()
        cfg.no_session = self.no_session.get()
        cfg.attention_delay_ms = max(0, min(5000, int(self.delay.get())))
        cfg.question_hint = bool(self.hint.get())
        cfg.notifications = bool(self.notifications.get())
        cfg.notify_states = [s for s, var in self.notify_states.items() if var.get()]
        cfg.sound = bool(self.sound.get())
        cfg.reapply_after_resume = bool(self.reapply.get())
        return cfg

    def _test(self, state: str) -> None:
        cfg = self._collect()
        effect = restore_effect(cfg) if state == "restore" else effect_for_state(cfg, state)
        self.core.post("test", effect)

    def _defaults(self) -> None:
        self.core.post("update_config", Config())
        self.destroy()

    def _save(self) -> None:
        self.core.post("update_config", self._collect())
        try:
            if self.autostart.get() != autostart.is_enabled():
                autostart.enable() if self.autostart.get() else autostart.disable()
        except OSError:
            pass
        self.destroy()


class DiagnosticsWindow(tk.Toplevel):
    def __init__(self, root, core):
        super().__init__(root)
        self.core = core
        self.title(f"{DISPLAY_NAME} — Diagnostics")
        self.geometry("900x620")
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")
        for text, command in (
            ("Re-apply lighting", lambda: core.post("reapply")),
            ("Acknowledge", lambda: core.post("ack")),
            ("Clear stuck request", lambda: core.post("clear_pending")),
            ("Copy", self._copy),
            ("Open data folder", lambda: os.startfile(paths.data_dir())),
        ):
            ttk.Button(bar, text=text, command=command).pack(side="left", padx=3)
        self.text = tk.Text(self, wrap="none", font=("Consolas", 9))
        self.text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._hooks_cache = (0.0, "")
        self._refresh()

    def _hooks_line(self) -> str:
        checked, line = self._hooks_cache
        if time.time() - checked < 5:
            return line
        try:
            settings = hooks_install.read_settings(paths.claude_settings_path())
            events = hooks_install.installed_events(settings.data)
            line = (f"installed for {len(events)}/{len(hooks_install.ALL_EVENTS)} events in {settings.path}"
                    if events else f"NOT installed in {settings.path} (run the install command)")
        except hooks_install.InstallError as exc:
            line = f"cannot read Claude settings: {exc}"
        self._hooks_cache = (time.time(), line)
        return line

    def render(self) -> str:
        snap = self.core.snapshot()
        light, recv, disp = snap["lighting"], snap["receiver"], snap["display"]
        lines = [
            f"Display     {disp['label']}{' (paused)' if snap['paused'] else ''} — {disp['detail']}",
            f"            session: {disp['session_label'] or '-'}   tracking: {snap['tracking']}",
            f"Receiver    127.0.0.1:{recv['port']}  events {recv['received']}  rejected {recv['rejected']}  "
            f"last {_ago(recv['last_event_age'])}",
            f"Hooks       {self._hooks_line()}",
            f"Keyboard    {'connected' if light['connected'] else 'NOT connected'}: {light['device'] or '-'}",
            f"            showing: {light['applied'] or '-'}   writes {light['writes']}  failures {light['failures']}"
            f"{'   last error: ' + light['last_error'] if light['last_error'] else ''}",
        ]
        lines.append("Conflicts   " + ("none detected" if not snap["conflicts"] else snap["conflicts"][0]))
        lines += [f"            {c}" for c in snap["conflicts"][1:]]
        lines += ["", "Sessions"]
        if not snap["sessions"]:
            lines.append("  (none yet: start or use Claude Code after installing the hooks)")
        for s in snap["sessions"]:
            alive = "live" if s["live"] else "closed"
            lines.append(f"  {s['label']:<34} {STATE_LABELS[s['state']]:<24} {alive:<6} pid {s['pid'] or '-':<7} "
                         f"last {_ago(s['last_event_age']):<10} events {s['events']}")
            lines.append(f"      {s['detail']}")
            if s["pending"]:
                lines.append(f"      waiting on: {', '.join(s['pending'])}")
        lines += ["", "Recent transitions (newest last; metadata only)"]
        for h in snap["history"][-60:]:
            lines.append(f"  {h['time']}  {h['project'][:18]:<18} {h['session']}  {h['event']:<20} "
                         f"{h['before']:>10} -> {h['after']:<10} {h['detail']}")
        return "\n".join(lines)

    def _refresh(self) -> None:
        if not self.winfo_exists():
            return
        content = self.render()
        top = self.text.yview()[0]
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.configure(state="disabled")
        self.text.yview_moveto(top)
        self.after(1000, self._refresh)

    def _copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.render())
