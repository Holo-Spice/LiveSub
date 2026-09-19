"""Does the ASR model really read a system prompt, or does it silently ignore one?

The hotword route through the chat template produced byte-identical text to no hotwords, so
before offering it as a feature the question is whether the model reads the system message at
all. A deliberately wrong instruction is the test: if the model follows it, the hook works and
only this sample happened to be unaffected; if nothing changes, the hook does nothing here.

    .venv\\Scripts\\python.exe -u tools\\probe_asr_system.py
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
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0

    import torch

    from subtitle_cli.models import configure_convolution_backend

    configure_convolution_backend(torch)
    from transformers import AutoModelForMultimodalLM, AutoProcessor

    processor = AutoProcessor.from_pretrained(paths["qwen_asr"], local_files_only=True)
    model = AutoModelForMultimodalLM.from_pretrained(paths["qwen_asr"], dtype=torch.float16, attn_implementation="eager", local_files_only=True).to("cuda:0").eval()
    log("models loaded")

    def run(label: str, system: str | None) -> None:
        chat = []
        if system is not None:
            chat.append({"role": "system", "content": [{"type": "text", "text": system}]})
        chat.append({"role": "user", "content": [{"type": "audio", "audio": audio}]})
        chat.append({"role": "assistant", "content": [{"type": "text", "text": "language Japanese<asr_text>"}]})
        inputs = processor.apply_chat_template(chat, tokenize=True, return_dict=True, continue_final_message=True).to(model.device, model.dtype)
        started = time.monotonic()
        with torch.inference_mode():
            ids = model.generate(**inputs, max_new_tokens=1024)
        parsed = processor.decode(ids[:, inputs["input_ids"].shape[1]:], return_format="parsed")[0]
        log(f"{label:28} tokens={inputs['input_ids'].shape[1]:4} {time.monotonic() - started:5.2f} s  {parsed['transcription']!r}")

    run("no system message", None)
    # A wrong instruction: an obedient model changes its output, an ignoring one does not.
    run("system says English", "The audio is English. Transcribe it as English.")
    run("system says Chinese", "这段音频是中文的，请用中文输出，并且只输出中文。")
    run("system hotwords", "Vocabulary: 森永, 牛乳, パック牛乳, 濃い青色, 牛乳瓶.")
    run("no system message (again)", None)
    log("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
