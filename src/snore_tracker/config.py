"""Settings, read from env vars (systemd loads them from .env)."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home()


def _env(name, default, cast=str):
    value = os.environ.get(name)
    return default if value in (None, "") else cast(value)


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: _env("DATA_DIR", HOME / "snore-data", Path))
    model_path: Path = field(
        default_factory=lambda: _env(
            "MODEL_PATH", HOME / "snore-tracker/models/yamnet.tflite", Path
        )
    )
    # Must match the watch: minute keys are local wall-clock time, like Garmin's.
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(_env("LOCAL_TZ", "America/Chicago")))
    # A minute counts as snoring when its peak YAMNet "Snoring" score reaches this.
    threshold: float = field(default_factory=lambda: _env("SNORE_THRESHOLD", 0.5, float))
    # Events: runs of snoring minutes, bridging gaps up to max_gap, kept if >= min_event.
    min_event_minutes: int = field(default_factory=lambda: _env("MIN_EVENT_MINUTES", 2, int))
    max_gap_minutes: int = field(default_factory=lambda: _env("MAX_GAP_MINUTES", 1, int))
    # dB is dBFS (uncalibrated mic) plus this offset; set it once the mic is calibrated.
    db_offset: float = field(default_factory=lambda: _env("DB_OFFSET", 0.0, float))
    loud_db: float = field(default_factory=lambda: _env("LOUD_DB", -20.0, float))
    clip_seconds: int = field(default_factory=lambda: _env("CLIP_SECONDS", 10, int))
    clip_days: int = field(default_factory=lambda: _env("CLIP_DAYS", 14, int))
    # Keep full-night audio after analysis, for threshold tuning. Off by default (privacy).
    keep_audio: bool = field(default_factory=lambda: _env("KEEP_AUDIO", "0") == "1")
    api_url: str = field(
        default_factory=lambda: _env("SNORE_API", "https://fitness.epetersons.com/api/snore")
    )
    token: str = field(default_factory=lambda: _env("SNORE_TOKEN", ""))

    @property
    def recordings(self):
        return self.data_dir / "recordings"

    @property
    def nights(self):
        return self.data_dir / "nights"

    @property
    def clips(self):
        return self.data_dir / "clips"
