"""App core: event loop that turns relay messages into lighting, tray state and notifications.

One thread owns the Tracker; the receiver and the UI only enqueue work, so the state
machine needs no locking beyond the snapshot the UI reads.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import replace
from typing import Callable

from . import config as config_mod
from . import conflicts, endpoint, paths, winproc
from .config import STATE_LABELS, Config
from .lighting import LightingController
from .receiver import Receiver
from .rgb.base import Effect
from .state import ATTENTION, PERMISSION, QUESTION, UNKNOWN, Selection, Tracker
from .winproc import ProcessWatch

log = logging.getLogger(__name__)

TICK = 0.1
LIVENESS_EVERY = 3.0
PRUNE_EVERY = 60.0
CONFLICTS_EVERY = 10.0  # also how quickly a restarted Vantage service is noticed
STATUS_EVERY = 5.0
SLEEP_GAP = 20.0  # a loop that should tick every 0.1 s but didn't run for this long: the PC slept
RESUME_REAPPLY = (2.0, 8.0)  # seconds after resume; Vantage may re-apply its own lighting on wake
TEST_SECONDS = 4.0


class Core:
    def __init__(self, cfg: Config, *, lighting: LightingController | None = None,
                 watch: ProcessWatch | None = None, write_status: bool = True):
        self.cfg = cfg
        self.tracker = Tracker(question_hint=cfg.question_hint)
        self.lighting = lighting or LightingController(cfg.device, cfg.allow_unverified_devices)
        self.watch = watch or ProcessWatch()
        self.write_status = write_status
        self.lock = threading.RLock()
        self.inbox: queue.Queue = queue.Queue()
        self.paused = False
        self.conflicts: list[str] = []
        self.selection = Selection(UNKNOWN, "Starting", None, cfg.tracking)
        self.shown_state = UNKNOWN
        self.shown_label = "Starting"
        self.receiver: Receiver | None = None
        self.started_at = time.time()
        self.on_change: list[Callable[[], None]] = []
        self.on_notify: Callable[[str, str, str], None] | None = None  # (state, title, message)
        self.on_quit_request: Callable[[], None] | None = None
        self._candidate: tuple[str, float] = (UNKNOWN, 0.0)
        self._test: tuple[Effect, str, float] | None = None
        self._session_state: dict[str, tuple[str, float]] = {}
        self._notified: dict[str, tuple[str, float]] = {}
        self._reapply_at: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_status = 0.0
        self._status_followup = 0.0
        self._lighting_pids: set[int] | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        ep = endpoint.ensure(self.cfg.udp_port)
        self.receiver = Receiver(ep.port, ep.token, lambda msg: self.inbox.put(("event", msg)))
        if self.receiver.port != ep.port:
            endpoint.ensure(self.receiver.port)  # preferred port busy; tell the relay
        self.receiver.start()
        self.lighting.start()
        self.conflicts = conflicts.detect()
        for warning in self.conflicts:
            log.warning(warning)
        self._thread = threading.Thread(target=self._loop, name="core", daemon=True)
        self._thread.start()
        log.info("listening on 127.0.0.1:%s", self.receiver.port)

    def shutdown(self) -> None:
        """Stop and restore the lighting (pause/quit look)."""
        self._stop.set()
        if self._thread:
            self._thread.join(2.0)
        if self.receiver:
            self.receiver.stop()
        self.lighting.shutdown(config_mod.restore_effect(self.cfg))
        self._write_status(force=True, stopped=True)

    def post(self, command: str, *args) -> None:
        """Thread-safe: run a command on the core thread."""
        self.inbox.put(("cmd", command, args))

    # ------------------------------------------------------------ loop

    def _loop(self) -> None:
        last_wall, last_mono = time.time(), time.monotonic()
        next_liveness = next_prune = next_conflicts = 0.0
        while not self._stop.is_set():
            try:
                item = self.inbox.get(timeout=TICK)
            except queue.Empty:
                item = None
            changed = False
            with self.lock:
                while item is not None:
                    changed |= self._handle(item)
                    try:
                        item = self.inbox.get_nowait()
                    except queue.Empty:
                        item = None
                now, mono = time.time(), time.monotonic()
                if max(now - last_wall, mono - last_mono) > SLEEP_GAP:
                    log.info("resume from sleep detected; reopening keyboard")
                    self.lighting.reopen()
                    if self.cfg.reapply_after_resume:
                        self._reapply_at = [now + d for d in RESUME_REAPPLY]
                last_wall, last_mono = now, mono
                if self._reapply_at and now >= self._reapply_at[0]:
                    self._reapply_at.pop(0)
                    self.lighting.reapply()
                if now >= next_liveness:
                    next_liveness = now + LIVENESS_EVERY
                    changed |= bool(self.tracker.check_liveness(self.watch.is_alive, now))
                if now >= next_prune:
                    next_prune = now + PRUNE_EVERY
                    self.tracker.prune(now)
                if now >= next_conflicts:
                    next_conflicts = now + CONFLICTS_EVERY
                    procs = winproc.list_processes()
                    self.conflicts = conflicts.detect(procs)
                    pids = conflicts.lighting_pids(procs)
                    if self._lighting_pids is not None and pids - self._lighting_pids:
                        log.info("other lighting software (re)started; re-applying status color")
                        self._reapply_at = sorted(self._reapply_at + [now + d for d in RESUME_REAPPLY])
                    self._lighting_pids = pids
                changed |= self._update_output(now)
                self._check_notifications(now)
            if changed:
                self._write_status()
                self._emit_change()
                # The lighting thread applies the change a moment later; record that too.
                self._status_followup = time.time() + 0.5
            elif time.time() - self._last_status > STATUS_EVERY or (
                    self._status_followup and time.time() >= self._status_followup):
                self._status_followup = 0.0
                self._write_status()

    def _handle(self, item: tuple) -> bool:
        if item[0] == "event" and "ctl" in item[1]:
            return self._control(item[1]["ctl"])
        if item[0] == "event":
            tr = self.tracker.apply(item[1])
            if tr:
                log.info("%s %s %s: %s -> %s (%s)", tr.project, tr.sid[:8], tr.event, tr.before, tr.after, tr.detail)
            return True
        _, command, args = item
        handler = getattr(self, f"_cmd_{command}", None)
        if handler is None:
            log.warning("unknown command %s", command)
            return False
        handler(*args)
        return True

    def _control(self, name: str) -> bool:
        log.info("control command: %s", name)
        if name == "quit" and self.on_quit_request:
            self.on_quit_request()
        elif name in ("pause", "resume"):
            self._cmd_pause(name == "pause")
        elif name == "reapply":
            self._cmd_reapply()
        else:
            return False
        return True

    # ------------------------------------------------------------ commands (core thread)

    def _cmd_ack(self) -> None:
        sel = self.tracker.select(self.cfg.tracking, self.cfg.pinned_session)
        if self.cfg.tracking == "aggregate":
            self.tracker.acknowledge([s.sid for s in self.tracker.live_sessions()])
        elif sel.session:
            self.tracker.acknowledge([sel.session.sid])

    def _cmd_clear_pending(self) -> None:
        sel = self.tracker.select(self.cfg.tracking, self.cfg.pinned_session)
        if sel.session:
            self.tracker.clear_pending(sel.session.sid)

    def _cmd_pause(self, paused: bool | None = None) -> None:
        self.paused = (not self.paused) if paused is None else paused

    def _cmd_test(self, state_or_effect, seconds: float = TEST_SECONDS) -> None:
        if isinstance(state_or_effect, Effect):
            effect, label = state_or_effect, "Test"
        elif state_or_effect == "restore":
            effect, label = config_mod.restore_effect(self.cfg), "Test: restore profile"
        else:
            effect = config_mod.effect_for_state(self.cfg, state_or_effect)
            label = f"Test: {STATE_LABELS[state_or_effect]}"
        self._test = (effect, label, time.time() + seconds)

    def _cmd_track(self, mode: str, sid: str = "") -> None:
        self._cmd_update_config(replace(self.cfg, tracking=mode, pinned_session=sid if mode == "pinned" else ""))

    def _cmd_update_config(self, cfg: Config) -> None:
        self.cfg = cfg
        self.tracker.question_hint = cfg.question_hint
        self.lighting.configure(cfg.device, cfg.allow_unverified_devices)
        try:
            config_mod.save(cfg, paths.config_path())
        except OSError as exc:
            log.error("cannot save config: %s", exc)

    def _cmd_reapply(self) -> None:
        self.lighting.reapply()

    # ------------------------------------------------------------ output

    def _update_output(self, now: float) -> bool:
        sel = self.tracker.select(self.cfg.tracking, self.cfg.pinned_session)
        self.selection = sel
        state = sel.state
        if state != self._candidate[0]:
            self._candidate = (state, now)
        # Requests must persist briefly before lighting up, so one that another hook
        # answers within milliseconds doesn't flash.
        delay = self.cfg.attention_delay_ms / 1000
        if state in (PERMISSION, QUESTION) and now - self._candidate[1] < delay:
            state = self.shown_state
        if self._test and now >= self._test[2]:
            self._test = None
        if self._test:
            effect, label = self._test[0], self._test[1]
        elif self.paused:
            effect, label = config_mod.restore_effect(self.cfg), "Paused"
        elif sel.session is None and self.cfg.no_session == "restore":
            effect, label = config_mod.restore_effect(self.cfg), "No session (normal lighting)"
        else:
            effect, label = config_mod.effect_for_state(self.cfg, state), STATE_LABELS[state]
        self.lighting.set_target(effect, label)
        changed = (state, label) != (self.shown_state, self.shown_label)
        self.shown_state, self.shown_label = state, label
        return changed

    def _check_notifications(self, now: float) -> None:
        delay = self.cfg.attention_delay_ms / 1000
        live = self.tracker.live_sessions()
        for sid in set(self._session_state) - {s.sid for s in live}:
            self._session_state.pop(sid, None)
            self._notified.pop(sid, None)
        for s in live:
            state, detail = self.tracker.display_for(s)
            current = self._session_state.get(s.sid)
            if current is None or current[0] != state:
                current = self._session_state[s.sid] = (state, now)
            wanted = (self.cfg.notifications or self.cfg.sound) and not self.paused
            if (wanted and state in ATTENTION and state in self.cfg.notify_states
                    and self._notified.get(s.sid) != current and now - current[1] >= delay):
                self._notified[s.sid] = current
                if self.on_notify:
                    try:
                        self.on_notify(state, STATE_LABELS[state], f"{s.label}: {detail}")
                    except Exception:
                        log.exception("notification failed")

    def _emit_change(self) -> None:
        for callback in list(self.on_change):
            try:
                callback()
            except Exception:
                log.exception("change listener failed")

    # ------------------------------------------------------------ snapshots

    def snapshot(self) -> dict:
        with self.lock:
            now = time.time()
            sel = self.selection
            sessions = []
            for s in sorted(self.tracker.sessions.values(), key=lambda s: -s.activity_t):
                state, detail = self.tracker.display_for(s)
                sessions.append({
                    "sid": s.sid, "label": s.label, "project": s.project, "cwd": s.cwd, "title": s.title,
                    "state": state, "detail": detail, "live": s.live, "pid": s.pid,
                    "last_event_age": round(now - s.last_t, 1) if s.last_t else None,
                    "events": s.events, "pending": [p.label for p in s.pending.values()],
                    "subagents_running": sum(1 for a in s.subagents.values() if a.get("stop") is None),
                })
            history = [{
                "time": time.strftime("%H:%M:%S", time.localtime(tr.at)), "project": tr.project,
                "session": tr.sid[:8], "event": tr.event, "before": tr.before, "after": tr.after, "detail": tr.detail,
            } for tr in list(self.tracker.history)[-150:]]
            receiver = self.receiver
            return {
                "updated": now,
                "paused": self.paused,
                "tracking": self.cfg.tracking,
                "pinned": self.cfg.pinned_session,
                "display": {
                    "state": self.shown_state,
                    "label": self.shown_label,
                    "detail": sel.detail,
                    "session": sel.session.sid if sel.session else None,
                    "session_label": sel.session.label if sel.session else None,
                },
                "sessions": sessions,
                "lighting": self.lighting.status(),
                "receiver": {
                    "port": receiver.port if receiver else None,
                    "received": receiver.received if receiver else 0,
                    "rejected": receiver.rejected if receiver else 0,
                    "last_event_age": round(now - receiver.last_event_at, 1)
                    if receiver and receiver.last_event_at else None,
                },
                "conflicts": list(self.conflicts),
                "history": history,
            }

    def _write_status(self, force: bool = False, stopped: bool = False) -> None:
        if not self.write_status:
            return
        snap = self.snapshot()
        snap["running"] = not stopped
        self._last_status = time.time()
        try:
            config_mod.write_json_atomic(paths.status_path(), snap)
        except OSError as exc:
            log.debug("status write failed: %s", exc)
