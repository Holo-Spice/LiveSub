"""How short does an utterance have to be before the reported language goes wrong?

The failing run stopped on a 0.574 s fragment that the model called Chinese while the video is
Japanese. This probe takes one clearly Japanese utterance and feeds the model its first N
seconds, so the length at which the language field stops being trustworthy becomes a number
instead of a guess.

Nothing is written: the audio is cut in memory.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from subtitle_cli.cli import configured_paths  # noqa: E402
from subtitle_cli.models import Models  # noqa: E402

LENGTHS = (0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 4.0)


def main() -> int:
    wav = Path(sys.argv[1])
    paths = configured_paths(ROOT, "7b")
    models = Models(paths, lambda step: None)

    with wave.open(str(wav), "rb") as handle:
        pcm = handle.readframes(handle.getnframes())
    audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
    print(f"source: {wav.name}  {len(audio) / 16000:.3f}s", flush=True)

    for seconds in LENGTHS:
        samples = int(seconds * 16000)
        if samples > len(audio):
            continue
        request = models.asr_processor.apply_transcription_request(audio=audio[:samples], language=None)
        inputs = request.to(models.asr_model.device, models.asr_model.dtype)
        with models.torch.inference_mode():
            ids = models.asr_model.generate(**inputs, max_new_tokens=256)
        parsed = models.asr_processor.decode(ids[:, inputs["input_ids"].shape[1]:], return_format="parsed")[0]
        print(f"{seconds:4.1f}s  language={parsed['language']!r:12} text={parsed['transcription']!r}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
