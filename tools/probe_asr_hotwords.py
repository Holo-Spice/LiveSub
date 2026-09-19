"""Does the ASR hotword / context hook work at all, and what does it cost?

`apply_transcription_request` in this transformers build drops a `prompt` argument with a
warning, so the documented simple path is broken here. This probe compares the same audio
three ways and prints timing plus text for each, so the hook is adopted only if it works:

    .venv\\Scripts\\python.exe -u tools\\probe_asr_hotwords.py
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

WORDS = "森永, 牛乳, パック牛乳, 濃い青色, 牛乳瓶"


def log(message: str) -> None:
    print(message, flush=True)


def main() -> int:
    config = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))["paths"]
    paths = {key: ROOT / value for key, value in config.items()}
    raw = subprocess.run(
        [str(paths["ffmpeg"]), "-nostdin", "-hide_banner", "-i", str(ROOT / "testdata" / "source-ja.flac"), "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
        capture_output=True, check=True,
    ).stdout
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0

    import torch

    from subtitle_cli.models import configure_convolution_backend

    configure_convolution_backend(torch)
    from transformers import AutoModelForMultimodalLM, AutoProcessor

    processor = AutoProcessor.from_pretrained(paths["qwen_asr"], local_files_only=True)
    model = AutoModelForMultimodalLM.from_pretrained(paths["qwen_asr"], dtype=torch.float16, attn_implementation="eager", local_files_only=True).to("cuda:0").eval()
    log("models loaded")

    def decode(ids, prompt_length: int, label: str, spent: float) -> None:
        parsed = processor.decode(ids[:, prompt_length:], return_format="parsed")[0]
        log(f"{label:24} {spent:5.2f} s  {parsed['transcription']!r}")

    # 1. what the CLI does today
    inputs = processor.apply_transcription_request(audio=audio, language="Japanese").to(model.device, model.dtype)
    started = time.monotonic()
    with torch.inference_mode():
        ids = model.generate(**inputs, max_new_tokens=1024)
    decode(ids, inputs["input_ids"].shape[1], "baseline", time.monotonic() - started)

    # 2. the documented but unsupported simple path (kept to show what it really does)
    inputs = processor.apply_transcription_request(audio=audio, language="Japanese", prompt=f"Vocabulary: {WORDS}.").to(model.device, model.dtype)
    started = time.monotonic()
    with torch.inference_mode():
        ids = model.generate(**inputs, max_new_tokens=1024)
    decode(ids, inputs["input_ids"].shape[1], "prompt= (documented)", time.monotonic() - started)

    # 3. the chat template route, which is the one that is actually supported
    chat = [
        {"role": "system", "content": [{"type": "text", "text": f"Vocabulary: {WORDS}."}]},
        {"role": "user", "content": [{"type": "audio", "audio": audio}]},
        {"role": "assistant", "content": [{"type": "text", "text": "language Japanese<asr_text>"}]},
    ]
    try:
        inputs = processor.apply_chat_template(chat, tokenize=True, return_dict=True, continue_final_message=True).to(model.device, model.dtype)
        started = time.monotonic()
        with torch.inference_mode():
            ids = model.generate(**inputs, max_new_tokens=1024)
        decode(ids, inputs["input_ids"].shape[1], "chat system hotwords", time.monotonic() - started)
        log(f"prompt tokens with system message: {inputs['input_ids'].shape[1]}")
    except Exception as exc:
        log(f"chat template FAILED {type(exc).__name__}: {exc}")

    # 4. no hotwords, but through the same chat template, so 3 and 4 differ only by the list
    chat = [
        {"role": "user", "content": [{"type": "audio", "audio": audio}]},
        {"role": "assistant", "content": [{"type": "text", "text": "language Japanese<asr_text>"}]},
    ]
    try:
        inputs = processor.apply_chat_template(chat, tokenize=True, return_dict=True, continue_final_message=True).to(model.device, model.dtype)
        started = time.monotonic()
        with torch.inference_mode():
            ids = model.generate(**inputs, max_new_tokens=1024)
        decode(ids, inputs["input_ids"].shape[1], "chat, no hotwords", time.monotonic() - started)
        log(f"prompt tokens without system message: {inputs['input_ids'].shape[1]}")
    except Exception as exc:
        log(f"chat template (plain) FAILED {type(exc).__name__}: {exc}")

    log("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
