# -*- coding: utf-8 -*-
r"""校验烧录结果：时长、流、全程可解码性，以及字幕在开头/中间/结尾都真的烧进去了。

    python tools\verify_burn.py
    python tools\verify_burn.py --output "D:\别的文件.mp4" --srt 字幕.srt
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
DEFAULT_OUTPUT = Path(r"F:\迅雷下载\sone-054-4k\sone-054-4k.hard-zh.mp4")
DEFAULT_SRT = ROOT / "outputs" / "offline" / "subtitle4k.zh.srt"


def parse_cues(srt: Path) -> list[tuple[float, float]]:
    text = srt.read_text(encoding="utf-8-sig")
    pattern = r"(\d\d):(\d\d):(\d\d),(\d\d\d)\s*-->\s*(\d\d):(\d\d):(\d\d),(\d\d\d)"
    cues = []
    for match in re.finditer(pattern, text):
        start = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + int(match.group(4)) / 1000
        end = int(match.group(5)) * 3600 + int(match.group(6)) * 60 + int(match.group(7)) + int(match.group(8)) / 1000
        cues.append((start, end))
    return cues


def frame_grey(source: Path, seconds: float, tag: str) -> np.ndarray:
    shot = Path(tempfile_dir()) / f"v_{tag}.png"
    subprocess.run([str(FFMPEG), "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{seconds:.3f}", "-i", str(source), "-frames:v", "1", str(shot)], check=True)
    return np.asarray(Image.open(shot).convert("L"), dtype=np.int16)


def tempfile_dir() -> Path:
    path = ROOT / ".tmp-verify"
    path.mkdir(exist_ok=True)
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="verify_burn")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--srt", type=Path, default=DEFAULT_SRT)
    parser.add_argument("--source", type=Path, default=Path(r"F:\迅雷下载\sone-054-4k\sone-054-4k.mp4"))
    args = parser.parse_args(argv)

    if not args.output.is_file():
        print(f"找不到输出文件: {args.output}", file=sys.stderr)
        return 2

    print(f"输出: {args.output}")
    print(f"大小: {args.output.stat().st_size / 1073741824:.2f} GiB")

    info = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(args.output)], capture_output=True, text=True)
    for line in info.stderr.splitlines():
        if "Duration" in line:
            print("时长:", line.split("Duration:")[1].split(",")[0].strip())
        if "Stream #" in line:
            print("流  :", line.split("Stream #")[1].strip())

    # Decode the whole file with only real errors reported: this proves every frame and packet is
    # readable, which a successful encode alone does not.
    print("\n全程解码检查（只报错误）…")
    decode = subprocess.run([str(FFMPEG), "-v", "error", "-i", str(args.output), "-f", "null", "-"],
                            capture_output=True, text=True)
    errors = [line for line in decode.stderr.splitlines() if line.strip()]
    print("  解码错误:", "无" if not errors else f"{len(errors)} 条")
    for line in errors[:5]:
        print("   ", line[:140])

    # Subtitles must be in the pixels near the start, the middle and the end: a burn that only
    # worked for the first minutes would otherwise pass unnoticed.
    cues = parse_cues(args.srt)
    if not cues:
        print("\n字幕文件里没有解析到条目")
        return 1
    picks = [cues[0], cues[len(cues) // 2], cues[-1]]
    print(f"\n字幕像素抽查（共 {len(cues)} 条，取首/中/末）:")
    for index, (start, end) in enumerate(picks):
        inside = start + (end - start) / 2
        burned = frame_grey(args.output, inside, f"burned{index}")
        plain = frame_grey(args.source, inside, f"plain{index}")
        # Same frame, so the difference is the subtitle plus codec noise; count strong changes.
        changed = np.abs(burned - plain) > 70
        rows = np.where(changed.any(axis=1))[0]
        span = f"y={rows.min()}..{rows.max()}" if len(rows) else "无"
        verdict = "有字幕" if int(changed.sum()) > 20000 else "疑似没有字幕"
        print(f"  {start:7.2f}s  {int(changed.sum()):>7} 像素改变  {span:<20} {verdict}")

    # A frame rendered out for a quick look.
    preview = tempfile_dir() / "final_preview.png"
    subprocess.run([str(FFMPEG), "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{picks[1][0] + 0.2:.3f}", "-i", str(args.output), "-frames:v", "1",
                    "-vf", "scale=1280:-1", str(preview)], check=True)
    print(f"\n预览图: {preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
