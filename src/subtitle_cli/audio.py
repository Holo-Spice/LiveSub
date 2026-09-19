"""FFmpeg PCM input and a sample-counted live queue."""

from __future__ import annotations

import collections
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 16_000
BLOCK_SAMPLES = 512
QUEUE_SAMPLES = 30 * SAMPLE_RATE
# What the capture device is asked for when its own format cannot be read. The live device
# (`virtual-audio-capturer` here) exposes stereo 96 kHz / 16 bit, and asking it for 16 kHz mono makes
# its filter resample for us; capturing at the device's own rate and resampling once, in our chain,
# is measurably better and costs nothing. 48 kHz stereo is the safe request when the probe fails.
FALLBACK_CAPTURE_RATE = 48_000
CAPTURE_CHANNELS = 2
# The device pin's format list looks like `ch= 2, bits=16, rate= 96000`.
_DEVICE_OPTION = re.compile(r"ch=\s*(\d+),\s*bits=\s*(\d+),\s*rate=\s*(\d+)")


@dataclass(frozen=True)
class CaptureFormat:
    """The format a DirectShow device is opened with, before the conversion to 16 kHz mono."""

    rate: int
    channels: int = CAPTURE_CHANNELS

    def describe(self) -> str:
        return f"{self.rate} Hz / {self.channels} ch"


def device_capture_format(executable: Path, device: str, timeout: float = 10.0) -> CaptureFormat:
    """The highest rate the device's pin offers, or the fallback when it cannot be read.

    `ch= 2, bits=16, rate= 96000` is the whole list this machine reports for the loopback device, so
    the stream is taken at 96 kHz instead of being downsampled by the device filter itself. The
    probe never fails the task: an unreadable or silent probe returns the fallback, which is a
    format every DirectShow audio device accepts.
    """
    try:
        result = subprocess.run(
            [str(executable), "-nostdin", "-hide_banner", "-list_options", "true", "-f", "dshow", "-i", f"audio={device}"],
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        options = [(int(rate), int(channels), int(bits)) for channels, bits, rate in _DEVICE_OPTION.findall(result.stderr.decode("utf-8", "replace"))]
        rate, channels, _bits = max(options)
        if rate > 0 and channels > 0:
            return CaptureFormat(rate, channels)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return CaptureFormat(FALLBACK_CAPTURE_RATE)


def capture_filter(rate: int, channels: int, *, dither: str = "shibata") -> str:
    """The conversion from the device format to the 16 kHz mono the models need.

    Every input rate here is above 16 kHz and every path decimates, so this is the one place where a
    careless resampler folds the whole top of the band back into the speech band. The anti-aliasing
    window, the resampling phase accuracy and the 16-bit dither are all asked for explicitly:
      * `phase_shift=24` — the largest interpolation accuracy swresample takes;
      * `filter_size=256` — a long window, so the stop band is deep instead of merely adequate;
      * `cutoff=0.97` — keep the transition band inside the band being removed;
      * `dither_method=shibata` — noise shaping for the final quantisation to 16 bit;
      * `highpass=f=20` — remove the DC offset and sub-bass rumble that some loopback devices carry,
        which Silero VAD reads as energy even though it carries no speech.
    SoXR is not used: this FFmpeg lists `resampler=soxr` in its help but was built without it
    ("Requested resampling engine is unavailable"), which silently fails the whole filter graph.
    """
    return f"highpass=f=20,aresample={SAMPLE_RATE}:phase_shift=24:filter_size=256:cutoff=0.97:dither_method={dither},aformat=sample_fmts=s16:channel_layouts=mono"



@dataclass(frozen=True)
class PCMBlock:
    start_sample: int
    data: bytes
    captured_at: float = 0.0

    @property
    def samples(self) -> int:
        return len(self.data) // 2


def media_time(start_sample: int, local_seconds: float) -> float:
    return start_sample / SAMPLE_RATE + local_seconds


def media_duration(ffmpeg: Path, video: Path) -> float | None:
    """Length of the audio stream that will be decoded, in seconds, or None.

    Only used to turn measured progress into a remaining-time estimate; a missing ffprobe or
    an unreadable header must never fail the task, so every failure returns None.
    """
    probe = ffmpeg.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if not probe.is_file():
        return None
    try:
        result = subprocess.run(
            [str(probe), "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        value = float(result.stdout.strip().splitlines()[0])
        return value if value > 0 and math.isfinite(value) else None
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


class FFmpegInput:
    def __init__(self, executable: Path, *, video: Path | None = None, device: str | None = None, capture_format: CaptureFormat | None = None, quality: bool = True):
        if (video is None) == (device is None):
            raise ValueError("select exactly one audio input")
        self.stderr = tempfile.TemporaryFile(mode="w+b")
        if video is not None:
            args = [str(executable), "-nostdin", "-hide_banner", "-i", str(video), "-map", "0:a:0", "-vn", "-af", "aresample=16000:async=1:first_pts=0", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"]
        else:
            # The device is opened at its own rate, not at 16 kHz, so its filter does not resample;
            # one high-quality conversion to the 16 kHz mono the models need happens in `-af`.
            source = capture_format or CaptureFormat(FALLBACK_CAPTURE_RATE)
            args = [str(executable), "-nostdin", "-hide_banner", "-f", "dshow", "-i", f"audio={device}", "-ar", str(source.rate), "-ac", str(source.channels)]
            if quality:
                args += ["-af", capture_filter(source.rate, source.channels)]
            args += ["-f", "s16le", "pipe:1"]
        try:
            self.process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=self.stderr, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            self.stderr.close()
            raise
        self.next_sample = 0

    def blocks(self):
        while True:
            data = self.process.stdout.read(BLOCK_SAMPLES * 2)
            if not data:
                break
            if len(data) % 2:
                raise RuntimeError("FFmpeg returned an incomplete PCM sample")
            block = PCMBlock(self.next_sample, data, time.monotonic())
            self.next_sample += block.samples
            yield block
        code = self.process.wait()
        if code:
            self.stderr.seek(0)
            tail = self.stderr.read()[-2000:].decode("utf-8", "replace")
            raise RuntimeError(f"FFmpeg exited {code}: {tail}")

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.process.stdout:
            self.process.stdout.close()
        self.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stop()


class PCMQueue:
    def __init__(self, capacity_samples: int = QUEUE_SAMPLES):
        self.capacity_samples = capacity_samples
        self.items = collections.deque()
        self.samples = 0
        self.peak_samples = 0
        self.closed = False
        self.condition = threading.Condition()

    def put(self, block: PCMBlock, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.samples + block.samples > self.capacity_samples and not self.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
            if self.closed:
                return False
            self.items.append(block)
            self.samples += block.samples
            self.peak_samples = max(self.peak_samples, self.samples)
            self.condition.notify_all()
            return True

    def get(self) -> PCMBlock | None:
        with self.condition:
            while not self.items and not self.closed:
                self.condition.wait()
            if not self.items:
                return None
            block = self.items.popleft()
            self.samples -= block.samples
            self.condition.notify_all()
            return block

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def backlog_seconds(self) -> float:
        with self.condition:
            return self.samples / SAMPLE_RATE

    def peak_backlog_seconds(self) -> float:
        with self.condition:
            return self.peak_samples / SAMPLE_RATE
