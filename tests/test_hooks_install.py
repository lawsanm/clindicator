import json
from pathlib import Path

import pytest

from claude_rgb_status import hooks_install as hi

USER_SETTINGS = {
    "model": "opus",
    "theme": "auto",
    "autoMode": {"environment": ["**Internal sharing**: None configured — treat public paste/gist services as outside"]},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "C:\\tools\\guard.exe", "args": ["--strict"]}]}
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
        "Notification": [],
    },
}


def write(path: Path, data) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return text


@pytest.fixture
def env(tmp_path):
    return {
        "settings": tmp_path / ".claude" / "settings.json",
        "data": tmp_path / "appdata",
        "python": hi.hook_python(),
        "relay": hi.relay_path(),
    }


def do_install(env):
    return hi.install(env["settings"], python=env["python"], relay=env["relay"], data_dir=env["data"])


def test_handler_shape_is_safe(env):
    h = hi.make_handler(env["python"], env["relay"])
    assert h["type"] == "command" and h["async"] is True
    assert h["args"][:2] == ["-I", "-S"] and h["args"][2].endswith("hook_relay.py")
    assert "async" not in hi.make_handler(env["python"], env["relay"], sync=True)
    blob = json.dumps(h)
    for forbidden in ("decision", "permissionDecision", "updatedInput", "if"):
        assert f'"{forbidden}"' not in blob
    assert hi.is_ours(h)


def test_install_into_missing_file(env):
    saved, count = do_install(env)
    assert saved is None and count == len(hi.ALL_EVENTS)
    data = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert sorted(hi.installed_events(data)) == sorted(hi.ALL_EVENTS)
    session_end = data["hooks"]["SessionEnd"][0]["hooks"][0]
    assert "async" not in session_end
    assert all("matcher" not in g for groups in data["hooks"].values() for g in groups)


def test_install_preserves_everything_and_backs_up(env):
    env["settings"].parent.mkdir(parents=True)
    original = write(env["settings"], USER_SETTINGS)
    saved, count = do_install(env)
    assert saved is not None and saved.read_text(encoding="utf-8") == original
    data = json.loads(env["settings"].read_text(encoding="utf-8"))
    for key in ("model", "theme", "autoMode"):
        assert data[key] == USER_SETTINGS[key]
    # user hooks untouched and still first
    assert data["hooks"]["PreToolUse"][0] == USER_SETTINGS["hooks"]["PreToolUse"][0]
    assert data["hooks"]["Stop"][0] == USER_SETTINGS["hooks"]["Stop"][0]
    assert hi.is_ours(data["hooks"]["PreToolUse"][1]["hooks"][0])
    assert "—" in env["settings"].read_text(encoding="utf-8")  # non-ASCII kept as-is


def test_install_is_idempotent(env):
    env["settings"].parent.mkdir(parents=True)
    write(env["settings"], USER_SETTINGS)
    do_install(env)
    first = env["settings"].read_bytes()
    mtime = env["settings"].stat().st_mtime_ns
    saved, count = do_install(env)
    assert saved is None and count == len(hi.ALL_EVENTS)
    assert env["settings"].read_bytes() == first and env["settings"].stat().st_mtime_ns == mtime
    data = json.loads(first)
    ours = sum(hi.is_ours(h) for groups in data["hooks"].values() for g in groups for h in g["hooks"])
    assert ours == len(hi.ALL_EVENTS)


def test_reinstall_after_move_replaces_old_entries(env, tmp_path):
    env["settings"].parent.mkdir(parents=True)
    write(env["settings"], USER_SETTINGS)
    old_relay = Path("C:/old/place/src/claude_rgb_status/hook_relay.py")
    stale = hi.add_ours(USER_SETTINGS, env["python"], old_relay)
    write(env["settings"], stale)
    do_install(env)
    data = json.loads(env["settings"].read_text(encoding="utf-8"))
    args = [h["args"][2] for groups in data["hooks"].values() for g in groups for h in g["hooks"] if hi.is_ours(h)]
    assert len(args) == len(hi.ALL_EVENTS) and all(a == str(env["relay"]) for a in args)


def test_install_then_uninstall_restores_exact_bytes(env):
    env["settings"].parent.mkdir(parents=True)
    original = write(env["settings"], USER_SETTINGS)
    do_install(env)
    saved, removed = hi.uninstall(env["settings"], data_dir=env["data"])
    assert removed == len(hi.ALL_EVENTS) and saved is not None
    assert env["settings"].read_text(encoding="utf-8") == original


def test_uninstall_without_prior_hooks_key_removes_it(env):
    env["settings"].parent.mkdir(parents=True)
    original = write(env["settings"], {"model": "opus"})
    do_install(env)
    hi.uninstall(env["settings"], data_dir=env["data"])
    assert env["settings"].read_text(encoding="utf-8") == original


def test_uninstall_keeps_user_handler_added_to_our_group(env):
    env["settings"].parent.mkdir(parents=True)
    write(env["settings"], {})
    do_install(env)
    data = json.loads(env["settings"].read_text(encoding="utf-8"))
    mine = {"type": "command", "command": "echo mine"}
    data["hooks"]["Stop"][0]["hooks"].append(mine)
    write(env["settings"], data)
    hi.uninstall(env["settings"], data_dir=env["data"])
    after = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert after == {"hooks": {"Stop": [{"hooks": [mine]}]}}


def test_uninstall_when_nothing_installed_is_a_no_op(env):
    env["settings"].parent.mkdir(parents=True)
    original = write(env["settings"], USER_SETTINGS)
    saved, removed = hi.uninstall(env["settings"], data_dir=env["data"])
    assert (saved, removed) == (None, 0) and env["settings"].read_text(encoding="utf-8") == original


@pytest.mark.parametrize("content", ["{ not json", "[1, 2]", '{"hooks": []}', '{"hooks": {"Stop": {}}}'])
def test_refuses_to_touch_bad_files(env, content):
    env["settings"].parent.mkdir(parents=True)
    env["settings"].write_text(content, encoding="utf-8")
    with pytest.raises(hi.InstallError):
        do_install(env)
    assert env["settings"].read_text(encoding="utf-8") == content


def test_selftest_detects_missing_interpreter(env, tmp_path):
    with pytest.raises(hi.InstallError):
        hi.selftest_relay(tmp_path / "nope" / "python.exe", env["relay"])


def test_preexisting_empty_hooks_object_survives(env):
    env["settings"].parent.mkdir(parents=True)
    original = write(env["settings"], {"model": "opus", "hooks": {}})
    do_install(env)
    hi.uninstall(env["settings"], data_dir=env["data"])
    assert env["settings"].read_text(encoding="utf-8") == original


def test_reinstall_keeps_original_manifest(env):
    env["settings"].parent.mkdir(parents=True)
    original = write(env["settings"], USER_SETTINGS)  # includes an empty "Notification": []
    do_install(env)
    do_install(env)
    hi.uninstall(env["settings"], data_dir=env["data"])
    assert env["settings"].read_text(encoding="utf-8") == original


def test_uninstall_without_manifest_still_removes_only_ours(env):
    env["settings"].parent.mkdir(parents=True)
    write(env["settings"], USER_SETTINGS)
    do_install(env)
    (env["data"] / "install-manifest.json").unlink()
    saved, removed = hi.uninstall(env["settings"], data_dir=env["data"])
    data = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert removed == len(hi.ALL_EVENTS) and not hi.installed_events(data)
    assert data["hooks"]["PreToolUse"] == USER_SETTINGS["hooks"]["PreToolUse"]
    assert data["hooks"]["Stop"] == USER_SETTINGS["hooks"]["Stop"]
