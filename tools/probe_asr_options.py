"""Measures what an ASR change would actually cost or save on this machine.

Runs the same 6.98 s Japanese clip several ways and prints one timing line per variant, so a
change is adopted only when the measurement supports it:

    .venv\\Scripts\\python.exe -u tools\\probe_asr_options.py

Variants:
  baseline          what the CLI does today (FP16, eager attention, the fixed backend)
  sdpa              the same, without forcing eager attention
  cache             generation with the key/value cache (transformers only, on demand)
  prompt            the same as the baseline plus a hotword list, to prove the model accepts
                    domain context and to see what it costs in time
  chunk1000         a larger audio convolution chunk, to see whether the conv path is the bill
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


def pcm_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def load_models(paths, torch):
    from transformers import AutoModelForMultimodalLM, AutoProcessor

    processor = AutoProcessor.from_pretrained(paths["qwen_asr"], local_files_only=True)
    model = AutoModelForMultimodalLM.from_pretrained(paths["qwen_asr"], dtype=torch.float16, attn_implementation="eager", local_files_only=True).to("cuda:0").eval()
    return processor, model


def run(processor, model, torch, audio, language, prompt=None, use_cache=False):
    inputs = processor.apply_transcription_request(audio=audio, language=language, prompt=prompt).to(model.device, model.dtype)
    started = time.monotonic()
    with torch.inference_mode():
        if use_cache:
            ids = model.generate(**inputs, max_new_tokens=1024, use_cache=True)
        else:
            ids = model.generate(**inputs, max_new_tokens=1024)
    spent = time.monotonic() - started
    parsed = processor.decode(ids[:, inputs["input_ids"].shape[1]:], return_format="parsed")[0]
    return spent, parsed


def main() -> int:
    config = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))["paths"]
    paths = {key: ROOT / value for key, value in config.items()}
    raw = subprocess.run(
        [str(paths["ffmpeg"]), "-nostdin", "-hide_banner", "-i", str(ROOT / "testdata" / "source-ja.flac"), "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
        capture_output=True, check=True,
    ).stdout
    audio = pcm_float(raw)
    log(f"audio {len(audio) / 16000:.2f} s")

    import torch

    from subtitle_cli.models import configure_convolution_backend

    configure_convolution_backend(torch)
    processor, model = load_models(paths, torch)
    log("models loaded")

    for label, kwargs in (
        ("baseline", {}),
        ("baseline (repeat)", {}),
        ("prompt", {"prompt": "Vocabulary: 森永, 牛乳, パック."}),
        ("cache", {"use_cache": True}),
    ):
        try:
            spent, parsed = run(processor, model, torch, audio, "Japanese", **kwargs)
            log(f"{label:20} {spent:6.2f} s  language={parsed['language']!r}  text={parsed['transcription'][:60]!r}")
        except Exception as exc:
            log(f"{label:20} FAILED {type(exc).__name__}: {exc}")

    # A different attention implementation is only interesting when it is faster.
    try:
        model.config._attn_implementation = "sdpa"
        spent, parsed = run(processor, model, torch, audio, "Japanese")
        log(f"{'sdpa':20} {spent:6.2f} s  text={parsed['transcription'][:60]!r}")
    except Exception as exc:
        log(f"{'sdpa':20} FAILED {type(exc).__name__}: {exc}")
    model.config._attn_implementation = "eager"

    log("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
