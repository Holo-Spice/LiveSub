import threading
import time
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from subtitle_cli.audio import BLOCK_SAMPLES, CaptureFormat, FFmpegInput, PCMBlock, PCMQueue, SAMPLE_RATE, capture_filter, device_capture_format, media_time
from subtitle_cli.cli import StartupCancelled, arguments, configured_paths, run_live, run_task, startup_step
from subtitle_cli.pipeline import Pipeline, PipelineFailure, map_sentences
from subtitle_cli.models import configure_convolution_backend
from subtitle_cli.subtitles import SubtitleWriter
from subtitle_cli import ui_bridge
from subtitle_cli.translator import Translator, llama_args


def units(text):
    # Small stand-in for Qwen's default path: whitespace splits, punctuation drops.
    result = []
    current = ""
    for char in text:
        if char.isspace():
            if current:
                result.append(current)
                current = ""
        elif char.isalnum() or char == "'":
            current += char
    if current:
        result.append(current)
    return result


def aligned(words):
    return [{"text": word, "start_time": index * 0.5, "end_time": (index + 1) * 0.5} for index, word in enumerate(words)]


def test_sentence_mapping_repeated_words_and_whitespace():
    text = "hello hello. hello\nworld"
    sentences = ["hello hello. ", "hello\nworld"]
    result = map_sentences(text, sentences, aligned(units(text)), units)
    assert [(part.char_start, part.char_end, part.start, part.end) for part in result] == [(0, 13, 0, 1), (13, len(text), 1, 2)]


def test_sentence_mapping_japanese_and_punctuation():
    text = "今日は。明日も。"
    sentences = ["今日は。", "明日も。"]
    japanese_units = lambda value: [char for char in value if char not in "。"]
    result = map_sentences(text, sentences, aligned(japanese_units(text)), japanese_units)
    assert [part.text for part in result] == sentences
    assert result[1].start == 1.5


def test_sentence_mapping_uses_whole_text_japanese_units():
    text = "そうです。なるほど。"
    sentences = ["そうです。", "なるほど。"]
    calls = []

    def context_sensitive_units(value):
        calls.append(value)
        return ["そう", "です", "なる", "ほど"]

    result = map_sentences(text, sentences, aligned(["そう", "です", "なる", "ほど"]), context_sensitive_units)
    assert calls == [text]
    assert [(part.text, part.start, part.end) for part in result] == [
        (sentences[0], 0, 1),
        (sentences[1], 1, 2),
    ]


def test_sentence_mapping_merges_boundary_inside_unit_and_empty_fragment():
    result = map_sentences("あ。い", ["あ。", "い"], aligned(["あい"]), lambda _: ["あい"])
    assert [(part.text, part.char_start, part.char_end, part.start, part.end) for part in result] == [
        ("あ。い", 0, 3, 0, 0.5)
    ]
    result = map_sentences("。そうです。", ["。", "そうです。"], aligned(["そう", "です"]), lambda _: ["そう", "です"])
    assert [part.text for part in result] == ["。そうです。"]


def test_sentence_mapping_allows_zero_length_word_inside_valid_sentence():
    items = [{"text": "Oh", "start_time": 0.8, "end_time": 0.8}, {"text": "yeah", "start_time": 0.8, "end_time": 1.12}]
    result = map_sentences("Oh yeah", ["Oh yeah"], items, units)
    assert (result[0].start, result[0].end) == (0.8, 1.12)
    collapsed = map_sentences("Oh", ["Oh"], items[:1], units)
    assert (collapsed[0].start, collapsed[0].end) == (0.8, 0.8)


@pytest.mark.parametrize("sentences,items", [(["a", "c"], aligned(["a", "b"])), (["a ", "b"], aligned(["a", "x"])), (["a ", "b"], [{"text": "a", "start_time": 1, "end_time": 2}, {"text": "b", "start_time": 1.5, "end_time": 3}])])
def test_sentence_mapping_rejects_mismatch(sentences, items):
    with pytest.raises(PipelineFailure) as error:
        map_sentences("a b", sentences, items, units)
    assert error.value.stage == "sentence_mapping"


@pytest.fixture
def run_dir():
    """A scratch directory in the first place the process may actually create files.

    `tmp_path` is unusable in this sandbox and a sandbox policy can also refuse a directory
    created directly in the checkout, so the writable parent is discovered first and only the
    files of this test are cleaned up afterwards.
    """
    probe = Path(tempfile.mkdtemp(prefix="livesub-test-"))
    try:
        (probe / ".writable").write_text("", encoding="utf-8")
    except OSError:
        shutil.rmtree(probe, ignore_errors=True)
        probe = Path(tempfile.mkdtemp(prefix="livesub-test-", dir=Path.cwd()))
    try:
        yield probe
    finally:
        shutil.rmtree(probe, ignore_errors=True)


class StubModels:
    """The model surface `recognize_segment` uses, with the transcript and alignment fixed."""

    def __init__(self, text, sentences, aligned):
        self.text = text
        self.sentences = sentences
        self.aligned = aligned

    def transcribe(self, _pcm, _source_lang):
        return self.text, "Japanese"

    def split(self, _text):
        return list(self.sentences)

    def align(self, _pcm, _text, _language):
        return [dict(item) for item in self.aligned]

    def alignment_units(self, text, _language):
        return units(text)


class StubTranslator:
    def translate(self, text):
        return f"[{text}]"


def write_segment(directory, name, pcm, text, sentences, aligned):
    """Run one utterance through the real chain and return the SRT text and diagnostics."""
    output = directory / f"{name}.srt"
    with SubtitleWriter(output) as writer:
        pipeline = Pipeline(StubModels(text, sentences, aligned), StubTranslator(), writer, "ja", "7b", "gpu")
        pipeline.start_sample = 0
        pipeline.utterance = bytearray(pcm)
        pipeline._commit(0.0)
    return output.read_text(encoding="utf-8"), [json.loads(line) for line in output.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()]


def test_alignment_overshoot_is_clamped_and_the_task_survives(run_dir):
    """The reported failure: the aligner answers on an 80 ms grid, so a boundary sentence can end
    one grid step past the utterance (`3.440s` over `3.422s`). That is quantisation, not a wrong
    subtitle, so the cue is pulled onto the audio instead of ending the whole run."""
    pcm = bytes(2 * 16_000)  # 1.000 s of captured speech
    srt, records = write_segment(run_dir, "clamped", pcm, "あい", ["あい"], [{"text": "あい", "start_time": 0.0, "end_time": 1.018}])
    assert "00:00:00,000 --> 00:00:01,000\n[あい]" in srt
    clamped = [record for record in records if record.get("status") == "alignment_clamped"]
    assert len(clamped) == 1
    assert clamped[0]["alignment_end_seconds"] == 1.018 and clamped[0]["duration_seconds"] == 1.0
    assert clamped[0]["trimmed_seconds"] == 0.018 and clamped[0]["sentences"] == 1
    assert not [record for record in records if record.get("status") == "alignment_dropped"]


def test_a_short_utterance_still_gets_its_clamped_subtitle(run_dir):
    """512 samples is one VAD block: the clamp leaves 32 ms of cue, and anything at all that is
    left must not be thrown away."""
    srt, records = write_segment(run_dir, "short", bytes(1024), "あ", ["あ"], [{"text": "あ", "start_time": 0.0, "end_time": 0.05}])
    assert "00:00:00,000 --> 00:00:00,032\n[あ]" in srt
    assert [record["status"] for record in records if record.get("status") == "alignment_clamped"] == ["alignment_clamped"]


def text_of(srt):
    """Just the cue times and the text: the stub translation keeps the sentence's own space."""
    lines = [line for line in srt.splitlines() if line and not line.isdigit()]
    return " | ".join(lines)


def test_a_tail_sentence_shorter_than_the_aligner_grid_is_dropped_not_fatal(run_dir):
    """The reported failure: every word of the last sentence lands on one timestamp, so the mapping
    saw `sentence has no positive alignment duration` and stopped the task after 96 subtitles. That
    sentence has no time anywhere in the audio, so it is dropped and recorded — never given a time
    that was not measured, and never allowed to end the run."""
    aligned = [
        {"text": "あ", "start_time": 0.0, "end_time": 0.5},
        {"text": "い", "start_time": 0.5, "end_time": 1.0},
        {"text": "う", "start_time": 1.0, "end_time": 1.0},
    ]
    srt, records = write_segment(run_dir, "tail", bytes(2 * 16_000), "あ い う", ["あ ", "い ", "う"], aligned)
    assert text_of(srt) == "00:00:00,000 --> 00:00:00,500 | [あ ] | 00:00:00,500 --> 00:00:01,000 | [い ]"
    clamped = [record for record in records if record.get("status") == "alignment_clamped"]
    assert clamped and clamped[0]["untimed_sentences"] == 1 and clamped[0]["sentences"] == 2


def test_an_utterance_without_a_single_timed_sentence_uses_the_audio_range(run_dir):
    """A completely collapsed short utterance gets one coarse cue and does not stop the task."""
    srt, records = write_segment(run_dir, "untimed", bytes(2 * 16_000), "あ", ["あ"], [{"text": "あ", "start_time": 0.5, "end_time": 0.5}])
    assert "00:00:00,000 --> 00:00:01,000\n[あ]" in srt
    assert [record["status"] for record in records if record.get("status") == "alignment_fallback"] == ["alignment_fallback"]


def test_a_collapsed_sentence_does_not_overwrite_the_measured_ones(run_dir):
    """The sentences the aligner did time keep exactly the times it gave them, in order; the one
    without any time is dropped instead of being written over the gap between them."""
    aligned = [
        {"text": "あ", "start_time": 0.0, "end_time": 0.5},
        {"text": "い", "start_time": 0.5, "end_time": 1.0},
        {"text": "う", "start_time": 0.5, "end_time": 0.5},
    ]
    srt, records = write_segment(run_dir, "mixed", bytes(3 * 16_000), "あ い う", ["あ ", "い ", "う"], aligned)
    assert text_of(srt) == "00:00:00,000 --> 00:00:00,500 | [あ ] | 00:00:00,500 --> 00:00:01,000 | [い ]"
    clamped = [record for record in records if record.get("status") == "alignment_clamped"]
    assert clamped and clamped[0]["untimed_sentences"] == 1 and clamped[0]["sentences"] == 2


def test_an_utterance_with_only_untimed_sentences_uses_one_coarse_cue(run_dir):
    """Multiple collapsed sentences are retained as one cue covering the captured utterance."""
    aligned = [
        {"text": "あ", "start_time": 0.0, "end_time": 0.0},
        {"text": "い", "start_time": 0.0, "end_time": 0.0},
    ]
    srt, records = write_segment(run_dir, "untimed-all", bytes(2 * 16_000), "あ い", ["あ ", "い"], aligned)
    assert "00:00:00,000 --> 00:00:01,000\n[あ い]" in srt
    assert [record["status"] for record in records if record.get("status") == "alignment_fallback"] == ["alignment_fallback"]


def test_a_large_alignment_overshoot_is_clamped_instead_of_stopping(run_dir):
    """Even a large discrepancy is retained in diagnostics while subtitle generation continues."""
    srt, records = write_segment(run_dir, "mismatch", bytes(2 * 16_000), "あい", ["あい"], [{"text": "あい", "start_time": 0.0, "end_time": 1.4}])
    assert "00:00:00,000 --> 00:00:01,000\n[あい]" in srt
    clamped = [record for record in records if record.get("status") == "alignment_clamped"]
    assert clamped and clamped[0]["trimmed_seconds"] == 0.4


def test_sample_time_and_srt(tmp_path):
    assert media_time(32_000, 0.1234) == pytest.approx(2.1234)
    path = tmp_path / "sub.srt"
    with SubtitleWriter(path) as writer:
        writer.append(2.1234, 3.1234, "你好", {"source_language": "English"})
        writer.append(3.1234, 4.5, "再见", {"source_language": "English"})
        with pytest.raises(ValueError):
            writer.append(4.0, 5.0, "错误", {})
    assert "1\n00:00:02,123 --> 00:00:03,124\n你好\n\n2\n" in path.read_text(encoding="utf-8")
    assert len(path.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_output_files_are_never_overwritten(tmp_path):
    output = tmp_path / "existing.srt"
    output.write_text("原有字幕", encoding="utf-8")
    with pytest.raises(FileExistsError):
        SubtitleWriter(output)
    assert output.read_text(encoding="utf-8") == "原有字幕"

    output = tmp_path / "existing-jsonl.srt"
    diagnostic = output.with_suffix(".jsonl")
    diagnostic.write_text("原有诊断", encoding="utf-8")
    with pytest.raises(FileExistsError):
        SubtitleWriter(output)
    assert not output.exists()
    assert diagnostic.read_text(encoding="utf-8") == "原有诊断"


def test_ui_bridge_emits_complete_saved_subtitle_and_one_finish(monkeypatch, tmp_path):
    def fake_task(args, root, *, stop_requested, on_status, on_capture, on_subtitle, on_detail, poll, on_progress, on_listening):
        on_status("asr")
        on_subtitle(1, 100, 1300, "你好")
        return "stopped"

    monkeypatch.setattr(ui_bridge, "run_task", fake_task)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"command":"stop"}\n'))
    code = ui_bridge.main(["offline", "--input", str(tmp_path / "视频.mkv"), "--output", str(tmp_path / "out.srt")])
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert code == 0
    assert [message["type"] for message in messages] == ["status", "subtitle", "finished"]
    assert messages[1] == {"type": "subtitle", "index": 1, "start_ms": 100, "end_ms": 1300, "text": "你好"}
    assert messages[2]["result"] == "stopped"


def test_ui_bridge_forwards_each_model_load_item(monkeypatch):
    """The UI progress line is driven by these two messages: a start without seconds and a
    completion with them. Without them a 200 second load is one frozen stage name."""
    def fake_task(args, root, *, stop_requested, on_status, on_capture, on_subtitle, on_detail, poll, on_progress, on_listening):
        on_status("model_deps")
        on_detail("wtpsplit", None)
        on_detail("wtpsplit", 5.031)
        return "completed"

    monkeypatch.setattr(ui_bridge, "run_task", fake_task)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    ui_bridge.main(["offline", "--input", "video.mkv", "--output", "out.srt"])
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages[0] == {"type": "status", "stage": "model_deps"}
    assert messages[1] == {"type": "status", "detail": "wtpsplit", "seconds": None}
    assert messages[2] == {"type": "status", "detail": "wtpsplit", "seconds": 5.031}


def test_ui_bridge_forwards_measured_progress_and_the_listening_wait(monkeypatch):
    """Offline progress is a measurement, so it travels as its own message instead of being
    squeezed into a stage name; the live wait for the first speech is its own message too."""
    def fake_task(args, root, *, stop_requested, on_status, on_capture, on_subtitle, on_detail, poll, on_progress, on_listening):
        on_listening({"waited_seconds": 5.0, "timeout_seconds": 300.0, "remaining_seconds": 295.0})
        on_status("listening")
        on_progress({"total_seconds": 600.0, "decoded_seconds": 300.0, "processed_seconds": 240.0, "eta_seconds": 180.0, "speed": 4.0})
        return "completed"

    monkeypatch.setattr(ui_bridge, "run_task", fake_task)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    ui_bridge.main(["offline", "--input", "video.mkv", "--output", "out.srt"])
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages[0] == {"type": "listening", "waited_seconds": 5.0, "timeout_seconds": 300.0, "remaining_seconds": 295.0}
    # "listening" is a wait, not a pipeline stage, so it must not leak into the stage line.
    assert [message["type"] for message in messages] == ["listening", "progress", "finished"]
    assert messages[1]["eta_seconds"] == 180.0 and messages[1]["processed_seconds"] == 240.0


def test_ui_bridge_reports_a_model_load_stopped_mid_import(monkeypatch):
    """Stop during the load: the load thread cannot be interrupted, so the bridge answers
    with `stopped` and leaves without finalising the interpreter."""
    exits = []

    def fake_task(args, root, *, stop_requested, on_status, on_capture, on_subtitle, on_detail, poll, on_progress, on_listening):
        raise StartupCancelled("transformers")

    monkeypatch.setattr(ui_bridge, "run_task", fake_task)
    monkeypatch.setattr(ui_bridge.os, "_exit", lambda code: exits.append(code))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    ui_bridge.main(["offline", "--input", "video.mkv", "--output", "out.srt"])
    message = json.loads(output.getvalue().splitlines()[-1])
    assert message["result"] == "stopped"
    assert "transformers" in message["message"]
    assert exits == [0]


def test_ui_bridge_stdin_eof_does_not_stop_the_task(monkeypatch):
    seen = []

    def fake_task(args, root, *, stop_requested, on_status, on_capture, on_subtitle, on_detail, poll, on_progress, on_listening):
        time.sleep(0.05)
        seen.append(stop_requested.is_set())
        return "stopped"

    monkeypatch.setattr(ui_bridge, "run_task", fake_task)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code = ui_bridge.main(["offline", "--input", "视频.mkv", "--output", "out.srt"])
    assert code == 0
    assert seen == [False]
    assert json.loads(output.getvalue().splitlines()[-1])["result"] == "stopped"


def test_ui_bridge_accepts_a_bom_before_the_stop_command(monkeypatch):
    seen = []

    def fake_task(args, root, *, stop_requested, on_status, on_capture, on_subtitle, on_detail, poll, on_progress, on_listening):
        deadline = time.monotonic() + 1.0
        while not stop_requested.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        seen.append(stop_requested.is_set())
        return "stopped"

    monkeypatch.setattr(ui_bridge, "run_task", fake_task)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stdin", io.StringIO('\ufeff{"command":"stop"}\n'))
    code = ui_bridge.main(["offline", "--input", "video.mkv", "--output", "out.srt"])
    assert code == 0
    assert seen == [True]


def test_miopen_convolution_path_is_disabled_unless_requested(monkeypatch):
    def fake_torch():
        cudnn = type("Cudnn", (), {"enabled": True})()
        return type("Torch", (), {"backends": type("Backends", (), {"cudnn": cudnn})()})()

    monkeypatch.delenv("LIVESUB_ENABLE_MIOPEN", raising=False)
    default = fake_torch()
    assert configure_convolution_backend(default) is False
    assert default.backends.cudnn.enabled is False

    monkeypatch.setenv("LIVESUB_ENABLE_MIOPEN", "1")
    kept = fake_torch()
    assert configure_convolution_backend(kept) is True
    assert kept.backends.cudnn.enabled is True


def test_startup_step_returns_value_and_propagates_failure():
    assert startup_step("model_load", lambda: "models") == "models"
    with pytest.raises(PipelineFailure) as error:
        startup_step("model_load", lambda: (_ for _ in ()).throw(PipelineFailure("model_load", "hip error")))
    assert error.value.stage == "model_load"
    assert str(error.value) == "hip error"


def test_startup_step_bounds_a_stalled_load(monkeypatch):
    monkeypatch.setattr("subtitle_cli.cli.STARTUP_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(PipelineFailure) as error:
        startup_step("model_load", lambda: time.sleep(30))
    assert error.value.stage == "model_load"
    assert "stalled" in str(error.value)


def test_startup_step_names_the_item_a_stalled_load_stopped_on(monkeypatch):
    monkeypatch.setattr("subtitle_cli.cli.STARTUP_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(PipelineFailure) as error:
        startup_step("model_load", lambda: time.sleep(30), describe=lambda: "transformers")
    assert "transformers" in str(error.value)


def test_startup_step_cancel_abandons_a_stalled_load():
    """A user stop must not wait for a load that is already inside a native call, and must
    not end in the launcher killing the process tree in the middle of the HIP teardown."""
    cancel = threading.Event()
    threading.Timer(0.05, cancel.set).start()
    with pytest.raises(StartupCancelled):
        startup_step("model_load", lambda: time.sleep(30), cancel=cancel)


def test_run_task_stops_during_the_model_load_without_starting_the_translator(monkeypatch, tmp_path):
    entries = {key: key for key in ("ffmpeg", "llama_server", "llama_runtime_bin", "qwen_asr", "qwen_aligner", "smart_turn", "sat", "sat_tokenizer", "hymt_7b", "hymt_1_8b")}
    (tmp_path / "config.toml").write_text("[paths]\n" + "".join(f'{key} = "{value}"\n' for key, value in entries.items()), encoding="utf-8")
    for key in entries:
        (tmp_path / key).touch()
    output = tmp_path / "stopped.srt"
    args = arguments(["live", "--audio-device", "loopback", "--source-lang", "ja", "--output", str(output)])
    stop = threading.Event()
    calls = []

    monkeypatch.setattr("subtitle_cli.cli.preflight", lambda paths, device: None)

    class FakeModels:
        def __init__(self, paths, on_progress=None, on_detail=None, hotwords=""):
            calls.append("models")
            stop.set()  # the user presses 停止 while the models are still loading

    monkeypatch.setattr("subtitle_cli.cli.Models", FakeModels)
    monkeypatch.setattr("subtitle_cli.cli.Translator", lambda *args, **kwargs: calls.append("translator"))
    assert run_task(args, tmp_path, stop_requested=stop) == "stopped"
    assert calls == ["models"]
    records = [json.loads(line) for line in output.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()]
    assert records[0]["stage"] == "startup"
    assert [record["stage"] for record in records if record["stage"] == "startup_host"] == ["startup_host"]


def test_startup_progress_is_recorded_before_any_model_is_loaded(monkeypatch, tmp_path):
    entries = {key: key for key in ("ffmpeg", "llama_server", "llama_runtime_bin", "qwen_asr", "qwen_aligner", "smart_turn", "sat", "sat_tokenizer", "hymt_7b", "hymt_1_8b")}
    (tmp_path / "config.toml").write_text("[paths]\n" + "".join(f'{key} = "{value}"\n' for key, value in entries.items()), encoding="utf-8")
    for key in entries:
        (tmp_path / key).touch()
    output = tmp_path / "stalled.srt"
    args = arguments(["live", "--audio-device", "loopback", "--source-lang", "ja", "--output", str(output)])

    def stalled(paths, device):
        raise PipelineFailure("preflight", "synthetic stop")

    monkeypatch.setattr("subtitle_cli.cli.preflight", stalled)
    with pytest.raises(PipelineFailure):
        run_task(args, tmp_path)
    records = [json.loads(line) for line in output.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()]
    assert records[0]["stage"] == "startup"
    assert records[0]["source"] == "loopback"
    assert [record["step"] for record in records if record["stage"] == "startup_step"] == ["preflight"]
    assert records[-1]["stage"] == "preflight" and records[-1]["error"] == "synthetic stop"


def test_live_capture_uses_the_device_native_rate_and_one_quality_conversion(monkeypatch):
    """The reported request: capture at the highest quality the device offers.

    The loopback device on this machine reports one pin format only, `ch= 2, bits=16, rate= 96000`.
    Opening it at 16 kHz mono made the device filter do the conversion; the input must be opened at
    the device's own rate instead, with the single conversion to 16 kHz mono done by our own
    anti-aliased, dithered resampler.
    """
    listing = '[in#0 @ 000001] DirectShow audio only device options (from audio devices)\n[in#0 @ 000001]  Pin "Capture Virtual Audio Pin" (alternative pin name "1")\n[in#0 @ 000001]   ch= 2, bits=16, rate= 96000\n'
    probed = {}

    def fake_run(args, **kwargs):
        probed["args"] = args
        return type("Result", (), {"returncode": 0, "stdout": b"", "stderr": listing.encode("utf-8")})()

    monkeypatch.setattr("subtitle_cli.audio.subprocess.run", fake_run)
    assert device_capture_format(Path("ffmpeg.exe"), "virtual-audio-capturer") == CaptureFormat(96000, 2)
    assert probed["args"][-1] == "audio=virtual-audio-capturer"

    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        return type("Process", (), {"poll": lambda self: None, "stdout": io.BytesIO(), "terminate": lambda self: None, "wait": lambda self, timeout=None: 0})()

    monkeypatch.setattr("subtitle_cli.audio.subprocess.Popen", fake_popen)
    FFmpegInput(Path("ffmpeg.exe"), device="virtual-audio-capturer", capture_format=CaptureFormat(96000, 2)).stop()
    args = captured["args"]
    # The device is asked for its own rate, never for the 16 kHz the models want.
    assert args[args.index("-ar") + 1] == "96000" and args[args.index("-ac") + 1] == "2"
    assert "16000" not in args[args.index("-ar") + 1]
    assert args[args.index("-af") + 1] == capture_filter(96000, 2)
    assert args[-3:] == ["-f", "s16le", "pipe:1"]


def test_capture_filter_is_anti_aliased_and_low_frequency_clean():
    """The whole band above 8 kHz folds back into the speech band if the decimation filter is
    careless, and DC offset from a loopback device reads as speech energy to the VAD. Both are
    pinned here so a later edit cannot quietly drop them, and SoXR stays out: this FFmpeg lists it
    in `-h filter=aresample` but was built without it, which fails the entire filter graph."""
    value = capture_filter(96000, 2)
    assert value.startswith("highpass=f=20")
    assert f"aresample={SAMPLE_RATE}" in value
    assert "phase_shift=24" in value and "filter_size=256" in value and "cutoff=0.97" in value
    assert "dither_method=" in value
    assert "channel_layouts=mono" in value and "sample_fmts=s16" in value
    assert "soxr" not in value


def test_a_failed_probe_falls_back_to_a_format_every_device_accepts(monkeypatch):
    """A probe that cannot read the device must never fail the task, and must not be mistaken for
    "the device is 16 kHz": the fallback is a normal capture rate, and 16 kHz would put the device
    back in charge of the conversion."""
    def broken(*args, **kwargs):
        raise OSError("no such device")

    monkeypatch.setattr("subtitle_cli.audio.subprocess.run", broken)
    assert device_capture_format(Path("ffmpeg.exe"), "missing") == CaptureFormat(48_000, 2)

    def empty(*args, **kwargs):
        return type("Result", (), {"returncode": 0, "stdout": b"", "stderr": b"[in#0] Could not find device\n"})()

    monkeypatch.setattr("subtitle_cli.audio.subprocess.run", empty)
    assert device_capture_format(Path("ffmpeg.exe"), "missing") == CaptureFormat(48_000, 2)


def test_the_ui_is_told_the_rate_the_device_was_opened_with(monkeypatch):
    """`采集中` is only useful if it also says what was captured. The bridge forwards the format the
    CLI probed, so "is it capturing at full quality" is answerable from the window itself."""
    seen = []

    def fake_task(args, root, *, stop_requested, on_status, on_capture, on_subtitle, on_detail, poll, on_progress, on_listening):
        # Twice the rate in samples: the second report is the one that proves the format travels
        # with a normal every-second update, not only with the first one.
        on_capture(0, "96000 Hz / 2 ch", "最高：设备原生采样率 + 高质量重采样")
        on_capture(2 * SAMPLE_RATE)
        return "stopped"

    monkeypatch.setattr(ui_bridge, "run_task", fake_task)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    ui_bridge.main(["live", "--audio-device", "loopback", "--output", "out.srt"])
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    statuses = [message for message in messages if message["type"] == "status"]
    assert statuses[0]["capture_format"] == "96000 Hz / 2 ch"
    assert statuses[0]["capture_quality"].startswith("最高")
    assert statuses[0]["captured_seconds"] == 0 and statuses[1]["captured_seconds"] == 2
    assert statuses[1]["capture_format"] == "96000 Hz / 2 ch"


def test_live_stop_drains_captured_blocks(monkeypatch):
    stop = threading.Event()

    class FakeProcess:
        def __init__(self):
            self.ended = False

        def poll(self):
            return 0 if self.ended else None

        def terminate(self):
            self.ended = True

    class FakeAudio:
        def __init__(self, *args, **kwargs):
            self.process = FakeProcess()

        def blocks(self):
            # Enough PCM for the capture report to reach its first whole second, so a stop can be
            # requested while the producer is genuinely mid-stream.
            for start in range(0, 8 * BLOCK_SAMPLES, BLOCK_SAMPLES):
                yield PCMBlock(start, bytes(BLOCK_SAMPLES * 2))
            while not stop.is_set():
                time.sleep(0.001)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.process.terminate()

    class FakePipeline:
        def __init__(self):
            self.blocks = []
            self.finished = False
            self.latencies = []
            self.start_sample = None

        def feed(self, block, backlog):
            self.blocks.append(block)
            self.start_sample = block.start_sample  # speech starts on the first block

        def finish(self):
            self.finished = True

    monkeypatch.setattr("subtitle_cli.cli.FFmpegInput", FakeAudio)
    pipeline = FakePipeline()
    args = type("Args", (), {"audio_device": "loopback"})()
    result = run_live(args, {"ffmpeg": Path("fake.exe")}, pipeline, stop, lambda *_args: stop.set())
    assert result is True
    assert len(pipeline.blocks) == 1 and pipeline.finished


def test_queue_backpressure_overrun_and_drain():
    queue = PCMQueue(capacity_samples=4)
    assert queue.put(PCMBlock(0, bytes(8)))
    assert queue.peak_backlog_seconds() == 4 / 16_000
    assert not queue.put(PCMBlock(4, bytes(2)), timeout=0.01)
    assert queue.get().start_sample == 0
    assert queue.put(PCMBlock(4, bytes(2)))
    queue.close()
    assert queue.get().start_sample == 4
    assert queue.get() is None


def test_queue_waits_for_space_and_cancel():
    queue = PCMQueue(capacity_samples=1)
    queue.put(PCMBlock(0, bytes(2)))
    results = []
    worker = threading.Thread(target=lambda: results.append(queue.put(PCMBlock(1, bytes(2)), timeout=0.5)))
    worker.start()
    time.sleep(0.02)
    queue.get()
    worker.join(timeout=1)
    assert results == [True]
    queue.close()
    assert not queue.put(PCMBlock(2, bytes(2)))


def test_cli_and_selected_model_paths(tmp_path):
    assert arguments(["offline", "--input", "x.mkv", "--output", "x.srt"]).mt_model == "7b"
    assert arguments(["offline", "--input", "x.mkv", "--output", "x.srt"]).pipeline == "fast"
    assert arguments(["live", "--audio-device", "loopback", "--output", "x.srt"]).no_speech_timeout == 300.0
    assert "--device" not in llama_args(Path("server"), Path("model"), "gpu", 1234)
    assert ["--device", "none", "-ngl", "0"] == llama_args(Path("server"), Path("model"), "cpu", 1234)[3:7]
    # Offline asks for several translations at once, so the server must expose as many slots.
    assert llama_args(Path("server"), Path("model"), "gpu", 1234, 3)[-9:] == ["-c", "6144", "--parallel", "3", "--jinja", "--host", "127.0.0.1", "--port", "1234"]
    assert llama_args(Path("server"), Path("model"), "gpu", 1234, 1)[-9:-5] == ["-c", "2048", "--parallel", "1"]
    entries = {key: key for key in ("ffmpeg", "llama_server", "llama_runtime_bin", "qwen_asr", "qwen_aligner", "smart_turn", "sat", "sat_tokenizer", "hymt_7b", "hymt_1_8b")}
    (tmp_path / "config.toml").write_text("[paths]\n" + "".join(f'{key} = "{value}"\n' for key, value in entries.items()), encoding="utf-8")
    for key in entries:
        if key != "hymt_1_8b":
            (tmp_path / key).touch()
    assert "hymt_7b" in configured_paths(tmp_path, "7b")
    with pytest.raises(PipelineFailure, match="hymt_1_8b"):
        configured_paths(tmp_path, "1.8b")


def test_subprocess_cleanup(monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.stdout = io.BytesIO()
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    process = FakeProcess()
    monkeypatch.setattr("subtitle_cli.audio.subprocess.Popen", lambda *args, **kwargs: process)
    with FFmpegInput(Path("ffmpeg.exe"), video=Path("video.mkv")):
        pass
    assert process.terminated and process.stdout.closed

    translator = object.__new__(Translator)
    translator.process = FakeProcess()
    translator.client = type("Client", (), {"close": lambda self: None})()
    translator.log = io.BytesIO()
    translator.close()
    assert translator.process.terminated and translator.log.closed

def test_empty_asr_records_diagnostic_and_skips_translation(tmp_path):
    class EmptyModels:
        def transcribe(self, pcm, source_lang):
            assert source_lang == "ja"
            return "", "Japanese"

    class NoTranslation:
        def translate(self, text):
            raise AssertionError("empty ASR output must not be translated")

    output = tmp_path / "empty.srt"
    with SubtitleWriter(output) as writer:
        pipeline = Pipeline(EmptyModels(), NoTranslation(), writer, "ja", "7b", "gpu")
        pipeline.start_sample = 32_000
        pipeline.utterance = bytearray(16_000)
        pipeline._commit(0.0)
    assert output.read_text(encoding="utf-8") == ""
    assert '"status": "empty_transcript"' in output.with_suffix(".jsonl").read_text(encoding="utf-8")
    assert '"start_sample": 32000' in output.with_suffix(".jsonl").read_text(encoding="utf-8")


def test_hotwords_go_in_as_a_system_message_and_are_absent_when_empty():
    """The documented `prompt=` route is ignored by this transformers build, so the word list
    has to reach the model as a system message through the chat template — and an empty list
    must produce exactly the old request, not an empty system message."""
    from subtitle_cli.models import Models

    import torch

    def torch_tensor():
        return torch.zeros((1, 3), dtype=torch.long)

    calls = []
    ids = torch_tensor()

    class Batch(dict):
        """Stands in for the processor output: unpackable and movable to a device."""

        def to(self, *_args):
            return self

    class FakeProcessor:
        def apply_chat_template(self, chat, **kwargs):
            calls.append(("chat", chat, kwargs))
            return Batch(input_ids=ids)

        def apply_transcription_request(self, **kwargs):
            calls.append(("plain", kwargs))
            return Batch(input_ids=ids)

    def make(hotwords):
        models = object.__new__(Models)
        models.hotwords = (hotwords or "").strip()
        models.asr_processor = FakeProcessor()
        models.asr_model = type("Model", (), {"device": "cpu", "dtype": None, "generate": staticmethod(lambda **kwargs: ids)})()
        models.torch = type("Torch", (), {"inference_mode": staticmethod(lambda: __import__("contextlib").nullcontext())})()
        models.asr_processor.decode = lambda *a, **k: [{"language": "Japanese", "transcription": "テスト"}]
        return models

    models = make("人名：张三, 李四。术语：MIOpen。")
    text, language = models.transcribe(b"\x00\x00" * 8000, "ja")
    assert (text, language) == ("テスト", "Japanese")
    kind, chat, kwargs = calls[0]
    assert kind == "chat" and kwargs["continue_final_message"] is True
    assert chat[0] == {"role": "system", "content": [{"type": "text", "text": "人名：张三, 李四。术语：MIOpen。"}]}
    assert chat[-1]["content"][0]["text"] == "language Japanese<asr_text>"

    calls.clear()
    models = make("   ")
    models.transcribe(b"\x00\x00" * 8000, "ja")
    assert calls[0][0] == "plain" and calls[0][1]["language"] == "Japanese"


def test_translation_prompt_is_the_instruction_before_every_sentence(monkeypatch):
    """The instruction is the one user-visible knob on the translation model, so an empty box
    must keep sending exactly what every previous version sent, and a filled box must replace
    it without losing the blank line that separates instruction from sentence."""
    from subtitle_cli.translator import DEFAULT_PROMPT, Translator

    sent = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "  译文  "}}]}

    class FakeClient:
        def post(self, url, json=None):
            sent.append(json)
            return FakeResponse()

    def make(prompt):
        translator = object.__new__(Translator)
        translator.prompt = (prompt or "").strip() or DEFAULT_PROMPT
        translator.url = "http://127.0.0.1:1"
        translator.client = FakeClient()
        return translator

    assert make("").translate("Hello.") == "译文"
    assert sent[0]["messages"][1]["content"] == f"{DEFAULT_PROMPT}\n\nHello."

    sent.clear()
    assert make("只输出日语，保留敬语：").translate("Hello.") == "译文"
    assert sent[0]["messages"][1]["content"] == "只输出日语，保留敬语：\n\nHello."
    assert sent[0]["messages"][0] == {"role": "system", "content": ""}
    assert sent[0]["temperature"] == 0 and sent[0]["stream"] is False


def test_translation_prompt_reaches_the_translator_from_the_command_line(monkeypatch, tmp_path):
    entries = {key: key for key in ("ffmpeg", "llama_server", "llama_runtime_bin", "qwen_asr", "qwen_aligner", "smart_turn", "sat", "sat_tokenizer", "hymt_7b", "hymt_1_8b")}
    (tmp_path / "config.toml").write_text("[paths]\n" + "".join(f'{key} = "{value}"\n' for key, value in entries.items()), encoding="utf-8")
    for key in entries:
        (tmp_path / key).touch()
    seen = {}

    class FakeModels:
        def __init__(self, paths, on_progress=None, on_detail=None, hotwords=""):
            pass

    class FakeTranslator:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            raise PipelineFailure("translator_start", "stop here")

    monkeypatch.setattr("subtitle_cli.cli.preflight", lambda paths, device: None)
    monkeypatch.setattr("subtitle_cli.cli.Models", FakeModels)
    monkeypatch.setattr("subtitle_cli.cli.Translator", FakeTranslator)
    video = tmp_path / "video.mkv"
    video.touch()
    args = arguments(["offline", "--input", str(video), "--output", str(tmp_path / "o.srt"), "--mt-prompt", "只输出简体中文"])
    with pytest.raises(PipelineFailure):
        run_task(args, tmp_path)
    assert seen["prompt"] == "只输出简体中文" and seen["slots"] == 2
    assert arguments(["offline", "--input", "v", "--output", "o.srt"]).mt_prompt == ""


def test_hotwords_reach_the_model_constructor_from_the_command_line(monkeypatch, tmp_path):
    entries = {key: key for key in ("ffmpeg", "llama_server", "llama_runtime_bin", "qwen_asr", "qwen_aligner", "smart_turn", "sat", "sat_tokenizer", "hymt_7b", "hymt_1_8b")}
    (tmp_path / "config.toml").write_text("[paths]\n" + "".join(f'{key} = "{value}"\n' for key, value in entries.items()), encoding="utf-8")
    for key in entries:
        (tmp_path / key).touch()
    seen = []

    class FakeModels:
        def __init__(self, paths, on_progress=None, on_detail=None, hotwords=""):
            seen.append(hotwords)
            raise PipelineFailure("model_load", "stop here")

    monkeypatch.setattr("subtitle_cli.cli.preflight", lambda paths, device: None)
    monkeypatch.setattr("subtitle_cli.cli.Models", FakeModels)
    video = tmp_path / "video.mkv"
    video.touch()
    args = arguments(["offline", "--input", str(video), "--output", str(tmp_path / "out.srt"), "--hotwords", "人名：张三"])
    with pytest.raises(PipelineFailure):
        run_task(args, tmp_path)
    assert seen == ["人名：张三"]
    assert arguments(["offline", "--input", "v", "--output", "o.srt"]).hotwords == ""
