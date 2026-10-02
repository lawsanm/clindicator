"""Per-session Claude Code state machines and the choice of what to display.

Input is the metadata dict produced by hook_relay (event name, session id, agent id,
tool name / use id / input hash, spawn time "t", Claude PID). Everything here is
pure logic, so it is tested with simulated events.

Rules that keep the display truthful:
- A pending permission or question clears only on evidence about that request
  (its tool call finishing, its batch completing, its elicitation result) or on
  turn-level evidence (Stop, StopFailure, a new prompt, idle_prompt, SessionEnd).
  Unrelated parallel tool events never clear it.
- Events older than the session's latest turn boundary can't change the turn
  (late async delivery), and repeated events are idempotent.
- Subagents only contribute requests; their stop never finishes the parent.
- Silence is never treated as failure: a session is "disconnected" only when its
  Claude Code process has exited without SessionEnd.
"""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

PROCESSING, PERMISSION, QUESTION, FINISHED, ERROR, IDLE, UNKNOWN = (
    "processing", "permission", "question", "finished", "error", "idle", "unknown",
)
# Aggregate mode shows the session that most needs you.
PRIORITY = {PERMISSION: 7, QUESTION: 6, ERROR: 5, FINISHED: 4, PROCESSING: 3, IDLE: 2, UNKNOWN: 1}
ATTENTION = (PERMISSION, QUESTION, ERROR, FINISHED)

QUESTION_TOOLS = {"AskUserQuestion"}
APPROVAL_TOOLS = {"ExitPlanMode"}  # its PreToolUse already means an approval dialog
MAIN = ""  # scope of the main agent; subagents use their agent_id
CLOSED_SESSION_TTL = 600.0
STALE_SESSION_TTL = 12 * 3600.0

# Pending-request sources
SRC_TOOL = "tool"  # PreToolUse / PermissionRequest
SRC_ELICIT = "elicitation"
SRC_NOTIF_PERMISSION = "notif:permission"
SRC_NOTIF_ELICIT = "notif:elicitation"
SRC_NOTIF_AGENT = "notif:agent"
SRC_NOTIF_QUOTA = "notif:quota"
TOOL_RELATED = {SRC_TOOL, SRC_ELICIT, SRC_NOTIF_PERMISSION, SRC_NOTIF_ELICIT}
RESOLVED_BY_PROGRESS = {SRC_NOTIF_AGENT, SRC_NOTIF_QUOTA}


@dataclass
class Pending:
    key: str
    kind: str  # PERMISSION or QUESTION
    scope: str
    label: str
    t: float
    source: str
    tool: str | None = None
    ih: str | None = None
    tuid: str | None = None
    mcp: str | None = None
    eid: str | None = None


@dataclass
class ToolCall:
    tuid: str
    tool: str
    ih: str | None
    scope: str
    t_pre: float | None
    t_done: float | None = None


@dataclass
class Session:
    sid: str
    cwd: str = ""
    title: str = ""
    pid: int | None = None
    first_t: float = 0.0
    last_t: float = 0.0
    activity_t: float = 0.0  # last prompt / session start, for "follow latest"
    turn: str = UNKNOWN  # processing | finished | error | idle | unknown
    turn_t: float = 0.0
    detail: str = ""
    prompt_t: float = 0.0
    boundary_t: float = 0.0  # latest turn start or end
    resolved_t: float = 0.0  # latest time any request was answered
    acknowledged: bool = False
    question_hint: bool = False
    background: int = 0
    compacting: bool = False
    ended: bool = False
    end_reason: str = ""
    closed_at: float | None = None  # local time when ended/disconnected (for pruning)
    disconnected: bool = False
    pending: dict[str, Pending] = field(default_factory=dict)
    calls: dict[str, ToolCall] = field(default_factory=dict)
    subagents: dict[str, dict] = field(default_factory=dict)
    events: int = 0

    @property
    def project(self) -> str:
        return os.path.basename(self.cwd.rstrip("\\/")) or self.cwd or "?"

    @property
    def short(self) -> str:
        return self.sid[:8]

    @property
    def label(self) -> str:
        name = self.title or self.project
        return f"{name} [{self.short}]"

    @property
    def live(self) -> bool:
        return not self.ended and not self.disconnected


@dataclass
class Selection:
    state: str
    detail: str
    session: Session | None
    mode: str


@dataclass
class Transition:
    at: float  # local wall time
    t: float  # event time
    sid: str
    project: str
    event: str
    before: str
    after: str
    detail: str


def _tool_label(tool: str | None) -> str:
    if not tool:
        return "tool"
    if tool == "ExitPlanMode":
        return "Plan approval"
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        if len(parts) >= 3:
            return f"{parts[1]}: {parts[2]}"
    return tool


class Tracker:
    def __init__(self, question_hint: bool = True, history: int = 300):
        self.sessions: dict[str, Session] = {}
        self.question_hint = question_hint
        self.history: deque[Transition] = deque(maxlen=history)

    # ------------------------------------------------------------------ input

    def apply(self, msg: dict, now: float | None = None) -> Transition | None:
        """Apply one relay message. Returns the display transition it caused, if any."""
        ev, sid = msg.get("ev"), msg.get("sid")
        if not ev or not sid:
            return None
        now = time.time() if now is None else now
        try:
            t = float(msg.get("t") or now)
        except (TypeError, ValueError):
            t = now
        s = self.sessions.get(sid)
        if s is None:
            if ev == "SessionEnd":
                return None
            s = self.sessions[sid] = Session(sid=sid, first_t=t, activity_t=t)
        if s.ended and ev != "SessionStart":
            return None  # late delivery after SessionEnd
        before = self.display_for(s)
        if msg.get("cwd"):
            s.cwd = msg["cwd"]
        if msg.get("title"):
            s.title = msg["title"]
        pid = msg.get("pid")
        if t >= s.last_t and (s.disconnected or (pid and pid != s.pid)):
            # Newer activity: the session is alive (possibly resumed in a new Claude process).
            if pid:
                s.pid = int(pid)
            s.disconnected = False
            s.closed_at = None
        handler: Callable[[Session, dict, float], None] | None = getattr(self, f"_on_{ev}", None)
        if handler is not None:
            handler(s, msg, t)
        s.last_t = max(s.last_t, t)
        s.events += 1
        return self._record(s, ev, t, before, now)

    def _record(self, s: Session, event: str, t: float, before: tuple[str, str], now: float) -> Transition | None:
        after = self.display_for(s)
        if after == before:
            return None
        tr = Transition(now, t, s.sid, s.project, event, before[0], after[0], after[1])
        self.history.append(tr)
        return tr

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _scope(msg: dict) -> str:
        return msg.get("aid") or MAIN

    def _set_turn(self, s: Session, turn: str, t: float, detail: str) -> None:
        s.turn, s.turn_t, s.detail = turn, t, detail
        s.acknowledged = False
        if turn != FINISHED:
            s.question_hint = False

    def _resume(self, s: Session, t: float, detail: str = "Working") -> None:
        """Main-agent work is evidently happening again."""
        if s.turn != PROCESSING:
            self._set_turn(s, PROCESSING, t, detail)

    def _resolve(self, s: Session, t: float, predicate: Callable[[Pending], bool]) -> int:
        keys = [k for k, p in s.pending.items() if predicate(p)]
        for k in keys:
            del s.pending[k]
        if keys:
            s.resolved_t = max(s.resolved_t, t)
        return len(keys)

    def _end_scope(self, s: Session, scope: str, t: float) -> None:
        """Everything in this scope that started before t is over."""
        self._resolve(s, t, lambda p: p.scope == scope and p.t <= t)
        for call in s.calls.values():
            if call.scope == scope and call.t_done is None and (call.t_pre or 0) <= t:
                call.t_done = t
        self._trim_calls(s)

    def _progress(self, s: Session, t: float) -> None:
        """The session moved on, so generic 'needs input' notices are answered."""
        self._resolve(s, t, lambda p: p.source in RESOLVED_BY_PROGRESS and p.t < t)

    @staticmethod
    def _trim_calls(s: Session, keep: int = 200) -> None:
        if len(s.calls) > keep:
            done = sorted((c for c in s.calls.values() if c.t_done is not None), key=lambda c: c.t_done or 0)
            for c in done[: len(s.calls) - keep]:
                del s.calls[c.tuid]

    def _open_call(self, s: Session, scope: str, tool: str | None, ih: str | None) -> ToolCall | None:
        candidates = [c for c in s.calls.values() if c.scope == scope and c.tool == tool and c.t_done is None]
        exact = [c for c in candidates if ih is not None and c.ih == ih]
        pool = exact or candidates
        return max(pool, key=lambda c: c.t_pre or 0) if pool else None

    def _recently_done(self, s: Session, scope: str, tool: str | None, ih: str | None, t: float) -> bool:
        return any(
            c.scope == scope and c.tool == tool and c.ih == ih and c.t_done is not None and c.t_done >= t - 0.5
            for c in s.calls.values()
        )

    def _add_pending(self, s: Session, p: Pending) -> None:
        if p.key not in s.pending:
            s.pending[p.key] = p

    # ------------------------------------------------------------ handlers

    def _on_SessionStart(self, s: Session, m: dict, t: float) -> None:
        s.ended = s.disconnected = False
        s.closed_at = None
        s.activity_t = max(s.activity_t, t)
        if m.get("src") == "compact" or t < s.turn_t:
            return
        s.pending.clear()
        s.calls.clear()
        resumed = m.get("src") in ("resume", "fork")
        self._set_turn(s, IDLE, t, "Session resumed" if resumed else "Session started")

    def _on_SessionEnd(self, s: Session, m: dict, t: float) -> None:
        s.ended = True
        s.end_reason = m.get("rsn") or "other"
        s.closed_at = time.time()
        s.pending.clear()

    def _on_UserPromptSubmit(self, s: Session, m: dict, t: float) -> None:
        if t < s.prompt_t:
            return
        s.prompt_t = max(s.prompt_t, t)
        s.activity_t = max(s.activity_t, t)
        self._end_scope(s, MAIN, t)
        self._progress(s, t)
        s.boundary_t = max(s.boundary_t, t)
        s.background = 0
        self._set_turn(s, PROCESSING, t, "Working")

    def _on_PreToolUse(self, s: Session, m: dict, t: float) -> None:
        scope, tool, tuid, ih = self._scope(m), m.get("tool"), m.get("tuid"), m.get("ih")
        key = tuid or f"anon:{scope}:{tool}:{t:.6f}"
        call = s.calls.get(key)
        if call is not None:  # duplicate, or Post* arrived first
            if call.t_pre is None:
                call.t_pre = t
            return
        s.calls[key] = ToolCall(key, tool or "", ih, scope, t)
        # A PermissionRequest processed before this event: attach it to this call.
        for p in s.pending.values():
            if p.source == SRC_TOOL and p.tuid is None and p.scope == scope and p.tool == tool and p.ih == ih:
                p.tuid = key
        if scope == MAIN and t > s.boundary_t:
            self._resume(s, t)
            self._progress(s, t)
        if any(p.tuid == key for p in s.pending.values()):
            return  # its PermissionRequest already registered the request
        if tool in QUESTION_TOOLS:
            self._add_pending(s, Pending(key, QUESTION, scope, "Question for you", t, SRC_TOOL, tool, ih, key))
        elif tool in APPROVAL_TOOLS:
            self._add_pending(s, Pending(key, PERMISSION, scope, _tool_label(tool), t, SRC_TOOL, tool, ih, key))

    def _on_PermissionRequest(self, s: Session, m: dict, t: float) -> None:
        scope, tool, ih = self._scope(m), m.get("tool"), m.get("ih")
        if scope == MAIN and t < s.boundary_t:
            return  # belongs to a turn that already ended
        if scope != MAIN:
            stopped = (s.subagents.get(scope) or {}).get("stop")
            if stopped is not None and stopped >= t:
                return  # the subagent already finished
        call = self._open_call(s, scope, tool, ih)
        if call is None and self._recently_done(s, scope, tool, ih, t):
            return  # answered before we saw the request (e.g. another hook approved it)
        if call is not None:
            key, tuid = call.tuid, call.tuid
        else:
            key, tuid = f"perm:{scope}:{tool}:{ih}:{t:.6f}", None
            if any(p.tuid is None and p.scope == scope and p.tool == tool and p.ih == ih for p in s.pending.values()):
                return  # duplicate delivery
        kind = QUESTION if tool in QUESTION_TOOLS else PERMISSION
        label = "Question for you" if kind == QUESTION else _tool_label(tool)
        self._add_pending(s, Pending(key, kind, scope, label, t, SRC_TOOL, tool, ih, tuid, m.get("mcp")))
        if scope == MAIN and t > s.boundary_t:
            self._resume(s, t)

    def _finish_call(self, s: Session, m: dict, t: float) -> ToolCall:
        scope, tool, tuid, ih = self._scope(m), m.get("tool"), m.get("tuid"), m.get("ih")
        key = tuid or f"anon:{scope}:{tool}:{t:.6f}"
        call = s.calls.get(key)
        if call is None:
            call = s.calls[key] = ToolCall(key, tool or "", ih, scope, None)
        call.t_done = t if call.t_done is None else min(call.t_done, t)
        self._resolve(s, t, lambda p: p.source == SRC_TOOL and (
            p.tuid == key or (p.tuid is None and p.scope == scope and p.tool == tool and p.ih == ih)))
        if tool in ("Bash", "PowerShell"):
            self._resolve(s, t, lambda p: p.source == SRC_NOTIF_PERMISSION and p.scope == scope and p.t <= t)
        server = m.get("mcp") or (tool.split("__")[1] if tool and tool.startswith("mcp__") and tool.count("__") >= 2 else None)
        if server:
            self._resolve(s, t, lambda p: p.source in (SRC_ELICIT, SRC_NOTIF_ELICIT) and p.scope == scope
                          and p.mcp in (None, server) and p.t <= t)
        self._trim_calls(s)
        return call

    def _on_PostToolUse(self, s: Session, m: dict, t: float) -> None:
        call = self._finish_call(s, m, t)
        if call.scope == MAIN and s.turn == UNKNOWN:
            self._resume(s, t)  # app started mid-turn

    _on_PostToolUseFailure = _on_PostToolUse

    def _on_PermissionDenied(self, s: Session, m: dict, t: float) -> None:
        self._finish_call(s, m, t)  # auto mode refused it; the tool won't run

    def _on_PostToolBatch(self, s: Session, m: dict, t: float) -> None:
        scope = self._scope(m)
        ids = {b[0] for b in m.get("batch") or [] if isinstance(b, list) and b and b[0]}
        for tuid in ids:
            call = s.calls.get(tuid)
            if call is not None and call.t_done is None:
                call.t_done = t
        # The whole batch resolved, so no tool request in this scope can still be open.
        self._resolve(s, t, lambda p: p.scope == scope and (
            p.tuid in ids or (p.source in TOOL_RELATED and p.t <= t)))
        for call in s.calls.values():
            if call.scope == scope and call.t_done is None and (call.t_pre or 0) <= t:
                call.t_done = t
        if scope == MAIN and t > s.boundary_t:
            self._resume(s, t)
            self._progress(s, t)

    def _on_Notification(self, s: Session, m: dict, t: float) -> None:
        nt = m.get("nt")
        if nt == "permission_prompt":
            # Sent ~6 s after any dialog appears while you're away, including AskUserQuestion
            # and plan approval (seen live). PermissionRequest/PreToolUse are the primary
            # signals; this only covers dialogs without one (sandbox network requests), so it
            # never overrides a request we already know about.
            if s.pending:
                return
            if s.turn != PROCESSING or t <= s.boundary_t or t <= s.resolved_t:
                return
            self._add_pending(s, Pending(f"notif-perm:{t:.6f}", PERMISSION, MAIN, "Permission prompt", t,
                                         SRC_NOTIF_PERMISSION))
        elif nt in ("elicitation_dialog", "elicitation_url_dialog"):
            if s.pending:
                return
            if t <= s.boundary_t or t <= s.resolved_t:
                return
            self._add_pending(s, Pending(f"notif-elicit:{t:.6f}", QUESTION, MAIN, "MCP input requested", t,
                                         SRC_NOTIF_ELICIT))
        elif nt in ("elicitation_complete", "elicitation_response"):
            self._resolve(s, t, lambda p: p.source in (SRC_ELICIT, SRC_NOTIF_ELICIT) and p.t <= t)
        elif nt == "idle_prompt":
            # "Finished ~60 s ago and you haven't typed since": nothing can still be
            # waiting in the main agent, and the turn is over even without a Stop
            # (Stop doesn't fire on interrupts). It is not evidence of a question.
            if t <= s.prompt_t:
                return
            self._end_scope(s, MAIN, t)
            if s.turn in (PROCESSING, UNKNOWN):
                self._set_turn(s, FINISHED, t, "Idle at the prompt (turn ended without a Stop event)")
                s.boundary_t = max(s.boundary_t, t)
        elif nt == "agent_needs_input":
            self._add_pending(s, Pending(f"notif-agent:{t:.6f}", QUESTION, MAIN, "Agent needs your input", t,
                                         SRC_NOTIF_AGENT))
        elif nt == "quota_auto_resume_fired":
            self._resolve(s, t, lambda p: p.source == SRC_NOTIF_QUOTA)
            if t > s.boundary_t:
                self._resume(s, t, "Resumed after usage limit")
        elif nt == "quota_auto_resume_stale":
            self._add_pending(s, Pending(f"notif-quota:{t:.6f}", QUESTION, MAIN,
                                         "Usage limit reset: press Enter to continue", t, SRC_NOTIF_QUOTA))
        elif nt == "quota_auto_resume_disabled":
            if t > s.prompt_t:
                self._set_turn(s, ERROR, t, "Usage limit: not continuing automatically")

    def _on_Stop(self, s: Session, m: dict, t: float) -> None:
        if m.get("aid"):
            self._on_SubagentStop(s, m, t)
            return
        if t < s.prompt_t:
            return  # a newer prompt already started another turn
        if s.turn == FINISHED and abs(t - s.turn_t) < 0.5:
            return  # duplicate
        self._end_scope(s, MAIN, t)
        self._progress(s, t)
        background = int(m.get("bg") or 0)
        self._set_turn(s, FINISHED, t, "Response ended, check Claude")
        s.question_hint = bool(m.get("q"))
        s.background = background
        s.boundary_t = max(s.boundary_t, t)

    def _on_StopFailure(self, s: Session, m: dict, t: float) -> None:
        if t < s.prompt_t:
            return
        self._end_scope(s, MAIN, t)
        self._set_turn(s, ERROR, t, f"API error: {m.get('err') or 'unknown'}")
        s.boundary_t = max(s.boundary_t, t)

    def _on_SubagentStart(self, s: Session, m: dict, t: float) -> None:
        aid = m.get("aid")
        if aid:
            s.subagents[aid] = {"type": m.get("at") or "", "start": t, "stop": None}

    def _on_SubagentStop(self, s: Session, m: dict, t: float) -> None:
        aid = m.get("aid")
        if not aid:
            return
        s.subagents.setdefault(aid, {"type": m.get("at") or "", "start": None, "stop": None})["stop"] = t
        self._end_scope(s, aid, t)

    def _on_PreCompact(self, s: Session, m: dict, t: float) -> None:
        if m.get("trig") == "manual" and t > s.turn_t:
            self._set_turn(s, PROCESSING, t, "Compacting conversation")
            s.compacting = True

    def _on_PostCompact(self, s: Session, m: dict, t: float) -> None:
        if m.get("trig") == "manual" and s.compacting and s.turn == PROCESSING:
            self._set_turn(s, IDLE, t, "Compaction finished")
        s.compacting = False

    def _on_Elicitation(self, s: Session, m: dict, t: float) -> None:
        scope, eid, server = self._scope(m), m.get("eid"), m.get("mcp")
        if eid and any(p.eid == eid for p in s.pending.values()):
            return
        key = f"elicit:{eid or server}:{t:.6f}"
        self._add_pending(s, Pending(key, QUESTION, scope, f"Input requested by {server or 'MCP server'}", t,
                                     SRC_ELICIT, mcp=server, eid=eid))
        self._resolve(s, t, lambda p: p.source == SRC_NOTIF_ELICIT)
        if scope == MAIN and t > s.boundary_t:
            self._resume(s, t)

    def _on_ElicitationResult(self, s: Session, m: dict, t: float) -> None:
        eid, server = m.get("eid"), m.get("mcp")
        if eid and any(p.eid == eid for p in s.pending.values()):
            self._resolve(s, t, lambda p: p.eid == eid)
        else:
            self._resolve(s, t, lambda p: p.source in (SRC_ELICIT, SRC_NOTIF_ELICIT)
                          and p.mcp in (None, server) and p.t <= t)

    # ----------------------------------------------------------- outputs

    def display_for(self, s: Session) -> tuple[str, str]:
        if s.ended:
            return UNKNOWN, f"Session ended ({s.end_reason})"
        if s.disconnected:
            return UNKNOWN, "Claude Code process exited without ending the session"
        for kind in (PERMISSION, QUESTION):
            waiting = sorted((p for p in s.pending.values() if p.kind == kind), key=lambda p: p.t)
            if waiting:
                more = f" (+{len(waiting) - 1} more)" if len(waiting) > 1 else ""
                agent = " in a subagent" if waiting[0].scope else ""
                return kind, f"{waiting[0].label}{agent}{more}"
        if s.turn == ERROR:
            return (IDLE, "Error acknowledged") if s.acknowledged else (ERROR, s.detail)
        if s.turn == FINISHED:
            if s.acknowledged:
                return IDLE, "Acknowledged"
            detail = s.detail
            if self.question_hint and s.question_hint:
                detail += " (reply may end with a question)"
            if s.background:
                detail += f"; {s.background} background task(s) still running"
            return FINISHED, detail
        if s.turn == PROCESSING:
            return PROCESSING, s.detail or "Working"
        if s.turn == IDLE:
            return IDLE, s.detail or "Idle"
        return UNKNOWN, "Waiting for the first event from this session"

    def live_sessions(self) -> list[Session]:
        return [s for s in self.sessions.values() if s.live]

    def select(self, mode: str = "latest", pinned: str = "") -> Selection:
        live = self.live_sessions()
        if mode == "pinned" and pinned in self.sessions and self.sessions[pinned].live:
            s = self.sessions[pinned]
            return Selection(*self.display_for(s), s, mode)
        if not live:
            return Selection(UNKNOWN, "No active Claude Code session", None, mode)
        if mode == "aggregate":
            best = max(live, key=lambda s: (PRIORITY[self.display_for(s)[0]], s.activity_t))
        else:
            best = max(live, key=lambda s: (s.activity_t, s.last_t))
        return Selection(*self.display_for(best), best, mode)

    # ----------------------------------------------------------- actions

    def acknowledge(self, sids: list[str]) -> int:
        count = 0
        for sid in sids:
            s = self.sessions.get(sid)
            if s and s.turn in (FINISHED, ERROR) and not s.acknowledged:
                before = self.display_for(s)
                s.acknowledged = True
                self._record(s, "acknowledge", s.last_t, before, time.time())
                count += 1
        return count

    def clear_pending(self, sid: str) -> int:
        """Manual override for a request the evidence never resolved."""
        s = self.sessions.get(sid)
        if not s or not s.pending:
            return 0
        before = self.display_for(s)
        n = len(s.pending)
        s.pending.clear()
        self._record(s, "clear requests", s.last_t, before, time.time())
        return n

    def check_liveness(self, is_alive: Callable[[int], bool | None], now: float | None = None) -> list[Transition]:
        now = time.time() if now is None else now
        changes = []
        for s in self.live_sessions():
            if s.pid and is_alive(s.pid) is False:
                before = self.display_for(s)
                s.disconnected = True
                s.closed_at = now
                tr = self._record(s, "process exited", s.last_t, before, now)
                if tr:
                    changes.append(tr)
        return changes

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for sid, s in list(self.sessions.items()):
            closed_long_ago = s.closed_at is not None and now - s.closed_at > CLOSED_SESSION_TTL
            silent_forever = s.live and not s.pid and now - s.last_t > STALE_SESSION_TTL
            if closed_long_ago or silent_forever:
                del self.sessions[sid]
