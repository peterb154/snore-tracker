# snore-tracker

Bedside snore tracker: a Raspberry Pi 4 with a USB mic records overnight, and each morning
[YAMNet](https://github.com/tensorflow/models/tree/master/research/audioset/yamnet) scores the
night. The results go to [garmin_connect](https://github.com/peterb154/garmin_connect)'s
`POST /api/snore`, where they line up minute by minute with Garmin's SpO2 and breathing data.

```
20:30  snore-record.timer   ffmpeg → ~/snore-data/recordings/<start>.opus (hourly, 16 kHz, 24 kbps)
08:30  snore-analyze.timer  stop recording → YAMNet → nights/<date>.json + clips → POST
```

**Privacy:** no audio leaves the Pi. Full-night audio is deleted once it's analyzed. One 10 s
clip per event is kept for 14 days, so detections can be checked by ear.

## Setup (Pi)

The Pi needs Raspberry Pi OS (Debian 13 is fine), `ffmpeg`, timezone `America/Chicago`, and NTP.
The timestamps have to match the watch.

```bash
git clone https://github.com/peterb154/snore-tracker ~/snore-tracker
~/snore-tracker/deploy/install.sh   # uv, deps, model, systemd timers
nano ~/snore-tracker/.env           # SNORE_MIC, SNORE_TOKEN
```

## Mic

- Put it near the headboard, about 1 m from the sleeper's head, away from fans and vents.
- Find the device: `arecord -L`, then set `SNORE_MIC=plughw:CARD=<name>,DEV=0`.
- Check the level: run `arecord -D <mic> -f S16_LE -r 16000 -d 10 test.wav`, then
  `alsamixer` (F6 → mic). Normal speech at the pillow should be well under clipping.

## Tuning

The settings are in `.env` (see `deploy/env.example`):

| Var | Default | Meaning |
|---|---|---|
| `SNORE_THRESHOLD` | 0.5 | a minute counts as snoring if its peak YAMNet "Snoring" score ≥ this |
| `MIN_EVENT_MINUTES` | 2 | shortest run of snoring minutes that counts as an event |
| `MAX_GAP_MINUTES` | 1 | quiet minutes bridged inside one event |
| `LOUD_DB` / `DB_OFFSET` | -20 / 0 | dB is dBFS (uncalibrated) + offset |
| `KEEP_AUDIO` | 0 | `1` keeps full-night audio so you can re-run with new settings |

To re-run by hand:

```bash
cd ~/snore-tracker && set -a && . ./.env && set +a
.venv/bin/snore-analyze && .venv/bin/snore-post
journalctl -u snore-analyze -n 50     # last scheduled run
```

## Payload (`POST /api/snore`)

Header `Authorization: Bearer $SNORE_TOKEN`. Re-posting a night replaces it. The contract lives
in garmin_connect `app/main.py` (`SnoreNight`).

```json
{
  "night": "2026-10-21",
  "start": "2026-10-20T20:30:00",
  "end": "2026-10-21T08:29:59",
  "source": "pi4-yamnet-v1",
  "summary": {"snore_minutes": 42, "snore_pct": 5.8, "events": 6, "loud_events": 1, "max_db": -18.2},
  "minutes": [{"t": "2026-10-21T02:12", "snore_prob": 0.83, "db_avg": -41.2, "db_max": -22.5}],
  "events": [{"start": "2026-10-21T02:10", "end": "2026-10-21T02:17", "minutes": 7,
              "peak_t": "2026-10-21T02:12", "peak_db": -22.5, "clip": "clips/2026-10-21/02-12-31.opus"}]
}
```

- `night` is the wake-up date, which matches Garmin's `sleep.day`.
- All times are local, with no offset.
- `events` is extra local detail; the API ignores it.
- `nights/<date>.posted` marks a night that has been sent. Unsent nights are retried every morning.

## Dev

```bash
uv sync && uv run pytest && uv run ruff check . && uv run ruff format --check .
```
