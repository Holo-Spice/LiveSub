"""VAD candidate pauses, Smart Turn commits, then ASR, SaT, alignment and MT.

The segmentation (VAD + Smart Turn) is shared by both modes. Live mode processes every
committed utterance inline, because audio arrives at real time and there is nothing to
overlap with. Offline mode instead hands the utterance to a worker, so decoding the video,
recognising speech and translating run at the same time: the file already exists in full, so
waiting for playback speed would be pure waste.
"""

from __future__ import annotations

import collections
import math
import queue
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from .audio import BLOCK_SAMPLES, PCMBlock, SAMPLE_RATE, media_time

# One utterance longer than this is cut at the next pause instead of growing without bound;
# the forced aligner accepts 300 seconds, but the ASR quality and the alignment cost both
# degrade on very long single utterances.
MAX_SEGMENT_SECONDS = 30.0
ALIGNER_LIMIT_SECONDS = 300.0
# Below this a cue has no duration at all, which is not a subtitle.
MIN_SENTENCE_DURATION_SECONDS = 0.001
# The forced aligner's timestamp resolution. A sentence shorter than one step has its words placed
# on the same grid point, which is not a mapping error: on Japanese video a one-second utterance
# collapsed to a single timestamp and `sentence has no positive alignment duration` stopped the
# task after 96 subtitles. Such a sentence is given this much time instead — never more, and never
# a share of the utterance worked out from its text.
ALIGNMENT_GRID_SECONDS = 0.08


class PipelineFailure(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


@dataclass(frozen=True)
class TimedSentence:
    text: str
    char_start: int
    char_end: int
    start: float
    end: float


@dataclass
class Translated:
    """One sentence of one utterance, with its translation filled in later."""

    sentence: TimedSentence
    start_sample: int
    translation: str | None = None


@dataclass
class Segment:
    """One committed utterance: the PCM to recognise plus where it sits in the media."""

    pcm: bytes
    start_sample: int
    position: int
    backlog_seconds: float = 0.0

    @property
    def duration(self) -> float:
        return len(self.pcm) / (2 * SAMPLE_RATE)


@dataclass
class SegmentResult:
    segment: Segment
    language: str = ""
    source: str = ""
    sentences: list[Translated] = field(default_factory=list)
    empty_transcript: bool = False
    truncated: bool = False
    started: float = 0.0
    finished: float = 0.0


def build_sentences(text: str, sentences: list[str], aligned: list[dict], units: Callable[[str], list[str]], *, writer=None) -> list[TimedSentence]:
    if not text or not sentences or "".join(sentences) != text:
        raise PipelineFailure("sentence_mapping", "SaT sentences do not reconstruct the alignment text")
    whole_units = units(text)
    if not whole_units or any(not unit for unit in whole_units):
        raise PipelineFailure("sentence_mapping", "alignment text has no usable alignment units")
    if len(aligned) != len(whole_units) or [item.get("text") for item in aligned] != whole_units:
        raise PipelineFailure("sentence_mapping", "forced aligner items differ from text units")
    # Match the processor's token cleaning, without tokenizing each SaT fragment again.
    kept = lambda char: char == "'" or unicodedata.category(char)[0] in ("L", "N")
    projected = "".join(char for char in text if kept(char))
    if "".join(whole_units) != projected:
        raise PipelineFailure("sentence_mapping", "alignment units cannot be located in the original text")
    kept_prefix = [0]
    for char in text:
        kept_prefix.append(kept_prefix[-1] + int(kept(char)))
    unit_boundaries = {}
    unit_length = 0
    for index, unit in enumerate(whole_units, 1):
        unit_length += len(unit)
        unit_boundaries[unit_length] = index

    # A SaT boundary inside an aligned unit has no exact timestamp. Keep the
    # adjacent sentences together, including any punctuation-only fragment.
    splits = [(0, 0)]
    char_position = 0
    for sentence in sentences[:-1]:
        char_position += len(sentence)
        unit_position = unit_boundaries.get(kept_prefix[char_position])
        if unit_position is not None and splits[-1][1] < unit_position < len(whole_units):
            splits.append((char_position, unit_position))
    splits.append((len(text), len(whole_units)))

    result = []
    previous_end = 0.0
    for (char_start, unit_start), (char_end, unit_end) in zip(splits, splits[1:]):
        items = aligned[unit_start:unit_end]
        for item in items:
            start, end = item.get("start_time"), item.get("end_time")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not math.isfinite(start) or not math.isfinite(end) or end < start:
                raise PipelineFailure("sentence_mapping", "invalid or non-monotonic alignment time")
        if items[-1]["end_time"] > items[0]["start_time"]:
            if items[0]["start_time"] < previous_end:
                raise PipelineFailure("sentence_mapping", "invalid or non-monotonic alignment time")
            previous_end = items[-1]["end_time"]
        result.append(TimedSentence(text[char_start:char_end], char_start, char_end, items[0]["start_time"], items[-1]["end_time"]))
    # Even if every sentence collapsed onto one aligner grid point, return the mapping. The
    # caller knows the actual utterance duration and can use that as a coarse fallback instead of
    # aborting an otherwise healthy long-running subtitle job.
    return result


def map_sentences(text: str, sentences: list[str], aligned: list[dict], units: Callable[[str], list[str]]) -> list[TimedSentence]:
    """Kept for the existing callers and tests."""
    return build_sentences(text, sentences, aligned, units)


def clamp_to_audio(sentences: list[TimedSentence], duration: float, *, minimum: float = MIN_SENTENCE_DURATION_SECONDS) -> tuple[list[TimedSentence], float, int]:
    """Put every sentence on the audio that was actually captured.

    Two limits of the aligner are handled here, and nothing else. It answers on a fixed token grid,
    so the last boundary sentence can end a fraction of one grid step after the PCM (on Japanese
    video: `alignment end 3.440s exceeds utterance audio 3.422s`, 18 ms over an 80 ms grid), and a
    sentence shorter than one grid step collapses onto a single timestamp. The first is clamped
    onto the audio; the second has no time anywhere and is dropped, counted and recorded by the
    caller, because no re-timing of it would be a measurement. Both used to stop whole tasks.

    A sentence that has real times keeps them unless they fall outside this audio, and a start may
    only move forward onto the previous cue's end. Nothing here re-times a sentence by proportion,
    by its text length or by the sentence before it. Returns the sentences, the largest trim and
    how many sentences had no time and were dropped.
    """
    trimmed = 0.0
    dropped = 0
    clamped: list[TimedSentence] = []
    for sentence in sentences:
        end = max(0.0, min(duration, sentence.end))
        start = max(0.0, min(end, sentence.start))
        trimmed = max(trimmed, sentence.end - end, sentence.start - start)
        if end <= start:
            # The aligner put every word of this sentence on one grid point because the sentence is
            # shorter than its timestamp resolution. There is no time for it anywhere in this
            # utterance, and one grid step of its own would be invented rather than measured, so the
            # sentence is dropped and recorded instead of being given a made-up time.
            dropped += 1
            continue
        clamped.append(TimedSentence(sentence.text, sentence.char_start, sentence.char_end, start, end))
    if not clamped:
        return [], trimmed, dropped

    # A start may move forward onto the previous cue and an end may take the room that is left,
    # because those are the only values the audio permits; nothing is re-spread over the gap.
    ordered: list[TimedSentence] = []
    cursor = 0.0
    for sentence in clamped:
        start = max(0.0, min(sentence.start, duration - minimum), cursor)
        end = max(min(sentence.end, duration), start + minimum)
        if end <= start:
            continue
        ordered.append(sentence if (start, end) == (sentence.start, sentence.end) else TimedSentence(sentence.text, sentence.char_start, sentence.char_end, start, end))
        cursor = end
    return ordered, trimmed, dropped


def fit_to_audio(sentences: list[TimedSentence], duration: float, *, minimum: float = MIN_SENTENCE_DURATION_SECONDS) -> tuple[list[TimedSentence], float, int]:
    """Kept for the callers and tests that know this pass under its earlier name."""
    return clamp_to_audio(sentences, duration, minimum=minimum)


def recognize_segment(models, translator, segment: Segment, source_lang: str, writer, progress: Callable[[Segment, str], None] | None = None, translate: bool = True, cancelled: threading.Event | None = None) -> SegmentResult:
    """ASR, sentence splitting, forced alignment and translation of one utterance.

    This is the whole per-utterance chain without any of the loop or queueing around it, so
    offline mode can run it on a worker while the next utterance is still being decoded.

    ``cancelled`` is checked between sentences. A stop request therefore ends the task after
    the sentence in flight instead of after the whole utterance, and the remaining sentences
    of that utterance plus every later utterance are dropped, so the SRT stays a clean prefix
    of the complete run instead of having a hole in the middle.
    """
    result = SegmentResult(segment=segment, started=time.monotonic())
    pcm = segment.pcm
    if progress is not None:
        progress(segment, "asr")
    original, language = models.transcribe(pcm, source_lang)
    result.language = language
    text = original.strip()
    result.source = text
    if not text:
        writer.diagnostic(stage="asr", status="empty_transcript", start_sample=segment.start_sample, duration_seconds=round(segment.duration, 3))
        result.empty_transcript = True
        result.finished = time.monotonic()
        return result
    if progress is not None:
        progress(segment, "sat")
    sentences = models.split(text)
    if progress is not None:
        progress(segment, "aligner")
    aligned = models.align(pcm, text, language)
    if progress is not None:
        progress(segment, "sentence_mapping")
    mapped = build_sentences(text, sentences, aligned, lambda value: models.alignment_units(value, language))
    alignment_end = max(sentence.end for sentence in mapped)
    mapped, trimmed, dropped = clamp_to_audio(mapped, segment.duration)
    if not mapped and segment.duration > 0:
        # The aligner occasionally puts a whole short utterance on a single timestamp, or puts its
        # complete range outside the captured PCM. Prefer one coarse cue covering the real audio
        # over stopping the entire file after hundreds of otherwise valid subtitles.
        mapped = [TimedSentence(text, 0, len(text), 0.0, segment.duration)]
        writer.diagnostic(
            stage="sentence_mapping",
            status="alignment_fallback",
            reason="no positive alignment duration inside utterance audio",
            start_sample=segment.start_sample,
            duration_seconds=round(segment.duration, 3),
            alignment_end_seconds=round(alignment_end, 3),
            trimmed_seconds=round(trimmed, 3),
            untimed_sentences=dropped,
            grid_seconds=ALIGNMENT_GRID_SECONDS,
            sentences=len(mapped),
            asr_text=text[:100],
        )
    elif trimmed or dropped:
        # Alignment outside the PCM is always pulled back to the real audio boundary. This is
        # deliberately permissive: diagnostics retain the discrepancy, while the task continues.
        writer.diagnostic(
            stage="sentence_mapping",
            status="alignment_clamped",
            start_sample=segment.start_sample,
            duration_seconds=round(segment.duration, 3),
            alignment_end_seconds=round(alignment_end, 3),
            trimmed_seconds=round(trimmed, 3),
            untimed_sentences=dropped,
            grid_seconds=ALIGNMENT_GRID_SECONDS,
            sentences=len(mapped),
        )
    for sentence in mapped:
        result.sentences.append(Translated(sentence, segment.start_sample))
    if translate:
        keep = 0
        for item in result.sentences:
            if cancelled is not None and cancelled.is_set():
                break
            if progress is not None:
                progress(segment, "translation")
            item.translation = translator.translate(item.sentence.text)
            keep += 1
        if keep < len(result.sentences):
            result.sentences = result.sentences[:keep]
            result.truncated = True
    result.finished = time.monotonic()
    return result


class Segmenter:
    """Silero VAD candidate pauses plus Smart Turn commits, producing whole utterances."""

    def __init__(self, models):
        self.models = models
        self.history = collections.deque(maxlen=4)
        self.start_sample: int | None = None
        self.utterance = bytearray()
        self.last_capture = 0.0
        self.position = 0
        self.next_sample = 0

    def reset(self) -> None:
        self.start_sample = None
        self.utterance.clear()
        self.history.clear()

    def _check_length(self):
        duration = len(self.utterance) / (2 * SAMPLE_RATE)
        if duration >= ALIGNER_LIMIT_SECONDS:
            raise PipelineFailure("utterance_too_long", f"utterance_too_long: {duration:.3f} seconds (forced aligner limit {ALIGNER_LIMIT_SECONDS} seconds)")

    def feed(self, block: PCMBlock, backlog_seconds: float = 0.0, max_segment_seconds: float | None = None) -> Segment | None:
        if block.start_sample != self.next_sample or len(block.data) % 2:
            raise PipelineFailure("audio", "discontinuous or invalid PCM block")
        self.next_sample += block.samples
        event = self.models.vad_event(block.data) if block.samples == BLOCK_SAMPLES else None
        if self.start_sample is None:
            self.history.append(block)
            if event and "start" in event:
                start = int(event["start"])
                pieces = []
                for earlier in self.history:
                    offset = max(0, start - earlier.start_sample)
                    if offset < earlier.samples:
                        pieces.append(earlier.data[offset * 2:])
                if not pieces:
                    raise PipelineFailure("vad", "speech start unavailable in PCM history")
                self.start_sample = max(start, self.history[0].start_sample)
                self.utterance = bytearray(b"".join(pieces))
                self.history.clear()
                self.last_capture = block.captured_at
                self._check_length()
            return None
        self.utterance.extend(block.data)
        self.last_capture = block.captured_at
        self._check_length()
        if max_segment_seconds is not None and len(self.utterance) / (2 * SAMPLE_RATE) >= max_segment_seconds:
            # Only cut where the speaker actually paused, so no word is split in half.
            if event and "end" in event:
                return self._take(backlog_seconds)
            return None
        if event and "end" in event:
            if self.models.turn_complete(bytes(self.utterance)):
                return self._take(backlog_seconds)
        return None

    def finish(self, backlog_seconds: float = 0.0) -> Segment | None:
        """Flush a trailing utterance the speaker never paused after."""
        if self.start_sample is None:
            return None
        if self.models.turn_complete(bytes(self.utterance)):
            return self._take(backlog_seconds)
        self.reset()
        return None

    def _take(self, backlog_seconds: float) -> Segment:
        self.position += 1
        segment = Segment(bytes(self.utterance), self.start_sample, self.position, backlog_seconds)
        self.reset()
        return segment


class Pipeline:
    """Live mode: every committed utterance is processed inline as it arrives."""

    def __init__(self, models, translator, writer, source_lang: str, model_name: str, device: str, on_status: Callable[[str], None] | None = None, progress: Callable[[Segment, str], None] | None = None):
        self.models = models
        self.translator = translator
        self.writer = writer
        self.source_lang = source_lang
        self.model_name = model_name
        self.device = device
        self.segmenter = Segmenter(models)
        self.stage = "audio"
        self.on_status = on_status
        self.progress = progress
        self.latencies = []
        self.max_backlog = 0.0

    @property
    def start_sample(self):
        return self.segmenter.start_sample

    @start_sample.setter
    def start_sample(self, value):
        self.segmenter.start_sample = value

    @property
    def utterance(self):
        return self.segmenter.utterance

    @utterance.setter
    def utterance(self, value):
        self.segmenter.utterance = value

    def _commit(self, backlog_seconds: float) -> None:
        """Commit whatever the segmenter currently holds, as Smart Turn would."""
        self.write(self.segmenter._take(backlog_seconds))

    def set_stage(self, stage: str) -> None:
        if self.stage != stage:
            self.stage = stage
            if self.on_status is not None:
                self.on_status(stage)

    def feed(self, block: PCMBlock, backlog_seconds: float = 0.0):
        self.max_backlog = max(self.max_backlog, backlog_seconds)
        self.set_stage("vad")
        segment = self.segmenter.feed(block, backlog_seconds)
        if segment is not None:
            self.write(segment)

    def finish(self, backlog_seconds: float = 0.0):
        self.set_stage("smart_turn")
        segment = self.segmenter.finish(backlog_seconds)
        if segment is not None:
            self.write(segment)
        elif self.segmenter.start_sample is not None:
            self.writer.diagnostic(stage="smart_turn", status="incomplete_residual", start_sample=self.segmenter.start_sample, duration_seconds=len(self.segmenter.utterance) / (2 * SAMPLE_RATE))
            self.segmenter.reset()

    def write(self, segment: Segment) -> None:
        """Run the chain on one utterance and append its sentences, in order."""
        started = time.monotonic()
        try:
            if self.on_status is not None:
                self.on_status("smart_turn")

            def report(_segment: Segment, stage: str) -> None:
                self.set_stage(stage)

            result = recognize_segment(self.models, self.translator, segment, self.source_lang, self.writer, report)
            for item in result.sentences:
                self.set_stage("subtitle_write")
                self.writer.append(
                    media_time(segment.start_sample, item.sentence.start),
                    media_time(segment.start_sample, item.sentence.end),
                    item.translation or "",
                    {
                        "source_language": result.language,
                        "original": item.sentence.text,
                        "mt_model": self.model_name,
                        "mt_device": self.device,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "backlog_seconds": round(segment.backlog_seconds, 3),
                    },
                )
            if self.segmenter.last_capture:
                self.latencies.append(time.monotonic() - self.segmenter.last_capture)
        except PipelineFailure:
            raise
        except Exception as exc:
            raise PipelineFailure(self.stage, str(exc)) from exc


class FastOfflinePipeline:
    """Offline mode: decode, recognise and translate at the same time.

    A task that already has the whole file does not have to wait for anything except its own
    slowest stage, so recognition and translation run on workers while the next utterances
    are still being decoded, and llama-server answers several translation requests at once
    through its parallel slots. Subtitle numbers still come out in media order, because only
    the writing step is serialised.

    ``slots`` is how many utterances may be in the chain at once (each one occupies a
    llama-server slot while it is being translated); ``asr_workers`` is how many of them may
    recognise at the same time. One is the default there, because the ASR model already uses
    the whole GPU for one utterance and a second concurrent stream only makes both slower.
    """

    def __init__(self, models, translator, writer, source_lang: str, model_name: str, device: str, *, slots: int = 2, asr_workers: int = 1, max_segment_seconds: float = MAX_SEGMENT_SECONDS, on_status: Callable[[str], None] | None = None, progress: Callable[[Segment, str], None] | None = None, stop: threading.Event | None = None):
        self.models = models
        self.translator = translator
        self.writer = writer
        self.source_lang = source_lang
        self.model_name = model_name
        self.device = device
        self.slots = max(1, min(4, slots))
        self.asr_workers = max(1, min(self.slots, asr_workers))
        self.max_segment_seconds = max_segment_seconds
        self.on_status = on_status
        self.progress = progress
        self.stop = stop
        self.segments: queue.Queue[Segment | None] = queue.Queue(maxsize=max(2, self.slots))
        self.pending: dict[int, SegmentResult] = {}
        self.skipped: set[int] = set()
        self.errors: list[BaseException] = []
        self.decoded_seconds = 0.0
        self.next_to_write = 1
        self.processed_seconds = 0.0
        self.started = time.monotonic()
        self.finished = 0
        self.alignment_workers = 0
        self.slot_semaphore = threading.Semaphore(self.slots)
        self.workers: list[threading.Thread] = []
        self.stage = "audio"
        self.latencies: list[float] = []
        self.peak_backlog = 0.0
        self._lock = threading.Lock()
        self._truncated = False

    def stopped(self) -> bool:
        return self.stop is not None and self.stop.is_set()

    # ---- worker side -------------------------------------------------------------

    def _worker(self, index: int) -> None:
        while True:
            try:
                segment = self.segments.get()
            except Exception:  # pragma: no cover - queue.get never raises here
                return
            try:
                if segment is None:
                    return
                if self.stopped() or self._truncated or self.errors:
                    # A stop means the file is a prefix of the complete output: everything
                    # from here on is dropped, so the numbering never has a hole.
                    with self._lock:
                        self.skipped.add(segment.position)
                    continue
                try:
                    # The slot is held across translation only; recognition has its own limit.
                    if self.progress is not None:
                        self.progress(segment, "asr")
                    words = self._recognize(segment)
                    with self.slot_semaphore:
                        self._translate(words)
                except BaseException as exc:
                    self.errors.append(exc)
                    with self._lock:
                        self.skipped.add(segment.position)
                    continue
                with self._lock:
                    self.pending[segment.position] = words
                    self.processed_seconds += segment.duration
            finally:
                self.segments.task_done()

    def _recognize(self, segment: Segment) -> SegmentResult:
        while True:
            with self._lock:
                busy = self.alignment_workers
            if not self.stopped() and busy < self.asr_workers:
                break
            time.sleep(0.01)
        with self._lock:
            self.alignment_workers += 1
        try:
            return recognize_segment(self.models, self.translator, segment, self.source_lang, self.writer, self.progress, translate=False, cancelled=self.stop)
        finally:
            with self._lock:
                self.alignment_workers -= 1

    def _translate(self, result: SegmentResult) -> None:
        keep = 0
        for item in result.sentences:
            if self.stopped() or self._truncated:
                break
            if self.progress is not None:
                self.progress(result.segment, "translation")
            item.translation = self.translator.translate(item.sentence.text)
            keep += 1
        if keep < len(result.sentences):
            result.sentences = result.sentences[:keep]
            result.truncated = True
            self._truncated = True

    # ---- consumer side -----------------------------------------------------------

    def start(self) -> None:
        self.workers = [threading.Thread(target=self._worker, args=(index,), name=f"livesub-offline-{index}", daemon=True) for index in range(self.slots)]
        for worker in self.workers:
            worker.start()

    def submit(self, segment: Segment) -> None:
        if self.errors:
            raise self.errors[0]
        self.segments.put(segment)

    def has_work(self) -> bool:
        """True while a submitted utterance has not reached the pending table yet."""
        with self._lock:
            in_flight = self.alignment_workers + len(self.pending) + len(self.skipped)
        return self.segments.unfinished_tasks > 0 or in_flight > 0

    def drain(self, wait_seconds: float = 3600.0) -> None:
        for _ in self.workers:
            self.segments.put(None)
        deadline = time.monotonic() + wait_seconds
        for worker in self.workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        alive = [worker for worker in self.workers if worker.is_alive()]
        self.flush(force=True)
        if self.errors:
            raise self.errors[0]
        if alive:
            raise PipelineFailure("cleanup", "识别线程未在期限内结束")

    def flush(self, force: bool = False) -> int:
        """Append every finished utterance that follows the ones already written."""
        written = 0
        while True:
            position = self.next_to_write
            result = self.pending.pop(position, None)
            if result is None:
                if position in self.skipped:
                    self.skipped.discard(position)
                    self.next_to_write += 1
                    continue
                break
            self._append(result)
            self.next_to_write += 1
            written += 1
        if force and (self.pending or self.skipped):
            missing = sorted(self.pending | self.skipped)
            self.pending.clear()
            self.skipped.clear()
            raise PipelineFailure("cleanup", f"字幕序号不连续，缺少第 {missing[0]} 段")
        return written

    def _append(self, result: SegmentResult) -> None:
        segment = result.segment
        for item in result.sentences:
            self.writer.append(
                media_time(segment.start_sample, item.sentence.start),
                media_time(segment.start_sample, item.sentence.end),
                item.translation or "",
                {
                    "source_language": result.language,
                    "original": item.sentence.text,
                    "mt_model": self.model_name,
                    "mt_device": self.device,
                    "elapsed_seconds": round(result.finished - result.started, 3),
                    "backlog_seconds": round(segment.backlog_seconds, 3),
                },
            )
        self.finished += 1

    # ---- progress ----------------------------------------------------------------

    def estimate(self, total_seconds: float | None) -> dict:
        """Real measured numbers only: decoded audio, processed audio and the rate so far.

        The remaining time is the measured throughput extrapolated to the audio that is left;
        nothing here is a guessed percentage.
        """
        with self._lock:
            processed = self.processed_seconds
        elapsed = time.monotonic() - self.started
        state = {
            # "source_seconds" is the length of the file; "decoded_seconds" is how much of it
            # the decoder has already read. Both are measurements and they are not the same.
            "processed_seconds": round(processed, 1),
            "segments_done": self.finished,
            "segments_open": len(self.pending),
        }
        if total_seconds:
            state["total_seconds"] = round(total_seconds, 1)
        if processed > 0 and elapsed > 1:
            speed = processed / elapsed
            state["speed"] = round(speed, 3)
            if total_seconds:
                state["eta_seconds"] = round(max(0.0, total_seconds - processed) / speed, 1)
        return state
