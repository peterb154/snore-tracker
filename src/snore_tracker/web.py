"""Web UI on :8000: live waveform + snore score, recording start/stop, stats, clip playback."""

import json
import shutil
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from snore_tracker.config import Config
from snore_tracker.live import LiveReader

RECORD_UNIT = "snore-record.service"
ANALYZE_UNIT = "snore-analyze.service"
TIMERS = ["snore-record.timer", "snore-analyze.timer", "snore-update.timer"]
INDEX = Path(__file__).parent / "static" / "index.html"
TOO_HOT_DB = -3
TOO_QUIET_DB = -45


def systemctl(*args):
    return subprocess.run(["systemctl", *args], capture_output=True, text=True)


def recording_state():
    out = systemctl(
        "show", "--timestamp=unix", "-p", "ActiveState", "-p", "ActiveEnterTimestamp", RECORD_UNIT
    ).stdout
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    active = props.get("ActiveState") == "active"
    started = props.get("ActiveEnterTimestamp", "").lstrip("@")
    return {"active": active, "started": int(started) if active and started else None}


def analyzing():
    """snore-analyze Conflicts= snore-record, so starting the recorder would kill it.
    It's a oneshot: "activating" while it runs, never "active"."""
    return systemctl("is-active", ANALYZE_UNIT).stdout.strip() in ("active", "activating")


def next_timers():
    out = systemctl("list-timers", "--all", "-o", "json", *TIMERS).stdout
    timers = {t: None for t in TIMERS}
    for t in json.loads(out or "[]"):
        timers[t["unit"]] = t["next"] // 1_000_000 if t["next"] else None
    return timers


def segments(cfg):
    segs = sorted(cfg.recordings.glob("*.opus")) if cfg.recordings.exists() else []
    return {
        "count": len(segs),
        "mb": round(sum(s.stat().st_size for s in segs) / 1e6, 1),
        "current": segs[-1].name if segs else None,
    }


def last_night(cfg):
    nights = sorted(cfg.nights.glob("*.json")) if cfg.nights.exists() else []
    if not nights:
        return None
    payload = json.loads(nights[-1].read_text())
    return {
        "night": payload["night"],
        "summary": payload["summary"],
        "posted": nights[-1].with_suffix(".posted").exists(),
    }


def level_hint(peak_db):
    if peak_db is None:
        return None
    if peak_db >= TOO_HOT_DB:
        return "too hot"
    if peak_db < TOO_QUIET_DB:
        return "too quiet"
    return "ok"


def status(cfg, level):
    rec = recording_state()
    if rec["started"]:
        rec["elapsed"] = int(time.time()) - rec["started"]
    disk_dir = cfg.data_dir if cfg.data_dir.exists() else Path.home()
    return {
        "recording": rec | segments(cfg),
        "level": level | {"hint": level_hint(level["peak_db"])},
        "disk_free_gb": round(shutil.disk_usage(disk_dir).free / 1e9, 1),
        "last_night": last_night(cfg),
        "timers": next_timers(),
        "clip_playback": clips_enabled(cfg),
        "threshold": cfg.threshold,
    }


def clips_enabled(cfg):
    return (cfg.data_dir / ".clip-playback").exists()


def clip_path(cfg, night, name):
    """The clip file, or None if it's missing or the path escapes the clips dir."""
    root = cfg.clips.resolve()
    path = (root / night / name).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def list_clips(cfg):
    clips = []
    nights = sorted(cfg.nights.glob("*.json"), reverse=True) if cfg.nights.exists() else []
    for f in nights:
        payload = json.loads(f.read_text())
        score = {m["t"]: m["snore_prob"] for m in payload["minutes"]}
        for e in payload["events"]:
            if "clip" not in e or not (cfg.data_dir / e["clip"]).is_file():
                continue  # pruned after clip_days
            clips.append(
                {
                    "night": payload["night"],
                    "start": e["start"],
                    "minutes": e["minutes"],
                    "peak_score": score.get(e["peak_t"]),
                    "peak_db": e["peak_db"],
                    # "clips/<night>/<file>" is also the URL path
                    "url": "/" + e["clip"],
                }
            )
    return clips


def create_app(cfg, reader):
    app = FastAPI()

    def require_clips():
        if not clips_enabled(cfg):  # checked per request: touch/rm the flag, no restart
            raise HTTPException(404)

    @app.get("/")
    def index():
        return FileResponse(INDEX)

    @app.get("/api/status")
    def get_status():
        return status(cfg, reader.level())

    @app.get("/api/live")
    def live():
        return StreamingResponse(
            reader.events(),
            media_type="text/event-stream",
            # NPM (nginx) buffers proxied responses by default, which batches SSE
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/record/{action}")
    def record(action: str, x_snore: str | None = Header(None)):
        # A custom header forces a CORS preflight, so another site's page can't press Start
        if x_snore != "1":
            raise HTTPException(403, "missing X-Snore header")
        if action not in ("start", "stop"):
            raise HTTPException(404)
        if action == "start" and analyzing():
            raise HTTPException(409, "snore-analyze is running; try again when it finishes")
        result = systemctl(action, RECORD_UNIT)  # allowed by the polkit rule
        if result.returncode != 0:
            raise HTTPException(500, result.stderr.strip())
        return recording_state()

    @app.get("/api/clips")
    def clips():
        require_clips()
        return list_clips(cfg)

    @app.get("/clips/{night}/{name}")
    def clip(night: str, name: str):
        require_clips()
        path = clip_path(cfg, night, name)
        if path is None:
            raise HTTPException(404)
        return FileResponse(path, media_type="audio/ogg")

    return app


def main():
    import uvicorn

    cfg = Config()
    model = None
    if cfg.model_path.exists():
        from snore_tracker.model import Yamnet

        model = Yamnet(cfg.model_path)
    reader = LiveReader(cfg.mic, model)
    reader.start()
    uvicorn.run(create_app(cfg, reader), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
