import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_rgb_status import hook_relay
from claude_rgb_status.hook_relay import extract, input_hash, looks_like_question

RELAY = Path(hook_relay.__file__)
PYTHON = getattr(sys, "_base_executable", sys.executable)  # what the installed hook uses
SECRET = "TOP-SECRET-CONTENT"
BASE = {"session_id": "s1", "cwd": "D:\\Project\\demo", "transcript_path": "C:\\t.jsonl", "permission_mode": "default"}


def event(name, **fields):
    return {**BASE, "hook_event_name": name, **fields}


SAMPLES = [
    event("UserPromptSubmit", prompt=SECRET),
    event("PreToolUse", tool_name="Bash", tool_input={"command": SECRET}, tool_use_id="toolu_1"),
    event("PermissionRequest", tool_name="Bash", tool_input={"command": SECRET},
          permission_suggestions=[{"rules": [{"ruleContent": SECRET}]}]),
    event("PostToolUse", tool_name="Bash", tool_input={"command": SECRET}, tool_use_id="toolu_1",
          tool_response={"stdout": SECRET}),
    event("PostToolUseFailure", tool_name="Bash", tool_input={"command": SECRET}, tool_use_id="toolu_1",
          error=SECRET, is_interrupt=False),
    event("PostToolBatch", tool_calls=[{"tool_name": "Read", "tool_input": {"file_path": SECRET},
                                        "tool_use_id": "toolu_2", "tool_response": SECRET}]),
    event("Notification", notification_type="permission_prompt", message=SECRET, title=SECRET),
    event("Stop", last_assistant_message=SECRET + "\nShall I continue?", stop_hook_active=False,
          background_tasks=[{"command": SECRET}]),
    event("SubagentStop", agent_id="a1", agent_type="Explore", last_assistant_message=SECRET,
          agent_transcript_path=SECRET),
    event("StopFailure", error="rate_limit", error_details=SECRET, last_assistant_message=SECRET),
    event("Elicitation", mcp_server_name="srv", message=SECRET, mode="form", requested_schema={"x": SECRET}),
    event("ElicitationResult", mcp_server_name="srv", action="accept", content={"x": SECRET}, elicitation_id="e1"),
    event("PreCompact", trigger="manual", custom_instructions=SECRET),
    event("PostCompact", trigger="manual", compact_summary=SECRET),
    event("SessionStart", source="startup", model="claude-x"),
    event("SessionEnd", reason="prompt_input_exit"),
]


@pytest.mark.parametrize("payload", SAMPLES, ids=[s["hook_event_name"] for s in SAMPLES])
def test_extract_never_forwards_content(payload):
    msg = extract(payload)
    text = json.dumps(msg)
    assert SECRET not in text
    assert "transcript" not in text
    assert msg["ev"] == payload["hook_event_name"] and msg["sid"] == "s1"


def test_tool_fields_and_hash_correlation():
    pre, perm, post = (extract(SAMPLES[i]) for i in (1, 2, 3))
    assert pre["tool"] == "Bash" and pre["tuid"] == "toolu_1"
    assert "tuid" not in perm  # PermissionRequest carries no tool_use_id
    assert pre["ih"] == perm["ih"] == post["ih"] == input_hash({"command": SECRET})
    assert input_hash({"command": "other"}) != pre["ih"]


def test_event_specific_fields():
    by_name = {s["hook_event_name"]: extract(s) for s in SAMPLES}
    assert by_name["Notification"]["nt"] == "permission_prompt"
    assert by_name["Stop"]["q"] is True and by_name["Stop"]["bg"] == 1
    assert by_name["StopFailure"]["err"] == "rate_limit"
    assert by_name["PostToolBatch"]["batch"] == [["toolu_2", "Read", input_hash({"file_path": SECRET})]]
    assert by_name["ElicitationResult"]["act"] == "accept" and by_name["ElicitationResult"]["eid"] == "e1"
    assert by_name["SubagentStop"]["aid"] == "a1"
    assert by_name["SessionStart"]["src"] == "startup" and by_name["SessionEnd"]["rsn"] == "prompt_input_exit"
    assert by_name["PreCompact"]["trig"] == "manual"


@pytest.mark.parametrize("text,expected", [
    ("All done. Tests pass.", False),
    ("I changed two files.\n\nWant me to also update the docs?", True),
    ("Which option do you prefer?\n\n**Note:** both are safe.", True),
    ("Fixed `re.match(r'a?')`.\nCommitted.\nPushed.\nDone.", False),
    (None, False),
])
def test_question_hint(text, expected):
    assert looks_like_question(text) is expected


@pytest.fixture
def listener(tmp_path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(5)
    endpoint = tmp_path / "endpoint.json"
    endpoint.write_text(json.dumps({"port": sock.getsockname()[1], "token": "tok123"}), encoding="utf-8")
    yield sock, endpoint
    sock.close()


def run_relay(payload, endpoint):
    stdin = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    start = time.perf_counter()
    proc = subprocess.run([PYTHON, "-I", "-S", str(RELAY), "--endpoint", str(endpoint)],
                          input=stdin, capture_output=True, timeout=30)
    return proc, time.perf_counter() - start


def test_relay_process_sends_datagram_silently(listener):
    sock, endpoint = listener
    proc, elapsed = run_relay(SAMPLES[1], endpoint)
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""
    data, _ = sock.recvfrom(65535)
    header, body = data.split(b"\n", 1)
    assert header == b"CRS1 tok123"
    msg = json.loads(body)
    assert msg["ev"] == "PreToolUse" and msg["tuid"] == "toolu_1" and SECRET not in body.decode()
    assert isinstance(msg["pid"], int) and msg["pid"] > 0
    assert msg["t"] <= msg["t0"] + 1 and msg["t0"] - msg["t"] < 5
    assert elapsed < 5


def test_relay_silent_when_app_never_ran(tmp_path):
    proc, elapsed = run_relay(SAMPLES[0], tmp_path / "missing.json")
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""


def test_relay_silent_when_nothing_listens(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    endpoint = tmp_path / "endpoint.json"
    endpoint.write_text(json.dumps({"port": port, "token": "x"}), encoding="utf-8")
    proc, elapsed = run_relay(SAMPLES[7], endpoint)
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""
    assert elapsed < 5


@pytest.mark.parametrize("stdin", [b"", b"not json", b"[1,2]", b"{\"no_event\": 1}", b"\xff\xfe\x00garbage"])
def test_relay_ignores_bad_input(listener, stdin):
    sock, endpoint = listener
    proc, _ = run_relay(stdin, endpoint)
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""
    sock.settimeout(0.3)
    with pytest.raises(socket.timeout):
        sock.recvfrom(65535)


def test_relay_latency(listener, capsys):
    _, endpoint = listener
    times = [run_relay(SAMPLES[1], endpoint)[1] for _ in range(5)]
    with capsys.disabled():
        print(f"\n  relay wall time per event: min {min(times) * 1000:.0f} ms, max {max(times) * 1000:.0f} ms")
    assert min(times) < 1.0
