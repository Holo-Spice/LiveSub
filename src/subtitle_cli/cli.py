"""Command entry point and resource lifetime."""

from __future__ import annotations

import argparse
import math
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path
from typing import Callable

from .audio import FFmpegInput, PCMQueue, media_duration
from .models import Models
from .pipeline import FastOfflinePipeline, Pipeline, PipelineFailure, Segmenter
from .subtitles import SubtitleWriter
from .translator import Translator, llama_environment


def arguments(argv=None):
    parser = argparse.ArgumentParser(prog="subtitle-cli")
    modes = parser.add_subparsers(dest="mode", required=True)
    for name in ("offline", "live"):
        mode = modes.add_parser(name)
        if name == "offline":
            mode.add_argument("--input", type=Path, required=True)
        else:
            mode.add_argument("--audio-device", required=True)
        mode.add_argument("--source-lang", choices=("auto", "en", "ja"), default="auto")
        mode.add_argument("--mt-model", choices=("7b", "1.8b"), default="7b")
        mode.add_argument("--mt-device", choices=("gpu", "cpu"), default="gpu")
        mode.add_argument("--output", type=Path, required=True)
        # Offline runs the whole file as fast as the machine allows; live cannot run ahead of
        # the audio, so it only uses the slot count for the translation service.
        mode.add_argument("--mt-slots", type=int, default=2, help="parallel llama-server slots")
        mode.add_argument("--progress-seconds", type=float, default=2.0, help="seconds between progress reports")
        # Free-text context for the speech recogniser: names, product terms, jargon. A sentence
        # works as well as a list. Empty means no context message at all.
        mode.add_argument("--hotwords", default="", help="context that biases recognition, e.g. '人名：张三, 李四。术语：MIOpen, ROCm。'")
        # The instruction the translation model is given, before every sentence. Empty means
        # the built-in default, which is what every previous version sent.
        mode.add_argument("--mt-prompt", default="", help="translation instruction; empty uses the built-in default")
        if name == "offline":
            mode.add_argument("--pipeline", choices=("fast", "sequential"), default="fast")
            mode.add_argument("--max-segment", type=float, default=30.0, help="cut an utterance at the next pause after this many seconds")
            # One utterance already fills the GPU, so a second concurrent recognition stream
            # usually only makes both slower; the number is a knob to measure that, not a
            # setting to raise.
            mode.add_argument("--asr-workers", type=int, default=1)
        else:
            # How long the task keeps listening before it concludes that nothing is playing.
            # Starting the capture before the audio is a normal mistake, so the default is
            # generous: an explicit start of speech is what ends the wait, not a timer.
            mode.add_argument("--no-speech-timeout", type=float, default=300.0)
    return parser.parse_args(argv)


def configured_paths(root: Path, model_name: str) -> dict[str, Path]:
    with (root / "config.toml").open("rb") as file:
        config = tomllib.load(file)["paths"]
    selected = ["ffmpeg", "llama_server", "llama_runtime_bin", "qwen_asr", "qwen_aligner", "smart_turn", "sat", "sat_tokenizer", f"hymt_{model_name.replace('.', '_')}"]
    paths = {key: root / config[key] for key in selected}
    missing = [f"{key}: {path}" for key, path in paths.items() if not path.exists()]
    if missing:
        raise PipelineFailure("config", "missing required paths: " + "; ".join(missing))
    return paths


def preflight(paths: dict[str, Path], device: str):
    import torch

    if sys.version_info[:2] != (3, 12):
        raise PipelineFailure("preflight", "Python 3.12 required")
    if not torch.cuda.is_available() or torch.version.hip is None:
        raise PipelineFailure("preflight", "AMD HIP GPU required for ASR and alignment")
    try:
        value = (torch.ones((16, 16), device="cuda:0", dtype=torch.float16) @ torch.ones((16, 16), device="cuda:0", dtype=torch.float16)).mean().item()
        if not math.isfinite(value):
            raise RuntimeError("non-finite FP16 result")
    except Exception as exc:
        raise PipelineFailure("preflight", f"AMD HIP FP16 operation failed: {exc}") from exc
    for name, args, env in (
        ("FFmpeg", [str(paths["ffmpeg"]), "-hide_banner", "-version"], None),
        ("llama-server", [str(paths["llama_server"]), "--list-devices"], llama_environment(paths["llama_runtime_bin"])),
    ):
        try:
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PipelineFailure("preflight", f"{name} could not start: {exc}") from exc
        output = result.stdout + result.stderr
        if result.returncode:
            raise PipelineFailure("preflight", f"{name} exited {result.returncode}: {output[-1000:]}")
        if name == "llama-server" and device == "gpu" and "AMD Radeon RX 9070 XT" not in output:
            raise PipelineFailure("preflight", "llama-server did not list AMD Radeon RX 9070 XT")


STARTUP_TIMEOUT_SECONDS = 600


class StartupCancelled(Exception):
    """The user stopped the task while a model was still loading."""


def host_details() -> dict:
    """The environment this task really runs in.

    The launcher and a hand-typed CMD start inherit different variables, and the only way to
    explain a start that is fast in one and slow in the other is to record both.
    """
    keys = (
        "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "HSA_OVERRIDE_GFX_VERSION", "MIOPEN_FIND_MODE",
        "MIOPEN_DEBUG_CONV_IMPLICIT_GEMM", "PYTHONPATH", "PYTHONHOME", "HF_HOME", "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "KMP_DUPLICATE_LIB_OK",
        "LIVESUB_ENABLE_MIOPEN", "TEMP", "TMP",
    )
    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "cwd": str(Path.cwd()),
        "cpu_count": os.cpu_count(),
        "path_entries": len(os.environ.get("PATH", "").split(os.pathsep)),
        "path_length": len(os.environ.get("PATH", "")),
        "env": {key: os.environ[key] for key in keys if key in os.environ},
    }


def startup_step(stage: str, work: Callable[[], object], describe: Callable[[], str] | None = None, cancel: threading.Event | None = None, poll: Callable[[], None] | None = None):
    """Run one blocking model-loading step in a daemon thread so a stalled load fails loudly.

    Loading Qwen3-ASR and the aligner onto ROCm has no internal deadline: a HIP stall, a
    driver reset or an antivirus-blocked read of the GGUF/safetensors leaves the task sitting
    at "正在加载模型" forever and writing nothing at all. The worker is abandoned on timeout,
    because the failure path ends the process and the OS reclaims the GPU memory.

    ``poll`` collects pending stop requests without blocking; it runs on this thread so the
    bridge never needs a second thread parked in a stdin read while native DLLs load.
    """
    outcome: list = []
    failure: list = []

    def run() -> None:
        try:
            outcome.append(work())
        except BaseException as exc:
            failure.append(exc)

    worker = threading.Thread(target=run, name=f"livesub-{stage}", daemon=True)
    worker.start()
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while True:
        worker.join(timeout=0.25)
        if not worker.is_alive():
            break
        if poll is not None:
            poll()
        if cancel is not None and cancel.is_set():
            # A load already inside from_pretrained cannot be interrupted, so the worker is
            # abandoned and the process ends as soon as the bridge reports the stop. Without
            # this the launcher force-kills the tree 60 seconds later, in the middle of the
            # HIP/driver teardown, which is the worst possible moment for the next start.
            raise StartupCancelled(f"{stage} was stopped by the user")
        if time.monotonic() >= deadline:
            last = describe() if describe is not None else ""
            raise PipelineFailure(stage, f"{stage} did not finish within {STARTUP_TIMEOUT_SECONDS} seconds{'; last step: ' + last if last else ''}; the model load is stalled")
    if failure:
        raise failure[0]
    return outcome[0]


class ProgressClock:
    """Rate limits progress reports and carries the last measurement forward.

    Every number in a report is measured: how much audio has been decoded, how much has been
    recognised, and the throughput those two imply. The remaining time is that throughput
    applied to the audio still left, and it is recomputed as the run proceeds.
    """

    def __init__(self, interval: float, report: Callable[[dict], None] | None, total: float | None = None):
        self.interval = max(0.5, interval)
        self.report = report
        self.total = total
        self.last = 0.0
        self.state: dict = {}

    def due(self, **values) -> None:
        self.state.update(values)
        now = time.monotonic()
        if self.report is None or now - self.last < self.interval:
            return
        self.last = now
        self.flush()

    def flush(self) -> None:
        """Send the current measurement even if the interval has not elapsed."""
        if self.report is None or not self.state:
            return
        self.state.setdefault("total_seconds", self.total)
        self.report(dict(self.state))


def run_offline(args, paths, pipeline, stop_requested: threading.Event | None = None, poll: Callable[[], None] | None = None) -> bool:
    video = args.input.resolve()
    if not video.is_file():
        raise PipelineFailure("input", f"video not found: {video}")
    try:
        stopped = False
        with FFmpegInput(paths["ffmpeg"], video=video) as audio:
            try:
                for block in audio.blocks():
                    if poll is not None:
                        poll()
                    if stop_requested is not None and stop_requested.is_set():
                        stopped = True
                        break
                    pipeline.feed(block)
            except RuntimeError as exc:
                if pipeline.stage in ("audio", "vad", "smart_turn") and "FFmpeg exited" in str(exc):
                    raise PipelineFailure("ffmpeg", str(exc)) from exc
                raise
        pipeline.finish()
    except PipelineFailure:
        raise
    except Exception as exc:
        raise PipelineFailure(pipeline.stage if pipeline.stage != "audio" else "ffmpeg", str(exc)) from exc
    return stopped


def run_offline_fast(args, paths, pipeline: FastOfflinePipeline, stop_requested: threading.Event | None = None, poll: Callable[[], None] | None = None, progress: Callable[[dict], None] | None = None) -> bool:
    """Decode the file while the workers recognise and translate what is already decoded.

    The audio capture runs on its own thread because it must keep the decode ahead of the
    inference; the main thread is the one that writes subtitles, so numbering stays in media
    order no matter which worker finishes first.
    """
    video = args.input.resolve()
    if not video.is_file():
        raise PipelineFailure("input", f"video not found: {video}")
    stop = stop_requested if stop_requested is not None else threading.Event()
    clock = ProgressClock(getattr(args, "progress_seconds", 2.0), progress, media_duration(paths["ffmpeg"], video))
    segmenter = Segmenter(pipeline.models)
    errors: list[BaseException] = []
    finished = threading.Event()
    total = {"seconds": 0.0}

    def produce() -> None:
        try:
            with FFmpegInput(paths["ffmpeg"], video=video) as audio:
                try:
                    for block in audio.blocks():
                        if stop.is_set():
                            break
                        total["seconds"] += block.samples / 16000
                        segment = segmenter.feed(block, 0.0, pipeline.max_segment_seconds)
                        if segment is not None:
                            pipeline.submit(segment)
                except RuntimeError as exc:
                    if "FFmpeg exited" in str(exc) and not stop.is_set():
                        raise PipelineFailure("ffmpeg", str(exc)) from exc
                    if not stop.is_set():
                        raise
            if not stop.is_set():
                tail = segmenter.finish(0.0)
                if tail is not None:
                    pipeline.submit(tail)
        except BaseException as exc:
            errors.append(exc)
            stop.set()
        finally:
            finished.set()

    producer = threading.Thread(target=produce, name="livesub-decode", daemon=False)
    pipeline.start()
    producer.start()
    try:
        while True:
            pipeline.flush()
            clock.due(decoded_seconds=round(total["seconds"], 1), **pipeline.estimate(clock.total))
            if errors:
                raise errors[0]
            if poll is not None:
                poll()
            if finished.is_set() and not pipeline.has_work():
                break
            time.sleep(0.2)
    finally:
        producer.join(timeout=30)
        if producer.is_alive():
            stop.set()
            producer.join(timeout=5)
    pipeline.drain()
    clock.due(decoded_seconds=round(total["seconds"], 1), **pipeline.estimate(clock.total))
    # The last report is sent even if the interval has not elapsed, so the UI shows the
    # finished value instead of the one from two seconds before the end.
    clock.flush()
    if errors:
        raise errors[0]
    if poll is not None:
        poll()
    return stop.is_set()


def run_live(args, paths, pipeline, stop_requested: threading.Event | None = None, on_capture: Callable[[int], None] | None = None, poll: Callable[[], None] | None = None, on_status: Callable[[str], None] | None = None, on_listening: Callable[[dict], None] | None = None) -> bool:
    queue = PCMQueue()
    stop = stop_requested or threading.Event()
    finished = threading.Event()
    errors = []
    timeout = float(getattr(args, "no_speech_timeout", 300.0))
    listening: dict = {"begin": time.monotonic(), "reported": -1.0, "waiting": True}

    def wait_update(force: bool = False) -> None:
        """Report how long the task has been waiting for speech to start.

        Starting the capture before the audio begins is a normal mistake, and the old
        behaviour ended the whole task with "no speech" while the user was still pressing
        play. The wait is now long and visible instead of short and silent.
        """
        if not listening["waiting"]:
            return
        waited = time.monotonic() - listening["begin"]
        if on_listening is not None and (force or waited - listening["reported"] >= 5):
            listening["reported"] = waited
            on_listening({"waited_seconds": round(waited, 1), "timeout_seconds": round(timeout, 1), "remaining_seconds": round(max(0.0, timeout - waited), 1)})
        if timeout > 0 and waited >= timeout:
            raise PipelineFailure("no_speech", f"等待 {timeout:.0f} 秒仍未检测到语音；请确认所选设备正在播放声音，或增大等待时间后重试")

    with FFmpegInput(paths["ffmpeg"], device=args.audio_device) as audio:
        def on_interrupt(_signum, _frame):
            stop.set()

        previous = signal.signal(signal.SIGINT, on_interrupt)

        def cancel_capture():
            while not finished.wait(0.1):
                if stop.is_set():
                    if audio.process.poll() is None:
                        audio.process.terminate()
                    return

        cancel_worker = threading.Thread(target=cancel_capture, name="livesub-cancel", daemon=True)
        cancel_worker.start()

        def produce():
            try:
                for block in audio.blocks():
                    if stop.is_set():
                        break
                    if on_capture is not None:
                        on_capture(block.start_sample + block.samples)
                    if not queue.put(block, timeout=1.0):
                        if not stop.is_set():
                            errors.append(PipelineFailure("audio_queue_overrun", f"audio_queue_overrun: subtitles are incomplete; peak backlog {queue.peak_backlog_seconds():.3f} seconds"))
                            stop.set()
                        break
                if not stop.is_set():
                    errors.append(PipelineFailure("ffmpeg", "live capture ended unexpectedly; subtitles are incomplete"))
            except Exception as exc:
                if not stop.is_set():
                    errors.append(PipelineFailure("ffmpeg", str(exc)))
            finally:
                queue.close()

        worker = threading.Thread(target=produce, name="livesub-audio", daemon=False)
        worker.start()
        try:
            wait_update(force=True)
            if on_status is not None:
                on_status("listening")
            while (block := queue.get()) is not None:
                if poll is not None:
                    poll()
                pipeline.feed(block, queue.backlog_seconds())
                if listening["waiting"]:
                    if pipeline.start_sample is not None:
                        listening["waiting"] = False
                    else:
                        try:
                            wait_update()
                        except PipelineFailure:
                            # Nothing was ever said: keep the (empty) output and the reason.
                            pipeline.finish()
                            raise
                if stop.is_set():
                    break
            pipeline.finish()
            if errors:
                raise errors[0]
        finally:
            finished.set()
            queue.close()
            if audio.process.poll() is None:
                audio.process.terminate()
            worker.join(timeout=10)
            cancel_worker.join(timeout=2)
            signal.signal(signal.SIGINT, previous)
            if worker.is_alive():
                raise PipelineFailure("cleanup", "audio capture thread did not stop")
    if pipeline.latencies:
        ordered = sorted(pipeline.latencies)
        p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
        pipeline.writer.diagnostic(stage="live_summary", p95_latency_seconds=round(p95, 3), max_backlog_seconds=round(queue.peak_backlog_seconds(), 3))
    return stop.is_set()


def run_task(args, root: Path, stop_requested: threading.Event | None = None, on_status: Callable[[str], None] | None = None, on_capture: Callable[[int], None] | None = None, on_subtitle: Callable[[int, int, int, str], None] | None = None, on_detail: Callable[[str, float | None], None] | None = None, poll: Callable[[], None] | None = None, on_progress: Callable[[dict], None] | None = None, on_listening: Callable[[dict], None] | None = None) -> str:
    paths = configured_paths(root, args.mt_model)
    if args.mode == "offline" and not args.input.is_file():
        raise PipelineFailure("input", f"video not found: {args.input}")
    incomplete = False
    cancelled = False
    stopped = False
    started = time.monotonic()
    current = [""]

    def note_diagnostic(details: dict) -> None:
        nonlocal incomplete
        if details.get("status") == "incomplete_residual":
            incomplete = True

    with SubtitleWriter(args.output, on_subtitle=on_subtitle, on_diagnostic=note_diagnostic) as writer:
        def note_step(step: str) -> None:
            """Report and record startup progress. A run that never reaches its first
            subtitle must still leave a trace of how far it got and how long it took."""
            if on_status is not None:
                on_status(step)
            writer.diagnostic(stage="startup_step", step=step, elapsed_seconds=round(time.monotonic() - started, 3))

        def note_detail(label: str, seconds: float | None) -> None:
            """Report and record one import or model constructor. The start line is written
            first, so a stalled load names the exact item it stopped on instead of only the
            stage, which was the whole reason the 03:08/03:13 runs could not be explained."""
            current[0] = label
            if on_detail is not None:
                on_detail(label, seconds)
            entry = {"stage": "startup_detail", "step": label, "state": "start" if seconds is None else "done"}
            if seconds is not None:
                entry["seconds"] = round(seconds, 3)
            writer.diagnostic(**entry)

        try:
            # Written before the first model is touched, so the JSONL is never empty.
            writer.diagnostic(stage="startup", mode=args.mode, source=str(args.input) if args.mode == "offline" else args.audio_device, source_lang=args.source_lang, mt_model=args.mt_model, mt_device=args.mt_device, hotwords=getattr(args, "hotwords", ""), mt_prompt=getattr(args, "mt_prompt", ""))
            writer.diagnostic(stage="startup_host", **host_details())
            if poll is not None:
                poll()
            note_step("preflight")
            preflight(paths, args.mt_device)
            note_step("model_load")
            try:
                models = startup_step("model_load", lambda: Models(paths, note_step, note_detail, getattr(args, "hotwords", "")), describe=lambda: current[0], cancel=stop_requested, poll=poll)
            except StartupCancelled:
                cancelled = True
                models = None
            except PipelineFailure:
                raise
            except Exception as exc:
                raise PipelineFailure("model_load", str(exc)) from exc
            if cancelled:
                writer.diagnostic(stage="startup_cancelled", step=current[0], elapsed_seconds=round(time.monotonic() - started, 3))
                raise StartupCancelled(current[0])
            stopped = stop_requested is not None and stop_requested.is_set()
            if not stopped:
                note_step("translator_start")
                model_path = paths[f"hymt_{args.mt_model.replace('.', '_')}"]
                slots = max(1, int(getattr(args, "mt_slots", 2)))
                try:
                    translator = Translator(paths["llama_server"], model_path, paths["llama_runtime_bin"], args.mt_device, poll=poll, slots=slots, prompt=getattr(args, "mt_prompt", ""))
                except Exception as exc:
                    raise PipelineFailure("translator_start", str(exc)) from exc
                note_step("translator_ready")
                with translator:
                    action = "采集实时音频并翻译" if args.mode == "live" else "转写视频并翻译"
                    stop_hint = "，按 Ctrl+C 停止" if args.mode == "live" else ""
                    print(f"模型加载完成：ASR / Aligner / SaT / 翻译 {args.mt_model}({args.mt_device}) 已就绪；开始{action}（源语言 {args.source_lang}，输出 {args.output}）{stop_hint}。", flush=True)
                    if stop_requested is not None and stop_requested.is_set():
                        stopped = True
                    elif args.mode == "offline" and getattr(args, "pipeline", "fast") == "fast":
                        total = media_duration(paths["ffmpeg"], args.input.resolve()) if args.input.is_file() else None
                        writer.diagnostic(stage="offline_pipeline", pipeline="fast", slots=slots, max_segment_seconds=float(getattr(args, "max_segment", 30.0)), total_seconds=None if total is None else round(total, 1))
                        fast = FastOfflinePipeline(
                            models,
                            translator,
                            writer,
                            args.source_lang,
                            args.mt_model,
                            args.mt_device,
                            slots=slots,
                            asr_workers=max(1, int(getattr(args, "asr_workers", 1))),
                            max_segment_seconds=float(getattr(args, "max_segment", 30.0)),
                            on_status=on_status,
                            stop=stop_requested,
                        )
                        stopped = run_offline_fast(args, paths, fast, stop_requested, poll, on_progress)
                    else:
                        pipeline = Pipeline(models, translator, writer, args.source_lang, args.mt_model, args.mt_device, on_status=on_status)
                        if args.mode == "offline":
                            stopped = run_offline(args, paths, pipeline, stop_requested, poll)
                        else:
                            stopped = run_live(args, paths, pipeline, stop_requested, on_capture, poll, on_status, on_listening)
        except StartupCancelled:
            raise
        except Exception as exc:
            stage = exc.stage if isinstance(exc, PipelineFailure) else "startup"
            source = str(args.input) if args.mode == "offline" else args.audio_device
            try:
                writer.diagnostic(stage=stage, error=str(exc), input=source, mt_model=args.mt_model, mt_device=args.mt_device, exit_code=1)
            except OSError as write_error:
                print(f"diagnostic_write: {write_error}", file=sys.stderr)
            raise
    return "incomplete" if incomplete else "stopped" if stopped else "completed"


def main(argv=None) -> int:
    args = arguments(argv)
    try:
        cwd_root = Path.cwd()
        root = cwd_root if (cwd_root / "config.toml").is_file() else Path(__file__).resolve().parents[2]
        run_task(args, root)
        return 0
    except KeyboardInterrupt:
        print("interrupted; subtitles may be incomplete", file=sys.stderr)
        return 130
    except Exception as exc:
        stage = exc.stage if isinstance(exc, PipelineFailure) else "startup"
        print(f"{stage}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
