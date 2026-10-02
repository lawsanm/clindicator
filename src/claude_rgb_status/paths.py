"""Filesystem locations used by the app, the hook relay and the installer."""

from __future__ import annotations

import os
from pathlib import Path

from . import APP_NAME

# Tests and side-by-side experiments can point everything at one folder.
HOME_OVERRIDE_ENV = "CLAUDE_RGB_STATUS_HOME"


def _override() -> Path | None:
    value = os.environ.get(HOME_OVERRIDE_ENV)
    return Path(value) if value else None


def config_dir() -> Path:
    """Roaming settings (colors, device choice, behavior)."""
    base = _override() or Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    return base


def data_dir() -> Path:
    """Machine-local runtime data (endpoint, status snapshot, logs, backups)."""
    base = _override() or Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    return base


def config_path() -> Path:
    return config_dir() / "config.json"


def endpoint_path() -> Path:
    """Port + token the hook relay uses to reach the app. Must match hook_relay.default_endpoint_path()."""
    return data_dir() / "endpoint.json"


def status_path() -> Path:
    return data_dir() / "status.json"


def log_dir() -> Path:
    return data_dir() / "logs"


def backup_dir() -> Path:
    return data_dir() / "backups"


def claude_config_dir() -> Path:
    """Claude Code's user config directory (honors CLAUDE_CONFIG_DIR like Claude Code does)."""
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(value) if value else Path.home() / ".claude"


def claude_settings_path() -> Path:
    return claude_config_dir() / "settings.json"


def ensure_dirs() -> None:
    for path in (config_dir(), data_dir(), log_dir(), backup_dir()):
        path.mkdir(parents=True, exist_ok=True)
