import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "deploy" / "snore-leds.sh"


def decide(net, synced, reachable, offline, hhmm, uptime):
    args = [net, synced, reachable, str(offline), hhmm, str(uptime)]
    out = subprocess.run(
        ["sh", SCRIPT, "decide", *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


@pytest.mark.parametrize(
    "net, synced, reachable, offline, expected",
    [
        ("no", "no", "no", 0, "joining"),
        ("no", "no", "no", 179, "joining"),
        ("no", "no", "no", 180, "offline"),
        ("yes", "no", "no", 0, "joining"),
        ("yes", "yes", "yes", 0, "online"),
        ("yes", "yes", "no", 0, "unreachable"),
    ],
)
def test_daytime_states(net, synced, reachable, offline, expected):
    assert decide(net, synced, reachable, offline, "1200", 5000) == expected


@pytest.mark.parametrize(
    "hhmm, expected",
    [("0829", "dark"), ("0830", "online"), ("2029", "online"), ("2030", "dark"), ("0000", "dark")],
)
def test_dark_during_recording_window(hhmm, expected):
    assert decide("yes", "yes", "yes", 0, hhmm, 5000) == expected


def test_shows_status_at_night_for_10_min_after_boot():
    assert decide("no", "no", "no", 300, "0300", 599) == "offline"
    assert decide("no", "no", "no", 300, "0300", 600) == "dark"
