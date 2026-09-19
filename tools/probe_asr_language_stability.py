"""Is the language the recogniser reports stable for one and the same audio?

`tools/probe_asr_language.py` showed the whole pipeline, segmented the way it really is, and the
utterance starting at 22.818 s came back as Chinese while the six before it came back as
Japanese. This probe takes that single utterance, feeds the identical PCM several times, and
prints what the model says each time - including the transcript of the runs it refuses.

Nothing is written outside stdout. The audio is read from `outputs/diag/utterance-*.wav`, which
is what the pipeline's own ffmpeg produced.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from subtitle_cli.cli import configured_paths  # noqa: E402
from subtitle_cli.models import Models  # noqa: E402

RUNS = 4


def main() -> int:
    wav = Path(sys.argv[1])
    paths = configured_paths(ROOT, "7b")
    models = Models(paths, lambda step: None)

    if wav.suffix.lower() == ".wav":
        with wave.open(str(wav), "rb") as handle:
            pcm = handle.readframes(handle.getnframes())
    else:
        # A video or any other container: the pipeline's own ffmpeg decodes it the same way the
        # CLI does, so the model sees exactly the samples it would see in a real run.
        from subtitle_cli.audio import FFmpegInput

        with FFmpegInput(paths["ffmpeg"], video=wav) as audio:
            pcm = b"".join(block.data for block in audio.blocks())
    seconds = len(pcm) / (2 * 16000)
    print(f"audio: {wav.name}  {seconds:.3f}s", flush=True)

    # The processor's own request, which is what the pipeline sends.
    for run in range(1, RUNS + 1):
        request = models.asr_processor.apply_transcription_request(audio=_floats(models, pcm), language=None)
        inputs = request.to(models.asr_model.device, models.asr_model.dtype)
        with models.torch.inference_mode():
            ids = models.asr_model.generate(**inputs, max_new_tokens=1024)
        parsed = models.asr_processor.decode(ids[:, inputs["input_ids"].shape[1]:], return_format="parsed")[0]
        print(f"auto#{run}: language={parsed['language']!r} text={parsed['transcription']!r}", flush=True)

    # The same audio with the language pinned, which is what the UI's 日语 option does.
    for language in ("Japanese", "English"):
        request = models.asr_processor.apply_transcription_request(audio=_floats(models, pcm), language=language)
        inputs = request.to(models.asr_model.device, models.asr_model.dtype)
        with models.torch.inference_mode():
            ids = models.asr_model.generate(**inputs, max_new_tokens=1024)
        parsed = models.asr_processor.decode(ids[:, inputs["input_ids"].shape[1]:], return_format="parsed")[0]
        print(f"pinned {language}: language={parsed['language']!r} text={parsed['transcription']!r}", flush=True)

    # Greedy decoding is deterministic, so the language of one and the same audio cannot vary
    # between runs; this prints the values to prove the decode really is greedy.
    config = models.asr_model.generation_config
    print("decoding:", flush=True)
    print("  do_sample =", config.do_sample, flush=True)
    print("  temperature =", config.temperature, flush=True)
    print("  top_p =", config.top_p, flush=True)
    print("  top_k =", config.top_k, flush=True)
    print("  num_beams =", config.num_beams, flush=True)
    return 0


def _floats(models, pcm: bytes):
    import numpy as np

    audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
    return audio


if __name__ == "__main__":
    raise SystemExit(main())
