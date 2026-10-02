"""The UDP port and shared token the hook relay uses to reach the app.

Stored in %LOCALAPPDATA%\\ClaudeRGBStatus\\endpoint.json, inside the user's profile,
so other local accounts can't read the token and inject fake events.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .config import write_json_atomic


@dataclass(frozen=True)
class Endpoint:
    port: int
    token: str


def load(path: Path | None = None) -> Endpoint | None:
    path = path or paths.endpoint_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        token = str(raw["token"])
        port = int(raw["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if len(token) < 16 or not 1 <= port <= 65535:
        return None
    return Endpoint(port, token)


def ensure(port: int, path: Path | None = None) -> Endpoint:
    """Keep the existing token, update the port if needed."""
    path = path or paths.endpoint_path()
    current = load(path)
    token = current.token if current else secrets.token_hex(24)
    endpoint = Endpoint(port, token)
    if current != endpoint:
        write_json_atomic(path, {"port": endpoint.port, "token": endpoint.token})
    return endpoint
