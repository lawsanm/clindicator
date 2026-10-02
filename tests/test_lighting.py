"""Lighting controller behavior with a fake device: dedupe, reconnect after failures, re-apply, restore."""

import time

import pytest

from claude_rgb_status import lighting
from claude_rgb_status.rgb.base import AdapterError, DeviceInfo, Effect, RgbAdapter

BLUE = Effect.solid((0, 64, 255))
GREEN = Effect.solid((0, 255, 48))
WHITE = Effect.solid((255, 255, 255))


class FakeAdapter(RgbAdapter):
    name = "fake"

    def __init__(self, log):
        self.log = log
        self._open = False

    def scan(self):
        return [DeviceInfo("fake-1", "Fake keyboard", "", True)]

    def open(self, device_id):
        if self.log["unplugged"]:
            raise AdapterError("not present")
        self.log["opens"] += 1
        self._open = True
        return self.scan()[0]

    def apply(self, effect):
        if self.log["fail_next"]:
            self.log["fail_next"] -= 1
            self._open = False
            raise AdapterError("write failed (stale handle)")
        self.log["writes"].append(effect)

    def close(self):
        self._open = False

    @property
    def is_open(self):
        return self._open


@pytest.fixture
def ctl(monkeypatch):
    monkeypatch.setattr(lighting, "RETRY_INTERVAL", 0.1)
    log = {"opens": 0, "writes": [], "fail_next": 0, "unplugged": False}
    c = lighting.LightingController("auto", adapter_factory=lambda allow: FakeAdapter(log))
    c.log = log
    c.start()
    yield c
    c.shutdown(None)


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_writes_only_on_change(ctl):
    ctl.set_target(BLUE, "Processing")
    assert wait_for(lambda: ctl.log["writes"] == [BLUE])
    ctl.set_target(BLUE, "Processing")
    time.sleep(0.3)
    assert ctl.log["writes"] == [BLUE]
    ctl.set_target(GREEN, "Finished")
    assert wait_for(lambda: ctl.log["writes"] == [BLUE, GREEN])


def test_reconnects_and_rewrites_after_failed_write(ctl):
    ctl.set_target(BLUE)
    assert wait_for(lambda: ctl.log["writes"] == [BLUE])
    ctl.log["fail_next"] = 1
    ctl.set_target(GREEN)
    assert wait_for(lambda: ctl.log["writes"][-1] == GREEN and ctl.connected)
    assert ctl.failures == 1 and ctl.log["opens"] >= 2


def test_waits_for_unplugged_device_then_recovers(ctl):
    ctl.log["unplugged"] = True
    ctl.reopen()
    ctl.set_target(BLUE)
    time.sleep(0.4)
    assert not ctl.connected and ctl.last_error
    ctl.log["unplugged"] = False
    assert wait_for(lambda: ctl.connected and ctl.log["writes"][-1:] == [BLUE])


def test_reapply_rewrites_same_effect(ctl):
    ctl.set_target(BLUE)
    assert wait_for(lambda: len(ctl.log["writes"]) == 1)
    ctl.reapply()
    assert wait_for(lambda: ctl.log["writes"] == [BLUE, BLUE])


def test_shutdown_applies_restore_profile(monkeypatch):
    log = {"opens": 0, "writes": [], "fail_next": 0, "unplugged": False}
    c = lighting.LightingController("auto", adapter_factory=lambda allow: FakeAdapter(log))
    c.start()
    c.set_target(BLUE)
    assert wait_for(lambda: log["writes"] == [BLUE])
    c.shutdown(WHITE)
    assert log["writes"][-1] == WHITE and not c.is_alive()
