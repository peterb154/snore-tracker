import json
import subprocess

import numpy as np
import pytest
from fastapi.testclient import TestClient

from snore_tracker import audio, live, web
from snore_tracker.config import Config


class FakeReader:
    def level(self):
        return {"live": True, "db": -30.0, "peak_db": -20.0, "score": 0.1}


@pytest.fixture
def calls(monkeypatch):
    """Records systemctl calls; every call succeeds with empty output."""
    calls = []

    def fake(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(web, "systemctl", fake)
    return calls


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path)


@pytest.fixture
def client(cfg, calls):
    return TestClient(web.create_app(cfg, FakeReader()))


def write_night(cfg, night="2026-10-21", clip=True):
    cfg.nights.mkdir(parents=True, exist_ok=True)
    event = {"start": f"{night}T02:10", "minutes": 3, "peak_t": f"{night}T02:12", "peak_db": -22.5}
    if clip:
        event["clip"] = f"clips/{night}/02-12-31.opus"
        (cfg.clips / night).mkdir(parents=True)
        (cfg.clips / night / "02-12-31.opus").write_bytes(b"OggS")
    payload = {
        "night": night,
        "summary": {"snore_minutes": 3, "snore_pct": 1.0, "events": 1},
        "minutes": [{"t": f"{night}T02:12", "snore_prob": 0.83}],
        "events": [event],
    }
    (cfg.nights / f"{night}.json").write_text(json.dumps(payload))


def test_slice_stats_of_a_sine():
    t = np.arange(audio.SAMPLE_RATE) / audio.SAMPLE_RATE
    sine = (0.5 * np.sin(2 * np.pi * 100 * t)).astype(np.float32)  # 5 whole cycles per slice
    stats = live.slice_stats(np.concatenate([sine, sine[:100]]))
    assert len(stats) == 20  # 1 s of 50 ms slices; the partial slice is dropped
    for lo, hi, rms in stats:
        assert lo == pytest.approx(-0.5, abs=1e-3)
        assert hi == pytest.approx(0.5, abs=1e-3)
        assert rms == pytest.approx(0.5 / np.sqrt(2), abs=1e-3)


def test_status_from_a_data_dir(cfg, client):
    cfg.recordings.mkdir()
    (cfg.recordings / "2026-10-21T20-30-00-0500.opus").write_bytes(b"x" * 600_000)
    (cfg.recordings / "2026-10-21T21-30-00-0500.opus").write_bytes(b"x" * 400_000)
    write_night(cfg, "2026-10-20")
    write_night(cfg, "2026-10-21", clip=False)
    (cfg.nights / "2026-10-21.posted").touch()

    s = client.get("/api/status").json()

    assert s["recording"]["count"] == 2
    assert s["recording"]["mb"] == 1.0
    assert s["recording"]["current"] == "2026-10-21T21-30-00-0500.opus"
    assert s["last_night"]["night"] == "2026-10-21"
    assert s["last_night"]["posted"] is True
    assert s["level"]["hint"] == "ok"
    assert s["clip_playback"] is False


def test_level_hint():
    assert web.level_hint(-1) == "too hot"
    assert web.level_hint(-60) == "too quiet"
    assert web.level_hint(None) is None


def test_recording_state_parses_systemctl(monkeypatch):
    out = "ActiveState=active\nActiveEnterTimestamp=@1792700000\n"
    monkeypatch.setattr(
        web, "systemctl", lambda *a: subprocess.CompletedProcess(a, 0, stdout=out, stderr="")
    )
    assert web.recording_state() == {"active": True, "started": 1792700000}


def test_clips_404_without_flag(cfg, client):
    write_night(cfg)
    assert client.get("/api/clips").status_code == 404
    assert client.get("/clips/2026-10-21/02-12-31.opus").status_code == 404


def test_clips_200_with_flag(cfg, client):
    write_night(cfg)
    (cfg.data_dir / ".clip-playback").touch()

    (clip,) = client.get("/api/clips").json()
    assert clip["url"] == "/clips/2026-10-21/02-12-31.opus"
    assert clip["peak_score"] == 0.83
    resp = client.get(clip["url"])
    assert resp.status_code == 200
    assert resp.content == b"OggS"


def test_clip_path_rejects_escape(cfg):
    write_night(cfg)
    (cfg.data_dir / ".env").write_text("SNORE_TOKEN=secret")
    assert web.clip_path(cfg, "..", ".env") is None
    assert web.clip_path(cfg, "2026-10-21", "../../.env") is None
    assert web.clip_path(cfg, "2026-10-21", "02-12-31.opus") is not None


@pytest.mark.parametrize("action", ["start", "stop"])
def test_record_start_stop_targets_only_the_recorder(client, calls, action):
    assert client.post(f"/api/record/{action}").status_code == 200
    assert calls[0] == (action, "snore-record.service")


def test_record_rejects_other_actions(client, calls):
    assert client.post("/api/record/restart").status_code == 404
    assert calls == []
