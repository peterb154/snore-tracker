"""Live mic reader for the web UI: waveform slices every 50 ms, a YAMNet score per window."""

import json
import subprocess
import threading
import time
from collections import deque

import numpy as np

from snore_tracker import audio

SLICE = audio.SAMPLE_RATE // 20  # 50 ms
PEAK_WINDOWS = 10  # ~10 s of YAMNet windows for the peak level


def slice_stats(samples):
    """One (min, max, rms) per whole 50 ms slice; the trailing partial slice is dropped."""
    n = len(samples) // SLICE
    s = samples[: n * SLICE].reshape(n, SLICE).astype(np.float64)
    rms = np.sqrt(np.mean(np.square(s), axis=1))
    return [
        (round(float(lo), 4), round(float(hi), 4), round(float(r), 4))
        for lo, hi, r in zip(s.min(axis=1), s.max(axis=1), rms, strict=True)
    ]


class LiveReader:
    """Reads the shared dsnoop PCM in a background thread; SSE clients wait on `cond`."""

    def __init__(self, mic, model=None):
        self.mic = mic
        self.model = model  # None: waveform and level only (no model file, e.g. on a laptop)
        self.cond = threading.Condition()
        self.messages = deque(maxlen=200)  # (seq, json)
        self.seq = 0
        self.live = False
        self.last = {"db": None, "score": None}
        self.recent_db = deque(maxlen=PEAK_WINDOWS)

    def start(self):
        threading.Thread(target=self._run_forever, daemon=True).start()

    def _run_forever(self):
        while True:
            try:
                self._read()
            except Exception as e:  # keep the web UI up if the mic is unplugged
                print(f"live reader: {e}", flush=True)
            self.live = False
            time.sleep(5)

    def _read(self):
        # arecord flushes every ALSA period, so it lags less than ffmpeg on a pipe
        proc = subprocess.Popen(
            ["arecord", "-q", "-D", self.mic, "-f", "FLOAT_LE", "-c", "1",
             "-r", str(audio.SAMPLE_RATE), "-t", "raw"],
            stdout=subprocess.PIPE,
        )  # fmt: skip
        window = np.empty(0, dtype=np.float32)
        try:
            while chunk := proc.stdout.read(SLICE * 4):
                self.live = True
                samples = np.frombuffer(chunk, dtype=np.float32)
                for lo, hi, rms in slice_stats(samples):
                    self._publish({"t": "wave", "min": lo, "max": hi, "rms": rms})
                window = np.concatenate([window, samples])
                if len(window) >= audio.WINDOW:
                    self._score(window[: audio.WINDOW])
                    window = window[audio.WINDOW :]
        finally:
            proc.kill()
            proc.wait()

    def _score(self, window):
        db = round(audio.dbfs(window), 1)
        score = round(self.model.snore_score(window), 3) if self.model else None
        self.recent_db.append(db)
        self.last = {"db": db, "score": score}
        self._publish({"t": "score", "db": db, "score": score})

    def _publish(self, msg):
        with self.cond:
            self.seq += 1
            self.messages.append((self.seq, json.dumps(msg)))
            self.cond.notify_all()

    def level(self):
        return {
            "live": self.live,
            "db": self.last["db"],
            "peak_db": max(self.recent_db, default=None),
            "score": self.last["score"],
        }

    def events(self, timeout=15):
        """SSE stream. Starlette runs this sync generator in a worker thread."""
        last = self.seq
        while True:
            with self.cond:
                self.cond.wait_for(lambda last=last: self.seq > last, timeout=timeout)
                new = [(s, m) for s, m in self.messages if s > last]
            if not new:
                yield ": ping\n\n"
                continue
            last = new[-1][0]
            yield "".join(f"data: {m}\n\n" for _, m in new)
