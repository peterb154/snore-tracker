"""POST every unsent nights/<date>.json to garmin_connect. A <date>.posted marker means done.

Then send a heartbeat (time, recording, queued nights, Wi-Fi network) so garmin_connect can show
when the Pi was last seen. snore-post.timer runs this every 15 min.
"""

import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime

from snore_tracker.config import Config


def post_json(url, body, cfg):
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {cfg.token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def post_night(path, cfg):
    return post_json(cfg.api_url, path.read_bytes(), cfg)


def recording_active():
    return subprocess.run(["systemctl", "is-active", "--quiet", "snore-record"]).returncode == 0


def wifi_network():
    """The active Wi-Fi connection's name (solonet, hotspot), or None."""
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-g", "NAME,TYPE", "connection", "show", "--active"],
            capture_output=True,
            text=True,
        ).stdout
    except FileNotFoundError:
        return None
    for line in out.splitlines():
        name, _, kind = line.rpartition(":")
        if kind == "802-11-wireless":
            return name
    return None


def send_heartbeat(cfg):
    body = {
        "at": datetime.now(cfg.tz).replace(tzinfo=None).isoformat(timespec="seconds"),
        "recording": recording_active(),
        "queued": len(unsent(cfg)),
        "network": wifi_network(),
    }
    return post_json(cfg.api_url.rstrip("/") + "/heartbeat", json.dumps(body).encode(), cfg)


def unsent(cfg):
    if not cfg.nights.exists():
        return []
    return [p for p in sorted(cfg.nights.glob("*.json")) if not p.with_suffix(".posted").exists()]


def main():
    cfg = Config()
    if not cfg.token:
        sys.exit("SNORE_TOKEN not set")
    failed = False
    for path in unsent(cfg):
        try:
            status = post_night(path, cfg)
        except (urllib.error.URLError, TimeoutError) as e:  # retried on the next run
            detail = (
                e.read().decode(errors="replace")[:500]
                if isinstance(e, urllib.error.HTTPError)
                else ""
            )
            print(f"{path.stem}: FAILED {e} {detail}", file=sys.stderr)
            failed = True
            continue
        path.with_suffix(".posted").touch()
        print(f"{path.stem}: posted ({status})")
    try:
        send_heartbeat(cfg)
    except (urllib.error.URLError, TimeoutError) as e:  # nights decide the exit code, not this
        print(f"heartbeat: FAILED {e}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
