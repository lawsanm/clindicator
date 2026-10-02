"""Send simulated Claude Code hook payloads through the real hook relay to the running app.

This exercises relay -> UDP -> receiver -> state machine -> lighting exactly like
Claude Code would, but with scripted events. Simulated sessions are labeled
"simulated-*" so they are easy to tell apart in the tray and diagnostics.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid

from .hooks_install import hook_python, relay_path

# (pause before the step in seconds, event name, extra payload fields, description)
Step = tuple[float, str, dict, str]

SCENARIOS: dict[str, list[Step]] = {
    "demo": [
        (0, "SessionStart", {"source": "startup"}, "session starts: dim white"),
        (3, "UserPromptSubmit", {"prompt": "(simulated)"}, "prompt submitted: blue"),
        (1, "PreToolUse", {"tool_name": "Read", "tool_input": {"file_path": "x"}, "tool_use_id": "sim_r1"}, "tool runs"),
        (1, "PostToolUse", {"tool_name": "Read", "tool_input": {"file_path": "x"}, "tool_use_id": "sim_r1"}, "tool done"),
        (2, "PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "x"}, "tool_use_id": "sim_b1"}, "Bash call"),
        (0, "PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "x"}}, "permission needed: amber pulse"),
        (8, "PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "x"}, "tool_use_id": "sim_b1"}, "approved: blue"),
        (3, "PreToolUse", {"tool_name": "AskUserQuestion", "tool_input": {"questions": []}, "tool_use_id": "sim_q1"},
         "question asked: purple pulse"),
        (8, "PostToolUse", {"tool_name": "AskUserQuestion", "tool_input": {"questions": []}, "tool_use_id": "sim_q1"},
         "answered: blue"),
        (3, "Stop", {"last_assistant_message": "Done."}, "response finished: green until acknowledged"),
    ],
    "permission": [
        (0, "UserPromptSubmit", {}, "blue"),
        (1, "PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "y"}, "tool_use_id": "sim_b2"}, "Bash call"),
        (0, "PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "y"}}, "amber pulse"),
        (2, "PostToolUse", {"tool_name": "Read", "tool_input": {"file_path": "z"}, "tool_use_id": "sim_r9"},
         "unrelated parallel tool finishes: still amber"),
        (6, "PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "y"}, "tool_use_id": "sim_b2"}, "blue"),
        (2, "Stop", {}, "green"),
    ],
    "question": [
        (0, "UserPromptSubmit", {}, "blue"),
        (1, "PreToolUse", {"tool_name": "AskUserQuestion", "tool_input": {"q": 1}, "tool_use_id": "sim_q2"}, "purple pulse"),
        (8, "PostToolUse", {"tool_name": "AskUserQuestion", "tool_input": {"q": 1}, "tool_use_id": "sim_q2"}, "blue"),
        (2, "Stop", {"last_assistant_message": "Should I also update the docs?"},
         "green (plain-text question: hint only, not purple)"),
    ],
    "error": [
        (0, "UserPromptSubmit", {}, "blue"),
        (2, "StopFailure", {"error": "rate_limit", "error_details": "429"}, "red until acknowledged"),
    ],
    "subagent": [
        (0, "UserPromptSubmit", {}, "blue"),
        (1, "PreToolUse", {"tool_name": "Agent", "tool_input": {"p": 1}, "tool_use_id": "sim_a1"}, "subagent launched"),
        (0, "SubagentStart", {"agent_id": "sim-sub", "agent_type": "Explore"}, ""),
        (1, "PreToolUse", {"tool_name": "Grep", "tool_input": {"p": 2}, "tool_use_id": "sim_g1", "agent_id": "sim-sub"}, ""),
        (1, "SubagentStop", {"agent_id": "sim-sub", "agent_type": "Explore"}, "subagent finished: parent still blue"),
        (3, "PostToolUse", {"tool_name": "Agent", "tool_input": {"p": 1}, "tool_use_id": "sim_a1"}, "still blue"),
        (2, "Stop", {}, "green"),
    ],
}


def send(payload: dict) -> int:
    proc = subprocess.run([str(hook_python()), "-I", "-S", str(relay_path())],
                          input=json.dumps(payload).encode(), capture_output=True, timeout=15)
    return proc.returncode


def run(name: str, *, end: bool = True, project: str = "simulated-project", speed: float = 1.0) -> None:
    steps = SCENARIOS[name]
    sid = f"sim-{uuid.uuid4().hex[:12]}"
    base = {"session_id": sid, "cwd": f"C:\\simulated\\{project}", "transcript_path": "", "permission_mode": "default"}
    print(f"Simulating '{name}' as session {sid[:8]} ({project})")
    for pause, event, fields, description in steps:
        time.sleep(pause / speed)
        send({**base, "hook_event_name": event, **fields})
        print(f"  {event:<18} {description}", flush=True)
    if end:
        time.sleep(6 / speed)
        send({**base, "hook_event_name": "SessionEnd", "reason": "other"})
        print("  SessionEnd         simulated session closed")
