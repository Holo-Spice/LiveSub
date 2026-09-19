"""Feed the real ASR model the same utterance at 16 kHz and at 96 kHz and compare.

The 96 kHz file is generated without any resampling of the samples being compared: the 16 kHz
source is upsampled once so that both inputs carry the same speech, then each is fed to the model
exactly as the pipeline feeds it (raw samples, no rate argument).
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from subtitle_cli.cli import configured_paths

FFMPEG = Path(r"C:\Users\29279\LiveSub\tools\ffmpeg\bin\ffmpeg.exe")
SOURCE = Path(r"C:\Users\29279\LiveSub\testdata\source-ja.flac")
OUT_16 = Path(r"C:\Users\29279\LiveSub\testdata\_ratecheck_16k.raw")
OUT_96 = Path(r"C:\Users\29279\LiveSub\testdata\_ratecheck_96k.raw")

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 7.0

# 16 kHz reference, exactly the offline pipe format.
subprocess.run(
    [str(FFMPEG), "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(SOURCE),
     "-t", str(seconds), "-ac", "1", "-ar", "16000", "-f", "s16le", str(OUT_16)],
    check=True,
)
# The same speech at 96 kHz: one conversion, then nothing else changes.
subprocess.run(
    [str(FFMPEG), "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
     "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", str(OUT_16),
     "-ac", "1", "-ar", "96000", "-f", "s16le", str(OUT_96)],
    check=True,
)

pcm16 = OUT_16.read_bytes()
pcm96 = OUT_96.read_bytes()
print(f"16 kHz input: {len(pcm16)} bytes = {len(pcm16) / 2 / 16000:.2f} s of samples")
print(f"96 kHz input: {len(pcm96)} bytes = {len(pcm96) / 2 / 96000:.2f} s of samples")
print(f"same audio  : {len(pcm96) == len(pcm16) * 6}")

paths = configured_paths(Path("."), "7b")
from subtitle_cli.models import Models

models = Models(paths, hotwords="")
print("models loaded; warmup:", models._warmup_notes)

for label, raw in (("16 kHz", pcm16), ("96 kHz", pcm96)):
    started = time.perf_counter()
    try:
        text, language = models.transcribe(raw, "ja")
        spent = time.perf_counter() - started
        print(f"\n--- {label} -> language={language}  ({spent:.2f}s) ---")
        print(repr(text))
    except Exception as exc:
        spent = time.perf_counter() - started
        print(f"\n--- {label} -> {type(exc).__name__} ({spent:.2f}s): {str(exc)[:300]}")

for path in (OUT_16, OUT_96):
    path.unlink(missing_ok=True)
