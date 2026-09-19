"""Append-only subtitle and diagnostic output."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Callable


def srt_time(seconds: float, *, end: bool = False) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("invalid subtitle time")
    milliseconds = math.ceil(seconds * 1000 - 1e-9) if end else math.floor(seconds * 1000 + 1e-9)
    hours, rem = divmod(milliseconds, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


class SubtitleWriter:
    def __init__(self, output: Path, on_subtitle: Callable[[int, int, int, str], None] | None = None, on_diagnostic: Callable[[dict], None] | None = None):
        if output.suffix.lower() != ".srt":
            raise ValueError("--output must end in .srt")
        output.parent.mkdir(parents=True, exist_ok=True)
        self.srt = output.open("x", encoding="utf-8", newline="\n")
        try:
            self.jsonl = output.with_suffix(".jsonl").open("x", encoding="utf-8", newline="\n")
        except BaseException:
            self.srt.close()
            output.unlink()
            raise
        self.count = 0
        self.last_end = 0.0
        self.on_subtitle = on_subtitle
        self.on_diagnostic = on_diagnostic

    def append(self, start: float, end: float, translation: str, details: dict) -> None:
        if start < self.last_end or end <= start or not translation.strip():
            raise ValueError("invalid or non-monotonic subtitle")
        number = self.count + 1
        self.srt.write(f"{number}\n{srt_time(start)} --> {srt_time(end, end=True)}\n{translation.strip()}\n\n")
        self.srt.flush()
        self.jsonl.write(json.dumps({"subtitle": number, "start": start, "end": end, "translation": translation.strip(), **details}, ensure_ascii=False) + "\n")
        self.jsonl.flush()
        self.count = number
        self.last_end = end
        if self.on_subtitle is not None:
            self.on_subtitle(number, math.floor(start * 1000 + 1e-9), math.ceil(end * 1000 - 1e-9), translation.strip())

    def diagnostic(self, **details) -> None:
        self.jsonl.write(json.dumps(details, ensure_ascii=False) + "\n")
        self.jsonl.flush()
        if self.on_diagnostic is not None:
            self.on_diagnostic(details)

    def close(self) -> None:
        self.srt.close()
        self.jsonl.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
