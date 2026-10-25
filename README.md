# snore-tracker

Bedside snore tracker: a Raspberry Pi 4 with a USB mic records overnight, and each morning
[YAMNet](https://github.com/tensorflow/models/tree/master/research/audioset/yamnet) scores the
night. The results go to [garmin_connect](https://github.com/peterb154/garmin_connect)'s
`POST /api/snore`, where they line up minute by minute with Garmin's SpO2 and breathing data.

```
20:30  snore-record.timer   ffmpeg → ~/snore-data/recordings/<start>.opus (hourly, 16 kHz, 24 kbps)
08:30  snore-analyze.timer  stop recording → YAMNet → nights/<date>.json + clips → POST
always snore-web.service    web UI on :8000 (https://snore.epetersons.com, LAN-only)
15 min snore-post.timer     retry any unsent nights (and 2 min after boot)
```

**Privacy:** full-night audio never leaves the Pi and is deleted once it's analyzed. One 10 s
clip per event is kept for 14 days, so detections can be checked by ear. The web UI streams
levels and scores, never raw audio. Clips can be played over the LAN **only while
`~/snore-data/.clip-playback` exists** (see [Web UI](#web-ui)).

## Setup (Pi)

The Pi needs Raspberry Pi OS (Debian 13 is fine), `ffmpeg`, timezone `America/Chicago`, and NTP.
The timestamps have to match the watch. The Pi has no RTC, so `deploy/wait-ntp.sh` holds a recording
start up to 120 s for an NTP sync, then records anyway and logs a warning (`journalctl -u snore-record -p warning`).

Wi-Fi falls back to the phone hotspot (NetworkManager profile `hotspot`, priority -10 vs `solonet`'s 10).
On the hotspot `fitness.epetersons.com` doesn't resolve, so nights queue until the Pi is home again.

```bash
git clone https://github.com/peterb154/snore-tracker ~/snore-tracker
~/snore-tracker/deploy/install.sh   # uv, deps, model, systemd timers
nano ~/snore-tracker/.env           # SNORE_MIC, SNORE_TOKEN, DEPLOY_TOKEN
```

Deploys work like every other app in `local_network`: a push to `main` fires the n8n
*Deploy: snore-tracker* workflow, which calls `POST /api/deploy` with `DEPLOY_TOKEN`. That runs
`git pull --ff-only`, `uv sync --frozen --no-dev` and restarts `snore-web`; n8n emails on failure.
It returns 409 while `snore-analyze` or another deploy is running (a recording is unaffected);
avoid pushing right before 08:30, since analysis can still start mid-sync.
`curl https://snore.epetersons.com/api/health` shows the commit `snore-web` is running. Deploys never touch
units: if a change touches `deploy/*` (units, `asound.conf`, polkit rule), re-run
`deploy/install.sh`.

To keep the board LEDs dark in the bedroom, append this to `/boot/firmware/config.txt` and reboot.
On a Pi 4 the red PWR LED's polarity is inverted, so it needs `default-on` to stay off.

```ini
dtparam=act_led_trigger=none
dtparam=act_led_activelow=off
dtparam=pwr_led_trigger=default-on
dtparam=pwr_led_activelow=off
```

## Mic

- Put it near the headboard, about 1 m from the sleeper's head, away from fans and vents.
- The recorder and web UI share the mic through the ALSA `dsnoop` PCM `snoop`
  (`deploy/asound.conf` → `/etc/asound.conf`). Find the card name with `arecord -l` (the word in
  `[brackets]`), put it in `hw:CARD=<name>,DEV=0` there, and keep `SNORE_MIC=snoop` in `.env`.
- Check that two readers work at once:
  `arecord -D snoop -f S16_LE -r 16000 -c 1 -d 5 a.wav & arecord -D snoop -f S16_LE -r 16000 -c 1 -d 5 b.wav`.
  If dsnoop rejects the rate, change `rate` in `asound.conf` to the card's native rate
  (`arecord -D hw:CARD=<name> --dump-hw-params`).
- Set the gain with the web UI's **Tuning** slider (see [Web UI](#web-ui)).

## Web UI

`snore-web.service` serves a single page on `:8000`: live waveform and level, the live YAMNet
snore score with a 60 s sparkline, recording Start/Stop, segment/disk stats, last night's
summary, and the next timer runs. The waveform and score work whether or not a recording is running.

**Tuning** has two sliders, applied when you let go:

- **Mic gain** (0–16) runs `amixer -c $SNORE_CARD sset Mic <n>` and turns Auto Gain Control off,
  since AGC lifts the noise floor in a quiet room. Fake-snore at the pillow and aim for peaks
  around −10 to −6 dBFS.
- **Threshold** moves the line on the score chart at once and is used by the next morning's
  analysis.

Both are saved to `~/snore-data/settings.json`, which wins over `.env`. `snore-web` re-applies
the gain at startup, so there's no `sudo alsactl store`.

The score is YAMNet's 0–1 confidence for its "Snoring" class over each ~1 s window: higher means
more snore-like. The nightly analysis takes each minute's peak score, and a minute counts as
snoring at or above `SNORE_THRESHOLD`. Real snores often score below 1.0, so set the threshold
from what your own snoring scores.

- **Start/Stop** runs `systemctl start|stop snore-record.service` as `pi`, allowed by
  `deploy/polkit/50-snore.rules` (that unit, plus `snore-web.service` for deploys). A daytime
  test recording is grouped into the *next* night by `night_of()`, so only Start when you mean it.
- **Clip playback** is off by default. `touch ~/snore-data/.clip-playback` turns it on and `rm`
  turns it off; there's no restart. While it's off, `/api/clips` and `/clips/*` return 404.
- No auth except on `/api/deploy`: LAN-only through NPM
  (`snore.epetersons.com` → `192.168.0.14:8000`), like `fitness`.
- Logs: `journalctl -u snore-web -f`.

## Tuning

Gain and threshold are set in the web UI (above). The rest are in `.env`
(see `deploy/env.example`):

| Var | Default | Meaning |
|---|---|---|
| `SNORE_THRESHOLD` | 0.5 | a minute counts as snoring if its peak YAMNet "Snoring" score ≥ this (the UI's value wins) |
| `SNORE_CARD` | Device | ALSA card the gain slider controls |
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
  "summary": {"snore_minutes": 42, "snore_pct": 5.8, "events": 6, "loud_events": 1, "max_db": -18.2,
              "threshold": 0.5},
  "minutes": [{"t": "2026-10-21T02:12", "snore_prob": 0.83, "db_avg": -41.2, "db_max": -22.5}],
  "events": [{"start": "2026-10-21T02:10", "end": "2026-10-21T02:17", "minutes": 7,
              "peak_t": "2026-10-21T02:12", "peak_db": -22.5, "clip": "clips/2026-10-21/02-12-31.opus"}]
}
```

- `night` is the wake-up date, which matches Garmin's `sleep.day`.
- All times are local, with no offset.
- `summary.threshold` is the snore cutoff the night was analyzed with; garmin_connect uses it to
  re-count snore minutes over Garmin's sleep window.
- `events` is extra local detail; the API ignores it.
- `nights/<date>.posted` marks a night that has been sent. `snore-post.timer` retries unsent
  nights 2 min after boot and every 15 min (`journalctl -u snore-post`).

## Dev

```bash
uv sync && uv run pytest && uv run ruff check . && uv run ruff format --check .
```
