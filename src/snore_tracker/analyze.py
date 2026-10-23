"""Morning batch: hourly recordings -> per-minute scores -> events -> nights/<date>.json."""

import json
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from snore_tracker import audio
from snore_tracker.config import Config

SOURCE = "pi4-yamnet-v1"
# ffmpeg segment names. %z keeps the two 01:30s on the DST fall-back night distinct.
SEGMENT_FORMAT = "%Y-%m-%dT%H-%M-%S%z"
MINUTE_FORMAT = "%Y-%m-%dT%H:%M"


@dataclass
class Frame:
    t: datetime  # timezone-aware
    snore_prob: float
    db: float
    src: Path
    offset: float  # seconds into src


def segment_start(path):
    return datetime.strptime(Path(path).stem, SEGMENT_FORMAT)


def local(t, tz):
    """Aware time -> naive local wall-clock time, the form garmin_connect stores."""
    return t.astimezone(tz).replace(tzinfo=None)


def minute_key(t, tz):
    return local(t, tz).strftime(MINUTE_FORMAT)


def night_of(t, tz):
    """The wake-up date. Shifting by 12 h puts 20:30 -> 08:30 on one date."""
    return (local(t, tz) + timedelta(hours=12)).date()


def group_segments(paths, tz):
    nights = {}
    for p in sorted(paths, key=segment_start):
        nights.setdefault(night_of(segment_start(p), tz), []).append(Path(p))
    return nights


def score_segment(path, model, db_offset=0.0):
    start = segment_start(path)
    frames = []
    for i, w in enumerate(audio.windows(audio.decode(path))):
        offset = i * audio.WINDOW_SECONDS
        frames.append(
            Frame(
                # Aware arithmetic, so offsets stay right across a DST change
                t=start + timedelta(seconds=offset),
                snore_prob=model.snore_score(w),
                db=audio.dbfs(w) + db_offset,
                src=Path(path),
                offset=offset,
            )
        )
    return frames


def per_minute(frames, tz):
    buckets = {}
    for f in frames:
        buckets.setdefault(minute_key(f.t, tz), []).append(f)
    minutes = []
    for t, fs in sorted(buckets.items()):
        dbs = np.array([f.db for f in fs])
        minutes.append(
            {
                "t": t,
                # Max, not mean: a snore is ~1-2 s of each ~4 s breath, so a mean washes it out.
                "snore_prob": round(max(f.snore_prob for f in fs), 3),
                "db_avg": round(float(10 * np.log10(np.mean(10 ** (dbs / 10)))), 1),
                "db_max": round(float(dbs.max()), 1),
            }
        )
    return minutes


def find_events(minutes, cfg):
    snoring = [m for m in minutes if m["snore_prob"] >= cfg.threshold]
    runs = []
    for m in snoring:
        t = datetime.strptime(m["t"], MINUTE_FORMAT)
        if runs:
            last = datetime.strptime(runs[-1][-1]["t"], MINUTE_FORMAT)
            if (t - last) <= timedelta(minutes=cfg.max_gap_minutes + 1):
                runs[-1].append(m)
                continue
        runs.append([m])
    events = []
    for run in runs:
        if len(run) < cfg.min_event_minutes:
            continue
        peak = max(run, key=lambda m: m["snore_prob"])
        events.append(
            {
                "start": run[0]["t"],
                "end": run[-1]["t"],
                "minutes": len(run),
                "peak_t": peak["t"],
                "peak_db": max(m["db_max"] for m in run),
            }
        )
    return events


def summarize(minutes, events, cfg):
    snoring = [m for m in minutes if m["snore_prob"] >= cfg.threshold]
    return {
        "snore_minutes": len(snoring),
        "snore_pct": round(100 * len(snoring) / len(minutes), 1) if minutes else 0.0,
        "events": len(events),
        "loud_events": sum(e["peak_db"] >= cfg.loud_db for e in events),
        "max_db": max((m["db_max"] for m in snoring), default=None),
        # garmin_connect re-counts minutes over Garmin's sleep window with this cutoff
        "threshold": cfg.threshold,
    }


def build_night(night, frames, cfg):
    """The garmin_connect POST /api/snore payload (plus events, which it ignores)."""
    minutes = per_minute(frames, cfg.tz)
    events = find_events(minutes, cfg)
    end = frames[-1].t + timedelta(seconds=audio.WINDOW_SECONDS)
    return {
        "night": night.isoformat(),
        "start": local(frames[0].t, cfg.tz).isoformat(timespec="seconds"),
        "end": local(end, cfg.tz).isoformat(timespec="seconds"),
        "source": SOURCE,
        "summary": summarize(minutes, events, cfg),
        "minutes": minutes,
        "events": events,
    }


def save_clips(payload, frames, cfg):
    """One clip per event, centred on the loudest-scoring frame of its peak minute."""
    night_dir = cfg.clips / payload["night"]
    shutil.rmtree(night_dir, ignore_errors=True)  # a re-run replaces the old clips
    for event in payload["events"]:
        in_peak = [f for f in frames if minute_key(f.t, cfg.tz) == event["peak_t"]]
        best = max(in_peak, key=lambda f: f.snore_prob)
        dest = night_dir / f"{local(best.t, cfg.tz):%H-%M-%S}.opus"
        audio.cut_clip(best.src, best.offset - cfg.clip_seconds / 2, cfg.clip_seconds, dest)
        event["clip"] = str(dest.relative_to(cfg.data_dir))


def prune_clips(cfg, today):
    if not cfg.clips.exists():
        return
    cutoff = today - timedelta(days=cfg.clip_days)
    for d in cfg.clips.iterdir():
        try:
            if date.fromisoformat(d.name) < cutoff:
                shutil.rmtree(d)
        except ValueError:
            continue


def analyze_night(night, segments, model, cfg):
    frames = []
    for seg in segments:
        frames += score_segment(seg, model, cfg.db_offset)
    if not frames:
        raise ValueError(f"{night}: no audio in {[s.name for s in segments]}")
    payload = build_night(night, frames, cfg)
    save_clips(payload, frames, cfg)
    cfg.nights.mkdir(parents=True, exist_ok=True)
    out = cfg.nights / f"{night}.json"
    out.write_text(json.dumps(payload, indent=1))
    # New numbers (e.g. a KEEP_AUDIO re-run after tuning) must be posted again
    out.with_suffix(".posted").unlink(missing_ok=True)
    if not cfg.keep_audio:
        for seg in segments:
            seg.unlink()
    return out


def recording_active():
    """snore-analyze stops the recorder (Conflicts=) before it runs; this guards manual runs."""
    return subprocess.run(["systemctl", "is-active", "--quiet", "snore-record"]).returncode == 0


def main():
    from snore_tracker.model import Yamnet

    cfg = Config()
    if recording_active():
        sys.exit("snore-record is running; stop it before analyzing")
    segments = list(cfg.recordings.glob("*.opus")) if cfg.recordings.exists() else []
    model = None
    failed = False
    for night, segs in group_segments(segments, cfg.tz).items():
        model = model or Yamnet(cfg.model_path)
        started = time.monotonic()
        try:
            out = analyze_night(night, segs, model, cfg)
        except Exception:  # keep going so one bad night doesn't block the rest
            print(f"{night}: FAILED", file=sys.stderr)
            traceback.print_exc()
            failed = True
            continue
        summary = json.loads(out.read_text())["summary"]
        print(f"{night}: {summary} ({time.monotonic() - started:.0f}s) -> {out}")
    prune_clips(cfg, date.today())
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
