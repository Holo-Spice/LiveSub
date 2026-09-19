"""Which segment makes the recogniser decide the audio is Chinese?

The failed run (`outputs/offline/1.zh.jsonl`) recorded nine Japanese subtitles and then
``unsupported detected source language: Chinese``. The recognised language of every utterance is
already in that log, so the only thing that has to be found is which utterance comes next and
what the model says about it.

This probe feeds the same PCM the pipeline would feed, one segment at a time, and prints the
language the model returns for each. It changes nothing: no file of the project is written, and
the audio comes from the same ffmpeg the CLI uses.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from subtitle_cli.audio import BLOCK_SAMPLES, SAMPLE_RATE, FFmpegInput  # noqa: E402
from subtitle_cli.cli import configured_paths  # noqa: E402
from subtitle_cli.models import Models  # noqa: E402
from subtitle_cli.pipeline import Segmenter  # noqa: E402


def note_step(step: str) -> None:
    print(f"  load: {step}", flush=True)


def main() -> int:
    video = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "testdata" / "1.mp4"
    paths = configured_paths(ROOT, "7b")

    print(f"video: {video}", flush=True)
    models = Models(paths, note_step)

    with FFmpegInput(paths["ffmpeg"], video=video) as audio:
        stream = audio.blocks()

        # One utterance at a time, segmented exactly the way the pipeline segments it, but with
        # the language of every utterance printed instead of being checked and thrown away.
        index = 0
        start_sample: int | None = None
        utterance = bytearray()
        history: list = []
        for block in stream:
            event = models.vad_event(block.data) if block.samples == BLOCK_SAMPLES else None
            if start_sample is None:
                history.append(block)
                if len(history) > 4:
                    history.pop(0)
                if event and "start" in event:
                    start = int(event["start"])
                    pieces = []
                    for earlier in history:
                        offset = max(0, start - earlier.start_sample)
                        if offset < earlier.samples:
                            pieces.append(earlier.data[offset * 2:])
                    start_sample = max(start, history[0].start_sample)
                    utterance = bytearray(b"".join(pieces))
                    history.clear()
                continue
            utterance.extend(block.data)
            duration = len(utterance) / (2 * SAMPLE_RATE)
            ready = bool(event and "end" in event) and models.turn_complete(bytes(utterance))
            if duration >= 30 and event and "end" in event:
                ready = True
            if not ready:
                continue
            index += 1
            seconds = start_sample / SAMPLE_RATE
            pcm = bytes(utterance)
            try:
                text, language = models.transcribe(pcm, "auto")
                print(f"[{index}] {seconds:7.3f}s +{duration:6.3f}s  language={language!r}  text={text[:70]!r}", flush=True)
            except ValueError as exc:
                # This is the failure that ends the real run. Here it is only reported, and the
                # audio is kept so the same segment can be fed to the model on its own.
                name = f"fail-{index:02d}-{seconds:.3f}".replace(".", "_")
                out = ROOT / "outputs" / "diag" / f"{name}.wav"
                with wave.open(str(out), "wb") as handle:
                    handle.setnchannels(1)
                    handle.setsampwidth(2)
                    handle.setframerate(SAMPLE_RATE)
                    handle.writeframes(pcm)
                print(f"[{index}] {seconds:7.3f}s +{duration:6.3f}s  LANGUAGE FAILURE {exc}  saved={out.name}", flush=True)
            start_sample = None
            utterance = bytearray()
        if start_sample is not None and models.turn_complete(bytes(utterance)):
            index += 1
            text, language = models.transcribe(bytes(utterance), "auto")
            print(f"[{index}] {start_sample / SAMPLE_RATE:7.3f}s +{len(utterance) / (2 * SAMPLE_RATE):6.3f}s  language={language!r}  text={text[:70]!r}", flush=True)
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
