import json
import shutil
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from snore_tracker import analyze, model
from snore_tracker.analyze import Frame
from snore_tracker.config import Config

CHI = ZoneInfo("America/Chicago")
START = datetime(2026, 10, 20, 23, 0, tzinfo=CHI)


@pytest.fixture
def cfg(tmp_path):
    return Config(
        data_dir=tmp_path,
        threshold=0.5,
        min_event_minutes=2,
        max_gap_minutes=1,
        db_offset=0.0,
        loud_db=-20.0,
        clip_seconds=10,
        clip_days=14,
        keep_audio=False,
        tz=CHI,
    )


def frames_for(probs_per_minute, db=-40.0):
    """One frame per second, every frame in a minute gets that minute's prob."""
    frames = []
    for m, prob in enumerate(probs_per_minute):
        for s in range(60):
            frames.append(Frame(START + timedelta(minutes=m, seconds=s), prob, db, Path("x"), 0))
    return frames


def minutes_for(probs, db_max=-30.0):
    return [
        {
            "t": (START + timedelta(minutes=i)).strftime(analyze.MINUTE_FORMAT),
            "snore_prob": p,
            "db_avg": -40.0,
            "db_max": db_max,
        }
        for i, p in enumerate(probs)
    ]


def test_night_is_wake_up_date():
    for t in [(10, 20, 20, 30), (10, 21, 3, 0), (10, 21, 8, 29)]:
        assert analyze.night_of(datetime(2026, *t, tzinfo=CHI), CHI) == date(2026, 10, 21)


def test_group_segments_splits_nights():
    paths = [
        "2026-10-20T20-30-00-0500.opus",
        "2026-10-21T02-00-00-0500.opus",
        "2026-10-21T21-00-00-0500.opus",
    ]
    nights = analyze.group_segments(paths, CHI)
    assert [len(v) for v in nights.values()] == [2, 1]
    assert list(nights) == [date(2026, 10, 21), date(2026, 10, 22)]


def test_per_minute_takes_max_prob_and_energy_mean_db():
    frames = frames_for([0.1])
    frames[5].snore_prob = 0.8
    frames[0].db = -10.0  # one loud frame among -40s
    (m,) = analyze.per_minute(frames, CHI)
    assert m["t"] == "2026-10-20T23:00"
    assert m["snore_prob"] == 0.8
    assert m["db_max"] == -10.0
    assert -40 < m["db_avg"] < -10  # energy mean is pulled up by the loud frame


def test_dst_fall_back_segments_stay_distinct_and_local():
    # 2026-11-01: 01:30 happens twice, as -0500 (CDT) then -0600 (CST)
    cdt, cst = "2026-11-01T01-30-00-0500.opus", "2026-11-01T01-30-00-0600.opus"
    assert analyze.segment_start(cst) - analyze.segment_start(cdt) == timedelta(hours=1)
    assert list(analyze.group_segments([cst, cdt], CHI).values()) == [[Path(cdt), Path(cst)]]
    assert analyze.minute_key(analyze.segment_start(cst), CHI) == "2026-11-01T01:30"


def test_dst_spring_forward_offsets_skip_the_missing_hour():
    start = analyze.segment_start("2026-03-08T01-30-00-0600.opus")
    assert analyze.minute_key(start + timedelta(hours=1), CHI) == "2026-03-08T03:30"


def test_events_bridge_small_gaps_and_drop_short_runs(cfg):
    #        0    1    2    3    4    5    6    7    8
    probs = [0.9, 0.9, 0.1, 0.9, 0.1, 0.1, 0.9, 0.1, 0.1]
    events = analyze.find_events(minutes_for(probs), cfg)
    # minutes 0,1,3 bridge (gap of 1); minute 6 alone is too short
    assert len(events) == 1
    assert events[0]["start"] == "2026-10-20T23:00"
    assert events[0]["end"] == "2026-10-20T23:03"
    assert events[0]["minutes"] == 3


def test_summary_counts_and_loud_events(cfg):
    minutes = minutes_for([0.9, 0.9, 0.1, 0.1], db_max=-15.0)
    events = analyze.find_events(minutes, cfg)
    s = analyze.summarize(minutes, events, cfg)
    assert s == {
        "snore_minutes": 2,
        "snore_pct": 50.0,
        "events": 1,
        "loud_events": 1,
        "max_db": -15.0,
    }


def test_summary_with_no_snoring(cfg):
    minutes = minutes_for([0.1, 0.1])
    s = analyze.summarize(minutes, [], cfg)
    assert s["snore_minutes"] == 0 and s["events"] == 0 and s["max_db"] is None


def test_payload_matches_garmin_connect_contract(cfg):
    payload = analyze.build_night(date(2026, 10, 21), frames_for([0.9, 0.9, 0.1]), cfg)
    assert payload["night"] == "2026-10-21"
    assert payload["start"] == "2026-10-20T23:00:00"
    assert payload["source"] == "pi4-yamnet-v1"
    assert set(payload["summary"]) == {
        "snore_minutes",
        "snore_pct",
        "events",
        "loud_events",
        "max_db",
    }
    assert set(payload["minutes"][0]) == {"t", "snore_prob", "db_avg", "db_max"}
    assert len(payload["minutes"]) == 3


def test_prune_clips_keeps_recent(cfg):
    for d in ["2026-10-01", "2026-10-20", "not-a-date"]:
        (cfg.clips / d).mkdir(parents=True)
    analyze.prune_clips(cfg, date(2026, 10, 21))
    assert sorted(p.name for p in cfg.clips.iterdir()) == ["2026-10-20", "not-a-date"]


class LoudnessModel:
    """Stands in for YAMNet: 'snoring' whenever the window is loud."""

    def snore_score(self, window):
        return 0.9 if np.sqrt(np.mean(window**2)) > 0.02 else 0.01


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def make_segment(cfg):
    """65 s quiet (clear of the minute boundary), then 2 minutes of loud tone."""
    cfg.recordings.mkdir(parents=True, exist_ok=True)
    seg = cfg.recordings / "2026-10-21T02-00-00-0500.opus"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=a=0.001:d=65",
            "-f",
            "lavfi",
            "-i",
            "sine=f=200:d=120",
            "-filter_complex",
            "[1]volume=0.5[s];[0][s]concat=n=2:v=0:a=1",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libopus",
            "-b:a",
            "24k",
            str(seg),
        ],
        check=True,
    )
    return seg


@needs_ffmpeg
def test_analyze_night_end_to_end(cfg):
    seg = make_segment(cfg)
    out = analyze.analyze_night(date(2026, 10, 21), [seg], LoudnessModel(), cfg)

    payload = json.loads(out.read_text())
    assert [m["snore_prob"] for m in payload["minutes"]] == [0.01, 0.9, 0.9, 0.9]
    assert payload["summary"]["events"] == 1
    clip = cfg.data_dir / payload["events"][0]["clip"]
    assert clip.exists()
    assert not seg.exists()  # full-night audio deleted after success


@needs_ffmpeg
def test_rerun_replaces_clips_and_reposts(cfg):
    cfg.keep_audio = True
    seg = make_segment(cfg)
    out = analyze.analyze_night(date(2026, 10, 21), [seg], LoudnessModel(), cfg)
    stale = cfg.clips / "2026-10-21" / "stale.opus"
    stale.touch()
    out.with_suffix(".posted").touch()

    analyze.analyze_night(date(2026, 10, 21), [seg], LoudnessModel(), cfg)

    assert seg.exists()  # KEEP_AUDIO
    assert not stale.exists()
    assert not out.with_suffix(".posted").exists()  # new numbers get posted again


def test_main_refuses_while_recording(cfg, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(cfg.data_dir))
    monkeypatch.setattr(analyze, "recording_active", lambda: True)
    with pytest.raises(SystemExit) as exit_:
        analyze.main()
    assert "snore-record is running" in str(exit_.value.code)


@needs_ffmpeg
def test_main_analyzes_a_just_finished_night(cfg, monkeypatch):
    # The recorder stops seconds before analysis, so a fresh segment must not be skipped
    seg = make_segment(cfg)
    monkeypatch.setenv("DATA_DIR", str(cfg.data_dir))
    monkeypatch.setattr(analyze, "recording_active", lambda: False)
    monkeypatch.setattr(model, "Yamnet", lambda path: LoudnessModel())
    with pytest.raises(SystemExit) as exit_:
        analyze.main()
    assert exit_.value.code == 0
    assert (cfg.nights / "2026-10-21.json").exists()
    assert not seg.exists()
