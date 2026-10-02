"""Simulated-event tests for the session state machine (no hardware, no Claude)."""

import pytest

from claude_rgb_status.state import (
    ERROR, FINISHED, IDLE, PERMISSION, PROCESSING, QUESTION, UNKNOWN, Tracker,
)


class Sim:
    """Feeds relay-shaped messages for one session with increasing timestamps."""

    def __init__(self, tracker=None, sid="sess-a", cwd="D:\\Project\\alpha", pid=4242, t=1000.0):
        self.tr = tracker or Tracker()
        self.sid, self.cwd, self.pid, self.t = sid, cwd, pid, t

    def ev(self, name, when=None, **fields):
        if when is None:
            self.t += 1.0
            when = self.t
        msg = {"v": 1, "ev": name, "sid": self.sid, "cwd": self.cwd, "pid": self.pid, "t": when, **fields}
        return self.tr.apply(msg, now=when)

    def tool(self, phase, tool, tuid, ih="h1", **fields):
        return self.ev(phase, tool=tool, tuid=tuid, ih=ih, **fields)

    @property
    def session(self):
        return self.tr.sessions[self.sid]

    @property
    def state(self):
        return self.tr.display_for(self.session)[0]

    @property
    def detail(self):
        return self.tr.display_for(self.session)[1]

    def start_turn(self):
        self.ev("SessionStart", src="startup")
        self.ev("UserPromptSubmit")


@pytest.fixture
def sim():
    return Sim()


# --------------------------------------------------------------- basic turn

def test_basic_turn(sim):
    sim.ev("SessionStart", src="startup")
    assert sim.state == IDLE
    sim.ev("UserPromptSubmit")
    assert sim.state == PROCESSING
    sim.tool("PreToolUse", "Read", "t1")
    sim.tool("PostToolUse", "Read", "t1")
    assert sim.state == PROCESSING
    sim.ev("Stop", q=False, bg=0)
    assert sim.state == FINISHED and "check Claude" in sim.detail
    sim.tr.acknowledge([sim.sid])
    assert sim.state == IDLE
    sim.ev("UserPromptSubmit")
    assert sim.state == PROCESSING


def test_finished_stays_until_acknowledged_or_new_work(sim):
    sim.start_turn()
    sim.ev("Stop")
    for _ in range(5):
        sim.ev("Notification", nt="auth_success")
    assert sim.state == FINISHED


# --------------------------------------------------------------- permissions

def test_permission_flow(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "Bash", "A", ih="cmd1")
    sim.ev("PermissionRequest", tool="Bash", ih="cmd1")
    assert sim.state == PERMISSION and sim.detail == "Bash"
    sim.tool("PostToolUse", "Bash", "A", ih="cmd1")
    assert sim.state == PROCESSING
    sim.ev("Stop")
    assert sim.state == FINISHED


def test_unrelated_parallel_tool_does_not_clear_permission(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "Read", "B", ih="read1")
    sim.tool("PreToolUse", "Bash", "A", ih="cmd1")
    sim.ev("PermissionRequest", tool="Bash", ih="cmd1")
    sim.tool("PostToolUse", "Read", "B", ih="read1")
    sim.tool("PostToolUseFailure", "Grep", "C", ih="g1", intr=False)
    assert sim.state == PERMISSION
    sim.tool("PostToolUse", "Bash", "A", ih="cmd1")
    assert sim.state == PROCESSING


def test_two_parallel_permissions_need_both_answers(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "Bash", "A", ih="a")
    sim.tool("PreToolUse", "Write", "B", ih="b")
    sim.ev("PermissionRequest", tool="Bash", ih="a")
    sim.ev("PermissionRequest", tool="Write", ih="b")
    assert sim.state == PERMISSION and "+1 more" in sim.detail
    sim.tool("PostToolUse", "Bash", "A", ih="a")
    assert sim.state == PERMISSION and sim.detail == "Write"
    sim.tool("PostToolUse", "Write", "B", ih="b")
    assert sim.state == PROCESSING


def test_permission_request_processed_before_its_pretooluse(sim):
    sim.start_turn()
    base = sim.t
    sim.ev("PermissionRequest", when=base + 1.005, tool="Bash", ih="cmd1")
    sim.tool("PreToolUse", "Bash", "A", ih="cmd1", when=base + 1.0)
    sim.t = base + 2
    assert sim.state == PERMISSION and "more" not in sim.detail
    sim.tool("PostToolUse", "Bash", "A", ih="cmd1")
    assert sim.state == PROCESSING


def test_permission_answered_before_request_seen(sim):
    """Another hook auto-approved within milliseconds; the late request must not light amber."""
    sim.start_turn()
    base = sim.t
    sim.tool("PreToolUse", "Bash", "A", ih="cmd1", when=base + 1.0)
    sim.tool("PostToolUse", "Bash", "A", ih="cmd1", when=base + 1.02)
    sim.ev("PermissionRequest", when=base + 1.01, tool="Bash", ih="cmd1")
    sim.t = base + 2
    assert sim.state == PROCESSING


def test_permission_without_pretooluse_resolved_by_input_hash(sim):
    sim.start_turn()
    sim.ev("PermissionRequest", tool="Edit", ih="e1")
    assert sim.state == PERMISSION
    sim.tool("PostToolUse", "Edit", "E", ih="e1")
    assert sim.state == PROCESSING


def test_denied_permission_then_interrupt_clears_via_idle_prompt(sim):
    """Manual denial fires no Post* event and an interrupt fires no Stop."""
    sim.start_turn()
    sim.tool("PreToolUse", "Bash", "A")
    sim.ev("PermissionRequest", tool="Bash", ih="h1")
    sim.ev("Notification", nt="permission_prompt")  # user was away
    assert sim.state == PERMISSION
    sim.t += 60
    sim.ev("Notification", nt="idle_prompt")
    assert sim.state == FINISHED and "without a Stop" in sim.detail
    assert not sim.session.pending


def test_new_prompt_clears_stale_permission(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "Bash", "A")
    sim.ev("PermissionRequest", tool="Bash", ih="h1")
    sim.ev("UserPromptSubmit")
    assert sim.state == PROCESSING and not sim.session.pending


def test_auto_mode_denial_resolves_request(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "Bash", "A")
    sim.ev("PermissionRequest", tool="Bash", ih="h1")
    sim.tool("PermissionDenied", "Bash", "A")
    assert sim.state == PROCESSING


def test_mcp_tool_label(sim):
    sim.start_turn()
    sim.ev("PermissionRequest", tool="mcp__github__create_issue", ih="x")
    assert sim.detail == "github: create_issue"


def test_plan_approval_is_permission(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "ExitPlanMode", "P")
    assert sim.state == PERMISSION and sim.detail == "Plan approval"
    sim.ev("PermissionRequest", tool="ExitPlanMode", ih="h1")
    assert "more" not in sim.detail
    sim.tool("PostToolUse", "ExitPlanMode", "P")
    assert sim.state == PROCESSING


# --------------------------------------------------------------- questions

def test_ask_user_question(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "AskUserQuestion", "Q")
    assert sim.state == QUESTION
    sim.ev("PermissionRequest", tool="AskUserQuestion", ih="h1")
    assert sim.state == QUESTION and "more" not in sim.detail
    sim.tool("PostToolUse", "AskUserQuestion", "Q")
    assert sim.state == PROCESSING


def test_question_permission_request_first(sim):
    sim.start_turn()
    base = sim.t
    sim.ev("PermissionRequest", when=base + 1.01, tool="AskUserQuestion", ih="q1")
    sim.tool("PreToolUse", "AskUserQuestion", "Q", ih="q1", when=base + 1.0)
    sim.t = base + 2
    assert sim.state == QUESTION and "more" not in sim.detail
    sim.tool("PostToolUse", "AskUserQuestion", "Q", ih="q1")
    assert sim.state == PROCESSING


def test_away_notification_does_not_turn_question_into_permission(sim):
    """Regression from the live test: Claude Code sends permission_prompt ~6 s after an
    AskUserQuestion dialog appears; it must stay purple."""
    sim.start_turn()
    sim.tool("PreToolUse", "AskUserQuestion", "Q")
    sim.t += 6
    sim.ev("Notification", nt="permission_prompt")
    sim.ev("Notification", nt="elicitation_dialog")
    assert sim.state == QUESTION and "more" not in sim.detail
    sim.ev("PostToolBatch", batch=[["Q", "AskUserQuestion", "h1"]])
    assert sim.state == PROCESSING


def test_unrelated_tool_does_not_clear_question(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "Read", "R")
    sim.tool("PreToolUse", "AskUserQuestion", "Q")
    sim.tool("PostToolUse", "Read", "R")
    assert sim.state == QUESTION


def test_plain_text_question_is_finished_with_hint(sim):
    sim.start_turn()
    sim.ev("Stop", q=True, bg=0)
    assert sim.state == FINISHED and "may end with a question" in sim.detail
    sim.tr.question_hint = False
    assert "question" not in sim.detail


def test_elicitation_flow(sim):
    sim.start_turn()
    sim.ev("Elicitation", mcp="srv", eid="e1", mode="form")
    assert sim.state == QUESTION and "srv" in sim.detail
    sim.ev("ElicitationResult", mcp="srv", eid="e1", act="accept")
    assert sim.state == PROCESSING


def test_elicitation_cleared_when_its_mcp_tool_finishes(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "mcp__github__create_issue", "M")
    sim.ev("Elicitation", mcp="github")
    sim.tool("PostToolUse", "Read", "R")  # unrelated
    assert sim.state == QUESTION
    sim.tool("PostToolUse", "mcp__github__create_issue", "M")
    assert sim.state == PROCESSING


def test_elicitation_dialog_notification_fallback(sim):
    sim.start_turn()
    sim.ev("Notification", nt="elicitation_dialog")
    assert sim.state == QUESTION
    sim.ev("Notification", nt="elicitation_response")
    assert sim.state == PROCESSING


def test_idle_prompt_is_not_a_question(sim):
    sim.start_turn()
    sim.ev("Stop")
    sim.t += 60
    sim.ev("Notification", nt="idle_prompt")
    assert sim.state == FINISHED


def test_agent_needs_input_until_progress(sim):
    sim.start_turn()
    sim.ev("Notification", nt="agent_needs_input")
    assert sim.state == QUESTION
    sim.tool("PreToolUse", "Read", "R")
    assert sim.state == PROCESSING


# --------------------------------------------------------------- errors

def test_stop_failure_is_error_until_ack_or_new_prompt(sim):
    sim.start_turn()
    sim.ev("StopFailure", err="rate_limit")
    assert sim.state == ERROR and "rate_limit" in sim.detail
    sim.tr.acknowledge([sim.sid])
    assert sim.state == IDLE
    sim.ev("UserPromptSubmit")
    sim.ev("StopFailure", err="overloaded")
    sim.ev("UserPromptSubmit")
    assert sim.state == PROCESSING


def test_failed_tool_is_not_a_terminal_error(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "Bash", "A")
    sim.tool("PostToolUseFailure", "Bash", "A", intr=False)
    assert sim.state == PROCESSING
    sim.tool("PreToolUse", "Bash", "B")  # Claude recovers
    sim.ev("Stop")
    assert sim.state == FINISHED


def test_quota_notifications(sim):
    sim.start_turn()
    sim.ev("StopFailure", err="rate_limit")
    sim.ev("Notification", nt="quota_auto_resume_stale")
    assert sim.state == QUESTION
    sim.ev("Notification", nt="quota_auto_resume_fired")
    assert sim.state == PROCESSING
    sim.ev("Notification", nt="quota_auto_resume_disabled")
    assert sim.state == ERROR


# --------------------------------------------------------------- ordering

def test_late_tool_events_after_stop_are_ignored(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "Bash", "A")
    stop_at = sim.t + 5
    sim.ev("Stop", when=stop_at)
    sim.tool("PostToolUse", "Bash", "A", when=stop_at - 0.5)
    sim.tool("PreToolUse", "Read", "late", when=stop_at - 0.2)
    sim.ev("PermissionRequest", when=stop_at - 0.1, tool="Read", ih="h1")
    assert sim.state == FINISHED


def test_late_stop_after_new_prompt_is_ignored(sim):
    sim.start_turn()
    sim.ev("UserPromptSubmit", when=sim.t + 10)
    sim.ev("Stop", when=sim.t - 1)
    assert sim.state == PROCESSING


def test_late_idle_prompt_after_new_prompt_is_ignored(sim):
    sim.start_turn()
    sim.ev("Stop")
    prompt_at = sim.t + 60
    sim.ev("UserPromptSubmit", when=prompt_at)
    sim.ev("Notification", when=prompt_at - 0.3, nt="idle_prompt")
    assert sim.state == PROCESSING


def test_late_permission_prompt_notification_after_answer(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "Bash", "A")
    sim.ev("PermissionRequest", tool="Bash", ih="h1")
    sent = sim.t + 6
    sim.tool("PostToolUse", "Bash", "A", when=sent + 0.5)
    sim.ev("Notification", when=sent, nt="permission_prompt")
    sim.t = sent + 1
    assert sim.state == PROCESSING


def test_permission_prompt_notification_without_request(sim):
    """Sandboxed network requests fire no PermissionRequest hook."""
    sim.start_turn()
    sim.tool("PreToolUse", "Bash", "A")
    sim.ev("Notification", nt="permission_prompt")
    assert sim.state == PERMISSION
    sim.tool("PostToolUse", "Read", "R")  # unrelated, must not clear
    assert sim.state == PERMISSION
    sim.tool("PostToolUse", "Bash", "A")
    assert sim.state == PROCESSING


def test_duplicate_events_are_idempotent(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "Bash", "A")
    sim.tool("PreToolUse", "Bash", "A")
    sim.ev("PermissionRequest", tool="Bash", ih="h1")
    sim.ev("PermissionRequest", tool="Bash", ih="h1")
    assert sim.state == PERMISSION and "more" not in sim.detail
    sim.tool("PostToolUse", "Bash", "A")
    stop_at = sim.t + 1
    first = sim.ev("Stop", when=stop_at)
    second = sim.ev("Stop", when=stop_at + 0.1)
    assert first is not None and second is None and sim.state == FINISHED


def test_work_resumes_after_stop(sim):
    sim.start_turn()
    sim.ev("Stop")
    sim.tool("PreToolUse", "Bash", "B")  # e.g. a background task woke Claude
    assert sim.state == PROCESSING


# --------------------------------------------------------------- subagents

def test_subagent_stop_does_not_finish_parent(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "Agent", "AG")
    sim.ev("SubagentStart", aid="a1", at="Explore")
    sim.tool("PreToolUse", "Read", "r1", aid="a1")
    sim.tool("PostToolUse", "Read", "r1", aid="a1")
    sim.ev("SubagentStop", aid="a1", at="Explore")
    assert sim.state == PROCESSING
    sim.ev("Stop", aid="a1")  # a Stop reported from inside a subagent
    assert sim.state == PROCESSING
    sim.tool("PostToolUse", "Agent", "AG")
    sim.ev("Stop")
    assert sim.state == FINISHED


def test_subagent_permission_survives_main_agent_events(sim):
    sim.start_turn()
    sim.ev("SubagentStart", aid="a1")
    sim.tool("PreToolUse", "Bash", "s1", aid="a1")
    sim.ev("PermissionRequest", tool="Bash", ih="h1", aid="a1")
    assert sim.state == PERMISSION and "subagent" in sim.detail
    sim.tool("PostToolUse", "Read", "m1")
    sim.ev("PostToolBatch", batch=[["m1", "Read", "h1"]])
    assert sim.state == PERMISSION
    sim.ev("Stop")  # main agent finished; background subagent still waiting
    assert sim.state == PERMISSION
    sim.ev("SubagentStop", aid="a1")
    assert sim.state == FINISHED


def test_background_subagent_activity_keeps_finished(sim):
    sim.start_turn()
    sim.ev("Stop", bg=1)
    sim.tool("PreToolUse", "Read", "x", aid="bg1")
    assert sim.state == FINISHED and "background" in sim.detail


def test_internal_subagent_stop_without_id_is_ignored(sim):
    sim.start_turn()
    sim.ev("Stop")
    sim.ev("SubagentStop", at="")
    assert sim.state == FINISHED


def test_post_tool_batch_resolves_its_scope(sim):
    sim.start_turn()
    sim.ev("PermissionRequest", tool="Bash", ih="zz")  # PreToolUse missed
    sim.ev("PostToolBatch", batch=[["toolu_9", "Bash", "different-hash"]])
    assert sim.state == PROCESSING


# --------------------------------------------------------------- sessions

def test_multi_session_latest_aggregate_pinned():
    tr = Tracker()
    a = Sim(tr, sid="A", cwd="D:\\p\\alpha", pid=1, t=100)
    b = Sim(tr, sid="B", cwd="D:\\p\\beta", pid=2, t=200)
    a.start_turn()
    b.start_turn()
    a.ev("PermissionRequest", tool="Bash", ih="h")
    latest = tr.select("latest")
    assert latest.session.sid == "B" and latest.state == PROCESSING
    agg = tr.select("aggregate")
    assert agg.session.sid == "A" and agg.state == PERMISSION
    pinned = tr.select("pinned", "A")
    assert pinned.state == PERMISSION and pinned.session.project == "alpha"


def test_aggregate_prefers_attention_over_work():
    tr = Tracker()
    a = Sim(tr, sid="A", t=100)
    b = Sim(tr, sid="B", t=200)
    a.start_turn()
    a.ev("Stop")
    b.start_turn()
    assert tr.select("aggregate").session.sid == "A"
    tr.acknowledge(["A"])
    assert tr.select("aggregate").session.sid == "B"


def test_session_end_and_no_sessions(sim):
    sim.start_turn()
    sim.ev("SessionEnd", rsn="prompt_input_exit")
    assert not sim.session.live
    sel = sim.tr.select("latest")
    assert sel.state == UNKNOWN and sel.session is None
    sim.ev("Stop")  # late async event after the end
    assert sim.tr.select("latest").session is None


def test_disconnected_process_and_revival(sim):
    sim.start_turn()
    changes = sim.tr.check_liveness(lambda pid: False)
    assert changes and sim.state == UNKNOWN and "exited" in sim.detail
    assert sim.tr.select("latest").state == UNKNOWN
    sim.pid = 5555
    sim.ev("SessionStart", src="resume")
    assert sim.state == IDLE and sim.session.pid == 5555


def test_long_silent_work_is_not_a_failure(sim):
    sim.start_turn()
    sim.tool("PreToolUse", "Bash", "long")
    sim.tr.check_liveness(lambda pid: True, now=sim.t + 3600)
    sim.tr.prune(now=sim.t + 3600)
    assert sim.state == PROCESSING


def test_unknown_liveness_never_disconnects(sim):
    sim.start_turn()
    sim.tr.check_liveness(lambda pid: None)
    assert sim.state == PROCESSING


def test_app_started_mid_turn(sim):
    sim.tool("PostToolUse", "Read", "r")
    assert sim.state == PROCESSING


def test_compaction(sim):
    sim.ev("SessionStart", src="startup")
    sim.ev("PreCompact", trig="manual")
    assert sim.state == PROCESSING and "Compacting" in sim.detail
    sim.ev("SessionStart", src="compact")
    sim.ev("PostCompact", trig="manual")
    assert sim.state == IDLE
    sim.ev("UserPromptSubmit")
    sim.ev("PreCompact", trig="auto")
    sim.ev("PostCompact", trig="auto")
    assert sim.state == PROCESSING


def test_clear_pending_override_and_history(sim):
    sim.start_turn()
    sim.ev("PermissionRequest", tool="Bash", ih="h")
    assert sim.tr.clear_pending(sim.sid) == 1
    assert sim.state == PROCESSING
    events = [tr.event for tr in sim.tr.history]
    assert "PermissionRequest" in events and "clear requests" in events


def test_prune_removes_closed_sessions(sim):
    sim.start_turn()
    sim.ev("SessionEnd", rsn="other")
    sim.tr.prune(now=sim.session.closed_at + 601)
    assert sim.sid not in sim.tr.sessions
