"""Verify the HEVC probe: codec profile/pixel format for phone playback, and burned-in pixels."""

import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

FF = r"C:\Users\29279\LiveSub\tools\ffmpeg\bin\ffmpeg.exe"
PROBE = Path(r"C:\Users\29279\LiveSub\.tmp-bench\hevc_probe.mp4")
SRC = r"F:\迅雷下载\sone-054-4k\sone-054-4k.mp4"
SRT = Path(r"C:\Users\29279\LiveSub\outputs\offline\subtitle4k.zh.srt")
WORK = Path(r"C:\Users\29279\LiveSub\.tmp-bench")

info = subprocess.run([FF, "-hide_banner", "-i", str(PROBE)], capture_output=True, text=True)
for line in info.stderr.splitlines():
    if "Stream #" in line or "Duration" in line:
        print(line.strip())

# A frame inside cue 2 (19.906-22.066) must differ from the same frame without the filter.
escaped = str(SRT).replace("\\", "/").replace(":", "\\:")
FILTER = f"subtitles=filename='{escaped}'"
plain = WORK / "hevc_plain.mp4"
plain.unlink(missing_ok=True)
subprocess.run([FF, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-t", "26", "-i", SRC,
                "-c:v", "hevc_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "24", "-qp_p", "24",
                "-profile:v", "main", "-pix_fmt", "yuv420p", "-an", str(plain)], check=True)


def grey(source, seconds):
    shot = WORK / f"v_{Path(source).stem}_{seconds}.png"
    subprocess.run([FF, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", str(seconds),
                    "-i", str(source), "-frames:v", "1", str(shot)], check=True)
    return np.asarray(Image.open(shot).convert("L"), dtype=np.int16)


for seconds in (20.5, 24.5):
    a = grey(PROBE, seconds)
    b = grey(plain, seconds)
    changed = np.abs(a - b) > 60
    rows = np.where(changed.any(axis=1))[0]
    span = f"y={rows.min()}..{rows.max()}" if len(rows) else "无"
    print(f"t={seconds}s 与无字幕帧差异像素 {int(changed.sum()):>7}  区域 {span}")
