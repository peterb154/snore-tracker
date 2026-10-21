"""POST every unsent nights/<date>.json to garmin_connect. A <date>.posted marker means done."""

import sys
import urllib.error
import urllib.request

from snore_tracker.config import Config


def post_night(path, cfg):
    req = urllib.request.Request(
        cfg.api_url,
        data=path.read_bytes(),
        headers={"Authorization": f"Bearer {cfg.token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


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
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
