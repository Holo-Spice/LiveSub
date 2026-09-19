"""Time every import and ONNX/HF object Models.__init__ builds, one at a time.

The UI parks on "正在加载本地组件" (= the model_deps heartbeat) for 143-203 s while the
same work finishes in ~10 s from a console, so this probe isolates which single import
or constructor is responsible. Every line is a JSON record with the wall-clock cost.
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

RECORDS: list[dict] = []
FAULT_STREAM = None


def timed(label: str, work):
    started = time.monotonic()
    try:
        value = work()
    except Exception as exc:  # keep every earlier measurement
        RECORDS.append({"step": label, "seconds": round(time.monotonic() - started, 3), "error": f"{type(exc).__name__}: {exc}"})
        print(json.dumps(RECORDS[-1], ensure_ascii=False), flush=True)
        raise
    RECORDS.append({"step": label, "seconds": round(time.monotonic() - started, 3)})
    print(json.dumps(RECORDS[-1], ensure_ascii=False), flush=True)
    return value


def imported(name: str):
    __import__(name)
    return sys.modules[name]


def environment() -> dict:
    keys = sorted(k for k in os.environ if k.split("_")[0] in {
        "HIP", "ROCR", "HSA", "MIOPEN", "AMD", "OMP", "MKL", "CUDA", "TORCH", "HF", "TRANSFORMERS",
        "LIVESUB", "PYTHON", "KMP", "OPENBLAS", "NNPACK", "MIOPENXDG",
    })
    return {
        "executable": sys.executable,
        "cwd": str(Path.cwd()),
        "python": sys.version.split()[0],
        "path_entries": len(os.environ.get("PATH", "").split(os.pathsep)),
        "path_length": len(os.environ.get("PATH", "")),
        "env": {k: os.environ[k] for k in keys},
    }


def main() -> int:
    global FAULT_STREAM
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true", help="run cli.preflight() (FP16 matmul + llama-server --list-devices) first, like the real task does")
    parser.add_argument("--fault-after", type=float, default=0.0, metavar="SECONDS", help="dump every thread's stack every SECONDS to a file, so a stall names its own line")
    parser.add_argument("--fault-file", default=str(ROOT / "outputs" / "probe-imports-fault.txt"))
    options = parser.parse_args()
    if options.fault_after > 0:
        FAULT_STREAM = open(options.fault_file, "w", encoding="utf-8")
        faulthandler.dump_traceback_later(options.fault_after, repeat=True, file=FAULT_STREAM)
        print(json.dumps({"step": "faulthandler", "every_seconds": options.fault_after, "file": options.fault_file}, ensure_ascii=False), flush=True)

    print(json.dumps({"step": "environment", **environment()}, ensure_ascii=False), flush=True)
    cli = timed("subtitle_cli.cli (pre-model_deps)", lambda: imported("subtitle_cli.cli"))

    paths = cli.configured_paths(ROOT, "7b")
    if options.preflight:
        timed("cli.preflight (imports torch, FP16 matmul, spawns ffmpeg + llama-server)", lambda: cli.preflight(paths, "gpu"))
    timed("onnxruntime", lambda: imported("onnxruntime"))
    torch = timed("torch", lambda: imported("torch"))
    timed("torch.cuda.is_available", lambda: torch.cuda.is_available())
    timed("torch.hip_version", lambda: torch.version.hip)
    timed("torch.device_count", lambda: torch.cuda.device_count())
    timed("torch.device_name", lambda: torch.cuda.get_device_name(0))
    timed("silero_vad", lambda: imported("silero_vad"))
    timed("transformers", lambda: imported("transformers"))
    timed("wtpsplit", lambda: imported("wtpsplit"))
    from silero_vad import VADIterator, load_silero_vad  # noqa: PLC0415

    timed("load_silero_vad(onnx=True)", lambda: load_silero_vad(onnx=True))
    timed("WhisperFeatureExtractor", lambda: imported("transformers").WhisperFeatureExtractor(chunk_length=8))
    ort = sys.modules["onnxruntime"]
    timed("ort.InferenceSession(smart_turn)", lambda: ort.InferenceSession(str(paths["smart_turn"]), providers=["CPUExecutionProvider"]))
    from wtpsplit import SaT  # noqa: PLC0415

    timed("SaT(sat-3l-sm)", lambda: SaT(str(paths["sat"]), tokenizer_name_or_path=str(paths["sat_tokenizer"]), ort_providers=["CPUExecutionProvider"]))
    timed("VADIterator", lambda: VADIterator(load_silero_vad(onnx=True), sampling_rate=16000))
    if options.fault_after > 0:
        faulthandler.cancel_dump_traceback_later()
    print(json.dumps({"step": "total_seconds", "seconds": round(sum(r["seconds"] for r in RECORDS), 3)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
