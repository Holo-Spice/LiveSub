"""FFmpeg PCM input and a sample-counted live queue."""

from __future__ import annotations

import collections
import math
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 16_000
BLOCK_SAMPLES = 512
QUEUE_SAMPLES = 30 * SAMPLE_RATE


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
    def __init__(self, executable: Path, *, video: Path | None = None, device: str | None = None):
        if (video is None) == (device is None):
            raise ValueError("select exactly one audio input")
        self.stderr = tempfile.TemporaryFile(mode="w+b")
        if video is not None:
            args = [str(executable), "-nostdin", "-hide_banner", "-i", str(video), "-map", "0:a:0", "-vn", "-af", "aresample=16000:async=1:first_pts=0", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"]
        else:
            args = [str(executable), "-nostdin", "-hide_banner", "-f", "dshow", "-i", f"audio={device}", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"]
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
