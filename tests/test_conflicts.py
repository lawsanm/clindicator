from claude_rgb_status import conflicts

PROCS = [
    (4, 0, "System"),
    (6000, 1, "LenovoVantage-(LenovoGamingSystemAddin).exe"),
    (6001, 1, "LenovoVantage-(GenericMessagingAddin).exe"),
    (7000, 1, "OpenRGB.exe"),
    (8000, 1, "explorer.exe"),
]


def test_detects_lighting_software_without_touching_it():
    found = conflicts.detect(PROCS)
    assert len(found) == 2
    assert any("Lenovo Vantage" in f for f in found) and any("OpenRGB" in f for f in found)
    assert all("does not disable" in f for f in found)


def test_nothing_detected_on_clean_system():
    assert conflicts.detect([(8000, 1, "explorer.exe")]) == []


def test_restart_shows_up_as_new_pid():
    before = conflicts.lighting_pids(PROCS)
    restarted = [p for p in PROCS if p[0] != 6000] + [(6123, 1, "LenovoVantage-(LenovoGamingSystemAddin).exe")]
    after = conflicts.lighting_pids(restarted)
    assert before == {6000, 7000} and after - before == {6123}
