"""How much of a task's time is the very first ASR call, and does a warm-up remove it?

The first call pays for whatever the convolution backend has to build before the first real
utterance; every later call is several times faster (measured: 4.86 s then 0.66 s on the same
6.98 s clip). This probe measures that gap directly, because if it is real then warming the
model up during startup is free time for the user.

    .venv\\Scripts\\python.exe -u tools\\probe_asr_warmup.py
"""

from __future__ import annotations

import subprocess
import sys
import time
import tomllib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def log(message: str) -> None:
    print(message, flush=True)


def main() -> int:
    config = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))["paths"]
    paths = {key: ROOT / value for key, value in config.items()}
    raw = subprocess.run(
        [str(paths["ffmpeg"]), "-nostdin", "-hide_banner", "-i", str(ROOT / "testdata" / "source-ja.flac"), "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
        capture_output=True, check=True,
    ).stdout

    from subtitle_cli.models import Models

    started = time.monotonic()
    models = Models(paths)
    log(f"model load {time.monotonic() - started:.2f} s")

    started = time.monotonic()
    text, language = models.transcribe(raw, "ja")
    log(f"first real call {time.monotonic() - started:6.2f} s  {language} {text[:40]!r}")

    started = time.monotonic()
    models.transcribe(raw, "ja")
    log(f"second real call {time.monotonic() - started:6.2f} s")

    # A short silent warm-up of the same shape as the real calls.
    silence = np.zeros(16000, dtype=np.int16).tobytes()
    started = time.monotonic()
    models.transcribe(silence, "ja")
    log(f"one second of warm-up costs {time.monotonic() - started:6.2f} s")

    started = time.monotonic()
    models.transcribe(raw, "ja")
    log(f"real call after warm-up {time.monotonic() - started:6.2f} s")

    # The aligner is a separate model with its own first-call cost.
    started = time.monotonic()
    models.align(silence, "テスト", "Japanese")
    log(f"first aligner call {time.monotonic() - started:6.2f} s")
    started = time.monotonic()
    models.align(raw, "森永の美味しい牛乳である。", "Japanese")
    log(f"aligner call after warm-up {time.monotonic() - started:6.2f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
