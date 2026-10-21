import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from snore_tracker import post
from snore_tracker.config import Config


@pytest.fixture
def server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
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
