import json
import subprocess
from pathlib import Path

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


def test_next_timers_parses_list_timers_json(monkeypatch):
    out = json.dumps(
        [
            {"next": 1792688400000000, "unit": "snore-record.timer"},
            {"next": 0, "unit": "snore-analyze.timer"},
        ]
    )
    monkeypatch.setattr(
        web, "systemctl", lambda *a: subprocess.CompletedProcess(a, 0, stdout=out, stderr="")
    )
    assert web.next_timers() == {
        "snore-record.timer": 1792688400,
        "snore-analyze.timer": None,
    }


SNORE = {"X-Snore": "1"}


@pytest.mark.parametrize("action", ["start", "stop"])
def test_record_start_stop_targets_only_the_recorder(client, calls, monkeypatch, action):
    def fake(*args):
        calls.append(args)
        out = "inactive\n" if args[0] == "is-active" else ""  # snore-analyze not running
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(web, "systemctl", fake)
    assert client.post(f"/api/record/{action}", headers=SNORE).status_code == 200
    assert (action, "snore-record.service") in calls


def test_record_needs_the_custom_header(client, calls):
    assert client.post("/api/record/start").status_code == 403
    assert calls == []


def test_record_start_refused_while_analyzing(client, calls, monkeypatch):
    def fake(*args):
        calls.append(args)
        # A running oneshot is "activating" (exit 3), never "active"
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 3, "activating\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(web, "systemctl", fake)
    assert client.post("/api/record/start", headers=SNORE).status_code == 409
    assert ("start", "snore-record.service") not in calls


def test_record_rejects_other_actions(client, calls):
    assert client.post("/api/record/restart", headers=SNORE).status_code == 404
    assert calls == []


def test_live_is_not_buffered_by_the_proxy(cfg):
    class OneShot(FakeReader):
        def events(self):
            yield "data: {}\n\n"

    resp = TestClient(web.create_app(cfg, OneShot())).get("/api/live")
    assert resp.headers["x-accel-buffering"] == "no"


TOKEN = {"Authorization": "Bearer s3cret"}


@pytest.fixture
def deploy(monkeypatch, calls):
    """Records git/uv runs and Popen restarts; snore-analyze idle unless a test says otherwise."""
    monkeypatch.setenv("DEPLOY_TOKEN", "s3cret")
    ran = {"run": [], "popen": [], "fail": None, "missing": None, "analyze": "inactive"}

    def fake_run(cmd, **kw):
        ran["run"].append(cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(cmd, 0, ran["analyze"] + "\n", "")
        if cmd[0] == ran["missing"]:
            raise FileNotFoundError(2, "No such file or directory", cmd[0])
        code = 1 if ran["fail"] and ran["fail"] in cmd else 0
        return subprocess.CompletedProcess(
            cmd, code, ran.get("stdout", "out"), "err" if code else ""
        )

    monkeypatch.setattr(web.subprocess, "run", fake_run)
    monkeypatch.setattr(web.subprocess, "Popen", lambda cmd, **kw: ran["popen"].append(cmd))
    monkeypatch.setattr(web, "systemctl", lambda *a: fake_run(["systemctl", *a]))
    return ran


def test_deploy_503_without_a_token(client, deploy, monkeypatch):
    monkeypatch.delenv("DEPLOY_TOKEN")
    assert client.post("/api/deploy", headers=TOKEN).status_code == 503
    assert deploy["run"] == []


def test_deploy_401_with_a_wrong_token(client, deploy):
    assert client.post("/api/deploy", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/api/deploy").status_code == 401
    assert deploy["run"] == []


def test_deploy_409_while_analyzing(client, deploy):
    deploy["analyze"] = "activating"  # a oneshot never reads "active"
    r = client.post("/api/deploy", headers=TOKEN)
    assert r.status_code == 409
    assert r.json()["status"] == "error"
    assert [c[0] for c in deploy["run"]] == ["systemctl"]  # no git, no uv
    assert deploy["popen"] == []


def test_deploy_stops_at_a_failed_pull(client, deploy):
    deploy["fail"] = "pull"
    r = client.post("/api/deploy", headers=TOKEN).json()
    assert r["status"] == "error"
    assert [s["step"] for s in r["results"]] == ["git pull"]
    assert "out" in r["results"][0]["output"] and "err" in r["results"][0]["output"]
    assert not any(c[0] == web.UV_BIN for c in deploy["run"])
    assert deploy["popen"] == []


def test_deploy_runs_the_steps_in_order(client, deploy):
    r = client.post("/api/deploy", headers=TOKEN).json()
    assert r["status"] == "ok"
    steps = [c for c in deploy["run"] if c[0] != "systemctl"]
    assert steps[0][-2:] == ["pull", "--ff-only"]
    assert steps[1][0] == web.UV_BIN and "--frozen" in steps[1]
    assert deploy["popen"] == [["systemctl", "--no-block", "restart", "snore-web.service"]]


def test_deploy_reports_a_missing_uv(client, deploy):
    deploy["missing"] = web.UV_BIN  # the fitness bug: uv not on systemd's PATH
    r = client.post("/api/deploy", headers=TOKEN).json()
    assert r["status"] == "error"
    assert r["results"][-1]["step"] == "uv sync"
    assert "FileNotFoundError" in r["results"][-1]["output"]
    assert deploy["popen"] == []


def test_deploy_409_while_another_deploy_runs(client, deploy):
    with web.DEPLOY_LOCK:
        r = client.post("/api/deploy", headers=TOKEN)
    assert r.status_code == 409
    assert deploy["run"] == []
    assert client.post("/api/deploy", headers=TOKEN).json()["status"] == "ok"  # released


def test_deploy_401_on_a_non_ascii_token(client, deploy):
    r = client.post("/api/deploy", headers={"Authorization": "Bearer \xe9".encode("latin-1")})
    assert r.status_code == 401


def test_health_reports_the_loaded_commit_not_the_pulled_one(cfg, deploy):
    deploy["stdout"] = "abc1234\n"
    client = TestClient(web.create_app(cfg, FakeReader()))
    deploy["stdout"] = "def5678\n"  # a later pull moved HEAD; this process didn't reload
    assert client.get("/api/health").json() == {"status": "ok", "commit": "abc1234"}


SGET_MIC = (Path(__file__).parent / "amixer_sget_mic.txt").read_text()  # real C-Media output


@pytest.fixture
def mixer(monkeypatch):
    """Fake amixer: `sget Mic` reports the current gain; `sset` records and updates it."""
    state = {"gain": 8, "sets": [], "card_missing": False}

    def fake(cfg, *args):
        if state["card_missing"]:
            return subprocess.CompletedProcess(args, 1, "", "Invalid card number.")
        if args[0] == "sset":
            state["sets"].append((cfg.card, *args[1:]))
            if args[1] == "Mic":
                state["gain"] = int(args[2])
            return subprocess.CompletedProcess(args, 0, "", "")
        out = SGET_MIC.replace("Capture 8 [", f"Capture {state['gain']} [")
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(web, "amixer", fake)
    return state


def test_read_gain_parses_amixer(cfg, mixer):
    assert web.read_gain(cfg) == {"gain": 8, "gain_max": 16}
    mixer["card_missing"] = True
    assert web.read_gain(cfg) is None


def test_settings_file_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SNORE_THRESHOLD", "0.5")
    assert Config(data_dir=tmp_path).threshold == 0.5  # no file: .env
    (tmp_path / "settings.json").write_text('{"gain": 6, "threshold": 0.3}')
    cfg = Config(data_dir=tmp_path)
    assert (cfg.gain, cfg.threshold) == (6, 0.3)


def test_post_settings_sets_gain_and_saves(tmp_path, mixer, calls):
    cfg = Config(data_dir=tmp_path / "snore-data")  # not created yet, as before the first night
    client = TestClient(web.create_app(cfg, FakeReader()))
    r = client.post("/api/settings", json={"gain": 5}, headers=SNORE)
    assert r.json() == {"threshold": 0.5, "gain": 5, "gain_max": 16}
    assert ("Device", "Auto Gain Control", "off") in mixer["sets"]
    assert ("Device", "Mic", "5") in mixer["sets"]
    assert json.loads(cfg.settings_file.read_text()) == {"gain": 5, "threshold": 0.5}


def test_post_threshold_moves_the_live_line(cfg, client, mixer):
    assert client.post("/api/settings", json={"threshold": 0.35}, headers=SNORE).status_code == 200
    assert client.get("/api/status").json()["threshold"] == 0.35
    assert mixer["sets"] == []  # threshold only: mixer untouched
    assert Config(data_dir=cfg.data_dir).threshold == 0.35  # what tomorrow's analysis reads


@pytest.mark.parametrize(
    "body", [{"gain": 17}, {"gain": -1}, {"threshold": 0.0}, {"threshold": 1.0}, {"gain": "x"}]
)
def test_post_settings_rejects_out_of_range(cfg, client, mixer, body):
    assert client.post("/api/settings", json=body, headers=SNORE).status_code == 422
    assert mixer["sets"] == []
    assert not cfg.settings_file.exists()


def test_post_settings_needs_the_custom_header(cfg, client, mixer):
    assert client.post("/api/settings", json={"gain": 5}).status_code == 403
    assert mixer["sets"] == []


def test_post_gain_without_the_mic_is_503(cfg, client, mixer):
    mixer["card_missing"] = True
    assert client.post("/api/settings", json={"gain": 5}, headers=SNORE).status_code == 503
    assert client.get("/api/settings").json()["gain"] is None


def test_startup_reapplies_the_saved_gain(tmp_path, mixer):
    (tmp_path / "settings.json").write_text('{"gain": 6, "threshold": 0.4}')
    web.apply_saved_gain(Config(data_dir=tmp_path))
    assert ("Device", "Mic", "6") in mixer["sets"]


def test_startup_without_a_saved_gain_leaves_the_mixer_alone(cfg, mixer):
    web.apply_saved_gain(cfg)
    assert mixer["sets"] == []


def test_startup_survives_a_missing_mic(tmp_path, mixer):
    (tmp_path / "settings.json").write_text('{"gain": 6}')
    mixer["card_missing"] = True
    web.apply_saved_gain(Config(data_dir=tmp_path))  # logs, doesn't raise
