"""ffmpeg in, numpy out. Plus clip extraction."""

import subprocess

import numpy as np

SAMPLE_RATE = 16000
WINDOW = 15600  # YAMNet TFLite input: 0.975 s at 16 kHz
WINDOW_SECONDS = WINDOW / SAMPLE_RATE


def decode(path):
    """Any audio file -> 16 kHz mono float32 in [-1, 1]."""
    out = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    return np.frombuffer(out, dtype=np.float32)


def windows(samples):
    """Split into whole YAMNet windows; the trailing partial window is dropped."""
    n = len(samples) // WINDOW
    return samples[: n * WINDOW].reshape(n, WINDOW)


def dbfs(window):
    rms = float(np.sqrt(np.mean(np.square(window, dtype=np.float64))))
    return 20 * np.log10(max(rms, 1e-10))


def cut_clip(src, offset_seconds, seconds, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{max(offset_seconds, 0):.2f}",
            "-i",
            str(src),
            "-t",
            str(seconds),
            "-c:a",
            "libopus",
            "-b:a",
            "24k",
            str(dest),
        ],
        check=True,
    )
