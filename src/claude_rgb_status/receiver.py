"""UDP receiver for hook relay datagrams (127.0.0.1 only, shared-token authenticated)."""

from __future__ import annotations

import hmac
import json
import logging
import socket
import threading
import time
from collections import deque
from typing import Callable

log = logging.getLogger(__name__)

PROTOCOL = b"CRS1"
MAX_EVENTS_PER_SECOND = 500
PORT_ATTEMPTS = 20


class Receiver(threading.Thread):
    def __init__(self, port: int, token: str, on_message: Callable[[dict], None]):
        super().__init__(name="receiver", daemon=True)
        self.token = token.encode("ascii")
        self.on_message = on_message
        self.received = 0
        self.rejected = 0
        self.last_event_at: float | None = None
        self._recent: deque[float] = deque()
        self._stop = threading.Event()
        self.sock, self.port = self._bind(port)

    @staticmethod
    def _bind(preferred: int) -> tuple[socket.socket, int]:
        last_error: OSError | None = None
        for port in [preferred + i for i in range(PORT_ATTEMPTS)] + [0]:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                # Nobody else may bind the same port and read our events.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as exc:
                last_error = exc
                sock.close()
                continue
            sock.settimeout(0.5)
            return sock, sock.getsockname()[1]
        raise OSError(f"cannot bind a local UDP port: {last_error}")

    def parse(self, data: bytes) -> dict | None:
        header, sep, body = data.partition(b"\n")
        parts = header.split(b" ", 1)
        if not sep or len(parts) != 2 or parts[0] != PROTOCOL or not hmac.compare_digest(parts[1], self.token):
            return None
        try:
            msg = json.loads(body)
        except ValueError:
            return None
        if not isinstance(msg, dict):
            return None
        if isinstance(msg.get("ctl"), str):  # control message from the CLI (quit, reapply, ...)
            return msg
        if not isinstance(msg.get("ev"), str) or not isinstance(msg.get("sid"), str):
            return None
        return msg

    def _rate_ok(self, now: float) -> bool:
        self._recent.append(now)
        while self._recent and now - self._recent[0] > 1.0:
            self._recent.popleft()
        return len(self._recent) <= MAX_EVENTS_PER_SECOND

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                # Windows reports ICMP "port unreachable" from earlier sends as errors on recv.
                if self._stop.is_set():
                    break
                continue
            now = time.time()
            msg = self.parse(data) if addr[0] == "127.0.0.1" else None
            if msg is None or not self._rate_ok(now):
                self.rejected += 1
                continue
            self.received += 1
            self.last_event_at = now
            try:
                self.on_message(msg)
            except Exception:  # never let one bad event stop the receiver
                log.exception("event handler failed")

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
