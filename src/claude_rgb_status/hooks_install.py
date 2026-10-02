"""Add or remove this app's hooks in Claude Code's user settings.json.

- The original file is backed up before every change.
- Only handlers that run this package's hook_relay.py are added or removed;
  other hooks and settings are left exactly as they were.
- Installing twice replaces the previous entries instead of duplicating them.
- Writes are atomic and re-validated; invalid JSON is never overwritten.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths

RELAY_MARK = "claude_rgb_status\\hook_relay.py"
# Async: Claude never waits for them. SessionEnd stays synchronous so the relay
# finishes before Claude Code exits (it takes ~0.1 s; the budget is 1.5 s).
ASYNC_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PermissionDenied",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "Notification",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "Elicitation",
    "ElicitationResult",
)
SYNC_EVENTS = ("SessionEnd",)
ALL_EVENTS = ASYNC_EVENTS + SYNC_EVENTS


class InstallError(Exception):
    pass


def relay_path() -> Path:
    return Path(__file__).with_name("hook_relay.py").resolve()


def hook_python() -> Path:
    """The base interpreter (not the venv redirector, which would add a second process)."""
    exe = Path(getattr(sys, "_base_executable", None) or sys.executable)
    if exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")
    return exe.resolve()


def make_handler(python: Path, relay: Path, *, sync: bool = False) -> dict:
    handler: dict = {"type": "command", "command": str(python), "args": ["-I", "-S", str(relay)]}
    if not sync:
        handler["async"] = True
    return handler


def is_ours(handler: object) -> bool:
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    parts = [handler.get("command")] + list(handler.get("args") or [])
    joined = " ".join(p for p in parts if isinstance(p, str)).replace("/", "\\").lower()
    return RELAY_MARK in joined


def strip_ours(settings: dict, *, keep_events: frozenset[str] = frozenset(),
               keep_hooks_key: bool = False) -> tuple[dict, int]:
    """Copy of settings without this app's handlers.

    Event lists and the "hooks" object that end up empty only because our handlers
    were removed are dropped, unless they existed before install (keep_*).
    """
    result = copy.deepcopy(settings)
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result, 0
    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                kept_groups.append(group)
                continue
            kept = [h for h in handlers if not is_ours(h)]
            removed += len(handlers) - len(kept)
            if kept or not handlers:
                kept_groups.append({**group, "hooks": kept} if len(kept) != len(handlers) else group)
        if kept_groups or not groups or event in keep_events:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks and not keep_hooks_key:
        del result["hooks"]
    return result, removed


def preexisting(settings: dict) -> dict:
    """Which of our event keys (and the "hooks" object) exist independently of our handlers."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return {"events": [], "hooks_key": False}
    events = []
    for event in ALL_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        only_ours = bool(groups) and all(
            isinstance(g, dict) and g.get("hooks") and all(is_ours(h) for h in g["hooks"]) for g in groups
        )
        if not only_ours:
            events.append(event)
    hooks_key = bool(events) or not hooks or any(e not in ALL_EVENTS for e in hooks)
    return {"events": events, "hooks_key": hooks_key}


def add_ours(settings: dict, python: Path, relay: Path, record: dict | None = None) -> dict:
    record = record or preexisting(settings)
    result, _ = strip_ours(settings, keep_events=frozenset(record["events"]), keep_hooks_key=record["hooks_key"])
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError('"hooks" in settings.json is not an object; refusing to modify it')
    for event in ALL_EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise InstallError(f'"hooks.{event}" is not a list; refusing to modify it')
        groups.append({"hooks": [make_handler(python, relay, sync=event in SYNC_EVENTS)]})
    return result


def installed_events(settings: dict) -> list[str]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    found = []
    for event, groups in hooks.items():
        if isinstance(groups, list) and any(
            isinstance(g, dict) and any(is_ours(h) for h in g.get("hooks") or []) for g in groups
        ):
            found.append(event)
    return found


@dataclass
class SettingsFile:
    path: Path
    data: dict
    text: str | None  # None when the file doesn't exist yet
    mtime_ns: int | None


def read_settings(path: Path) -> SettingsFile:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return SettingsFile(path, {}, None, None)
    text = raw.decode("utf-8-sig")
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise InstallError(f"{path} is not valid JSON ({exc}); fix it first, nothing was changed") from exc
    if not isinstance(data, dict):
        raise InstallError(f"{path} does not contain a JSON object; nothing was changed")
    return SettingsFile(path, data, text, path.stat().st_mtime_ns)


def backup(current: SettingsFile, backup_dir: Path | None = None) -> Path | None:
    if current.text is None:
        return None
    backup_dir = backup_dir or paths.backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"claude-settings-{stamp}.json"
    n = 1
    while target.exists():
        n += 1
        target = backup_dir / f"claude-settings-{stamp}-{n}.json"
    shutil.copy2(current.path, target)
    return target


def write_settings(current: SettingsFile, data: dict) -> None:
    path = current.path
    # Claude Code writes 2-space JSON with a trailing newline; match it so only our block changes.
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if current.mtime_ns is not None and path.stat().st_mtime_ns != current.mtime_ns:
        raise InstallError(f"{path} changed while we were editing it; run the command again")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="settings.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if json.loads(path.read_text(encoding="utf-8")) != data:
        raise InstallError(f"verification of {path} failed")


def selftest_relay(python: Path, relay: Path) -> None:
    """Run the relay the way Claude Code will; it must exit 0 without output."""
    if not python.exists():
        raise InstallError(f"Python interpreter not found: {python}")
    if not relay.exists():
        raise InstallError(f"hook relay not found: {relay}")
    sample = json.dumps({"hook_event_name": "Notification", "session_id": "selftest", "notification_type": "selftest"})
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [str(python), "-I", "-S", str(relay), "--endpoint", str(Path(tmp) / "endpoint.json")],
            input=sample.encode(), capture_output=True, timeout=30,
        )
    if proc.returncode != 0 or proc.stdout or proc.stderr:
        raise InstallError(f"relay self-test failed (exit {proc.returncode}): {proc.stderr.decode(errors='replace')}")


def _manifest_path(data_dir: Path) -> Path:
    return data_dir / "install-manifest.json"


def _load_manifest(data_dir: Path) -> dict:
    try:
        data = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_manifest(data_dir: Path, manifest: dict) -> None:
    from .config import write_json_atomic

    write_json_atomic(_manifest_path(data_dir), manifest)


def _key(settings_path: Path) -> str:
    return str(settings_path.resolve()).lower()


def install(settings_path: Path | None = None, *, python: Path | None = None, relay: Path | None = None,
            data_dir: Path | None = None) -> tuple[Path | None, int]:
    """Returns (backup file or None, number of hook events registered)."""
    settings_path = settings_path or paths.claude_settings_path()
    data_dir = data_dir or paths.data_dir()
    python, relay = python or hook_python(), relay or relay_path()
    selftest_relay(python, relay)
    current = read_settings(settings_path)
    manifest = _load_manifest(data_dir)
    record = manifest.get(_key(settings_path))
    if not isinstance(record, dict) or not installed_events(current.data):
        # First install (or our entries were removed by hand): remember what was there before us.
        record = preexisting(current.data)
    updated = add_ours(current.data, python, relay, record)
    saved = None
    if updated != current.data:
        saved = backup(current, data_dir / "backups")
        write_settings(current, updated)
    manifest[_key(settings_path)] = {**record, "installed": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _save_manifest(data_dir, manifest)
    return saved, len(installed_events(updated))


def uninstall(settings_path: Path | None = None, *, data_dir: Path | None = None) -> tuple[Path | None, int]:
    """Returns (backup file or None, number of handlers removed)."""
    settings_path = settings_path or paths.claude_settings_path()
    data_dir = data_dir or paths.data_dir()
    current = read_settings(settings_path)
    manifest = _load_manifest(data_dir)
    record = manifest.get(_key(settings_path)) or {}
    updated, removed = strip_ours(
        current.data,
        keep_events=frozenset(record.get("events", [])),
        keep_hooks_key=bool(record.get("hooks_key", False)),
    )
    saved = None
    if removed:
        saved = backup(current, data_dir / "backups")
        write_settings(current, updated)
    if manifest.pop(_key(settings_path), None) is not None:
        _save_manifest(data_dir, manifest)
    return saved, removed
