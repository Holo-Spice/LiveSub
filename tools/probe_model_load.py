"""Staged probe for the 'model_load' stage: prints elapsed time after every step.

Run it with the project interpreter to see which part of the startup is slow:
    .venv\\Scripts\\python.exe -u tools\\probe_model_load.py
"""

from __future__ import annotations

import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T0 = time.monotonic()


def log(message: str) -> None:
    print(f"[{time.monotonic() - T0:7.2f}s] {message}", flush=True)


def main() -> int:
    log(f"python {sys.version.split()[0]} from {sys.executable}")
    config = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))["paths"]
    paths = {key: ROOT / value for key, value in config.items()}

    log("import torch")
    import torch

    log(f"torch {torch.__version__} hip={torch.version.hip} cuda_available={torch.cuda.is_available()}")
    if torch.cuda.is_available():
        log(f"device0={torch.cuda.get_device_name(0)}")
        log("run fp16 matmul")
        value = (torch.ones((16, 16), device="cuda:0", dtype=torch.float16) @ torch.ones((16, 16), device="cuda:0", dtype=torch.float16)).mean().item()
        log(f"fp16 matmul mean={value}")

    log("import onnxruntime")
    import onnxruntime as ort

    log("load silero vad onnx")
    from silero_vad import VADIterator, load_silero_vad

    VADIterator(load_silero_vad(onnx=True), sampling_rate=16000)
    log("silero ok")

    log("load smart turn onnx")
    ort.InferenceSession(str(paths["smart_turn"]), providers=["CPUExecutionProvider"])
    log("smart turn ok")

    log("import transformers")
    from transformers import AutoModelForMultimodalLM, AutoModelForTokenClassification, AutoProcessor, WhisperFeatureExtractor

    WhisperFeatureExtractor(chunk_length=8)
    log("whisper features ok")

    log("import wtpsplit")
    from wtpsplit import SaT

    log("construct SaT")
    SaT(str(paths["sat"]), tokenizer_name_or_path=str(paths["sat_tokenizer"]), ort_providers=["CPUExecutionProvider"])
    log("SaT ok")

    log("ASR processor from_pretrained")
    AutoProcessor.from_pretrained(paths["qwen_asr"], local_files_only=True)
    log("ASR processor ok")

    log("ASR model from_pretrained (fp16/eager)")
    asr = AutoModelForMultimodalLM.from_pretrained(paths["qwen_asr"], dtype=torch.float16, attn_implementation="eager", local_files_only=True)
    log("ASR weights loaded; moving to cuda:0")
    asr = asr.to("cuda:0").eval()
    log(f"ASR on device ok; vram_allocated={torch.cuda.memory_allocated() / 2**30:.2f} GiB")

    log("aligner processor from_pretrained")
    AutoProcessor.from_pretrained(paths["qwen_aligner"], local_files_only=True)
    log("aligner processor ok")

    log("aligner model from_pretrained (fp16/eager)")
    aligner = AutoModelForTokenClassification.from_pretrained(paths["qwen_aligner"], dtype=torch.float16, attn_implementation="eager", local_files_only=True)
    log("aligner weights loaded; moving to cuda:0")
    aligner = aligner.to("cuda:0").eval()
    log(f"aligner on device ok; vram_allocated={torch.cuda.memory_allocated() / 2**30:.2f} GiB")

    log("MODEL_LOAD_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
