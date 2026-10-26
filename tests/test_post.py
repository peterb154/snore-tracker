import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from snore_tracker import post
from snore_tracker.config import Config

heartbeats = []
missing_heartbeat = False


@pytest.fixture(autouse=True)
def pi(monkeypatch):
    global missing_heartbeat
    heartbeats.clear()
    missing_heartbeat = False
    monkeypatch.setattr(post, "recording_active", lambda: False)
    monkeypatch.setattr(post, "wifi_network", lambda: "hotspot")


@pytest.fixture
def server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path.endswith("/heartbeat"):
                heartbeats.append(json.loads(body))
                self.send_response(404 if missing_heartbeat else 200)
            else:
                received.append((self.headers["Authorization"], json.loads(body)))
                self.send_response(200 if self.headers["Authorization"] == "Bearer good" else 401)
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/api/snore", received
    srv.shutdown()


def write_nights(cfg, *nights):
    cfg.nights.mkdir(parents=True)
    for n in nights:
        (cfg.nights / f"{n}.json").write_text(json.dumps({"night": n}))


def test_posts_unsent_nights_and_marks_them(tmp_path, server, monkeypatch):
    url, received = server
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SNORE_API", url)
    monkeypatch.setenv("SNORE_TOKEN", "good")
    cfg = Config()
    write_nights(cfg, "2026-10-20", "2026-10-21")
    (cfg.nights / "2026-10-20.posted").touch()

    with pytest.raises(SystemExit) as exit_:
        post.main()

    assert exit_.value.code == 0
    assert received == [("Bearer good", {"night": "2026-10-21"})]
    assert post.unsent(cfg) == []


def test_rejected_night_stays_unsent(tmp_path, server, monkeypatch):
    url, _ = server
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SNORE_API", url)
    monkeypatch.setenv("SNORE_TOKEN", "bad")
    cfg = Config()
    write_nights(cfg, "2026-10-21")

    with pytest.raises(SystemExit) as exit_:
        post.main()

    assert exit_.value.code == 1
    assert [p.stem for p in post.unsent(cfg)] == ["2026-10-21"]


def test_heartbeat_reports_the_queue(tmp_path, server, monkeypatch):
    url, _ = server
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SNORE_API", url)
    monkeypatch.setenv("SNORE_TOKEN", "bad")  # nights get a 401 and stay queued
    write_nights(Config(), "2026-10-21")

    with pytest.raises(SystemExit):
        post.main()

    assert len(heartbeats) == 1
    beat = heartbeats[0]
    assert (beat["recording"], beat["queued"], beat["network"]) == (False, 1, "hotspot")


def test_failed_heartbeat_does_not_fail_the_run(tmp_path, server, monkeypatch):
    global missing_heartbeat
    missing_heartbeat = True  # garmin_connect without the endpoint yet
    url, _ = server
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SNORE_API", url)
    monkeypatch.setenv("SNORE_TOKEN", "good")
    write_nights(Config(), "2026-10-21")

    with pytest.raises(SystemExit) as exit_:
        post.main()

    assert exit_.value.code == 0
    assert post.unsent(Config()) == []
