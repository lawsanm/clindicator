"""End-to-end without hardware: real relay process -> UDP -> receiver -> core -> lighting (tray-only adapter)."""

import json
import socket
import subprocess
import time

import pytest

from claude_rgb_status import config, hooks_install
from claude_rgb_status.core import Core
from claude_rgb_status.lighting import LightingController

SECRET = "SECRET-PROMPT-TEXT"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_RGB_STATUS_HOME", str(tmp_path))
    cfg = config.Config(device="none", udp_port=free_port(), attention_delay_ms=200)
    c = Core(cfg, lighting=LightingController(device="none"))
    c.start()
    yield c
    c.shutdown()


def relay(payload: dict) -> None:
    proc = subprocess.run([str(hooks_install.hook_python()), "-I", "-S", str(hooks_install.relay_path())],
                          input=json.dumps(payload).encode(), capture_output=True, timeout=30)
    assert proc.returncode == 0 and not proc.stdout and not proc.stderr


def wait_for(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def ev(name, **fields):
    return {"session_id": "it-1", "cwd": "D:\\Project\\integration", "hook_event_name": name, **fields}


def shown(core):
    return core.shown_state


def test_full_pipeline_states_and_lighting(core):
    relay(ev("SessionStart", source="startup"))
    assert wait_for(lambda: shown(core) == "idle")
    relay(ev("UserPromptSubmit", prompt=SECRET))
    assert wait_for(lambda: shown(core) == "processing")
    assert wait_for(lambda: core.lighting.adapter.last_effect == config.effect_for_state(core.cfg, "processing"))

    relay(ev("PreToolUse", tool_name="Bash", tool_input={"command": SECRET}, tool_use_id="toolu_A"))
    relay(ev("PermissionRequest", tool_name="Bash", tool_input={"command": SECRET}))
    assert wait_for(lambda: shown(core) == "permission")
    assert wait_for(lambda: core.lighting.adapter.last_effect.mode == "breath")

    relay(ev("PostToolUse", tool_name="Read", tool_input={"file_path": "x"}, tool_use_id="toolu_B"))
    time.sleep(0.5)
    assert shown(core) == "permission"  # unrelated tool finished

    relay(ev("PostToolUse", tool_name="Bash", tool_input={"command": SECRET}, tool_use_id="toolu_A"))
    assert wait_for(lambda: shown(core) == "processing")
    relay(ev("Stop", last_assistant_message=SECRET))
    assert wait_for(lambda: shown(core) == "finished")

    core.post("ack")
    assert wait_for(lambda: shown(core) == "idle")
    relay(ev("SessionEnd", reason="other"))
    assert wait_for(lambda: shown(core) == "unknown")


def test_status_file_has_metadata_only(core, tmp_path):
    relay(ev("UserPromptSubmit", prompt=SECRET))
    relay(ev("PreToolUse", tool_name="Bash", tool_input={"command": SECRET}, tool_use_id="toolu_A"))
    assert wait_for(lambda: shown(core) == "processing")
    assert wait_for(lambda: (tmp_path / "status.json").exists() and "integration" in (tmp_path / "status.json").read_text("utf-8"))
    core._write_status()
    text = (tmp_path / "status.json").read_text("utf-8")
    assert SECRET not in text
    snap = json.loads(text)
    assert snap["sessions"][0]["project"] == "integration"


def test_receiver_rejects_wrong_token(core):
    before = core.receiver.rejected
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        body = json.dumps({"ev": "Stop", "sid": "evil", "t": time.time()}).encode()
        s.sendto(b"CRS1 wrongtoken\n" + body, ("127.0.0.1", core.receiver.port))
        s.sendto(b"garbage", ("127.0.0.1", core.receiver.port))
    assert wait_for(lambda: core.receiver.rejected >= before + 2)
    assert "evil" not in core.tracker.sessions


def test_pause_shows_restore_profile_and_resume(core):
    relay(ev("UserPromptSubmit"))
    assert wait_for(lambda: shown(core) == "processing")
    core.post("pause", True)
    assert wait_for(lambda: core.lighting.adapter.last_effect == config.restore_effect(core.cfg))
    core.post("pause", False)
    assert wait_for(lambda: core.lighting.adapter.last_effect == config.effect_for_state(core.cfg, "processing"))


def test_test_command_overrides_then_reverts(core):
    relay(ev("UserPromptSubmit"))
    assert wait_for(lambda: shown(core) == "processing")
    core.post("test", "error", 0.5)
    assert wait_for(lambda: core.lighting.adapter.last_effect == config.effect_for_state(core.cfg, "error"))
    assert wait_for(lambda: core.lighting.adapter.last_effect == config.effect_for_state(core.cfg, "processing"))


def test_notifications_fire_once_per_request(core):
    seen = []
    core.on_notify = lambda state, title, message: seen.append((state, message))
    relay(ev("UserPromptSubmit"))
    relay(ev("PermissionRequest", tool_name="Write", tool_input={"file_path": SECRET}))
    assert wait_for(lambda: len(seen) == 1, timeout=3)
    time.sleep(0.6)
    assert len(seen) == 1 and seen[0][0] == "permission" and SECRET not in seen[0][1]
