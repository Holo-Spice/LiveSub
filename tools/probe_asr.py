"""Loads the real speech models and runs one ASR call, with per-step timing.

    .venv\\Scripts\\python.exe -u tools\\probe_asr.py
"""

from __future__ import annotations

import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
T0 = time.monotonic()


def log(message: str) -> None:
    print(f"[{time.monotonic() - T0:7.2f}s] {message}", flush=True)


def main() -> int:
    config = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))["paths"]
    paths = {key: ROOT / value for key, value in config.items()}
    audio = ROOT / "testdata" / "source-ja.flac"
    log(f"decode {audio.name} to 16 kHz mono PCM")
    raw = subprocess.run(
        [str(paths["ffmpeg"]), "-nostdin", "-hide_banner", "-i", str(audio), "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
        capture_output=True, check=True,
    ).stdout
    log(f"pcm bytes={len(raw)} duration={len(raw) / 2 / 16000:.2f}s")

    from subtitle_cli.models import Models

    models = Models(paths, lambda step: log(f"progress: {step}"))
    log(f"cudnn/miopen enabled = {models.torch.backends.cudnn.enabled} (set LIVESUB_ENABLE_MIOPEN=1 to force it on)")
    log("models loaded; calling transcribe()")
    text, language = models.transcribe(raw, "ja")
    log(f"RESULT language={language!r} text={text!r}")
    log("ASR_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
