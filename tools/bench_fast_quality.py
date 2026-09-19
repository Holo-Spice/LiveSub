"""Quality check of the fast encoder modes: whole-frame and dark-area error against the source."""

import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

FF = r"C:\Users\29279\LiveSub\tools\ffmpeg\bin\ffmpeg.exe"
VIDEO = Path(r"F:\迅雷下载\sone-054-4k\sone-054-4k.mp4")
W = Path(r"C:\Users\29279\LiveSub\.tmp-bench\fast")
OFFSET = 2400  # the clips above were cut at this point
SAMPLES = (5.0, 15.0, 25.0)


def frame(source, seconds, tag):
    path = W / f"q_{tag}.png"
    subprocess.run([FF, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-ss", str(seconds), "-i", str(source), "-frames:v", "1", str(path)], check=True)
    return np.asarray(Image.open(path).convert("L"), dtype=np.int16)


originals = [frame(VIDEO, OFFSET + s, f"orig{int(s)}") for s in SAMPLES]
dark_mask = np.zeros_like(originals[0], dtype=bool)
for image in originals:
    dark_mask |= image < 40
print(f"采样 {len(SAMPLES)} 帧，暗部像素占比 {dark_mask.mean() * 100:.1f}%\n")

for name in ("amf_quality_q22", "amf_speed_q22", "amf_balanced_q22", "hevc_amf_speed_q24", "libx264_ultrafast"):
    clip = W / f"{name}.mp4"
    if not clip.exists():
        print(f"{name:<20} 缺少样本，跳过")
        continue
    errors = []
    dark_errors = []
    for original, seconds in zip(originals, SAMPLES):
        encoded = frame(clip, seconds, f"{name}{int(seconds)}")
        diff = np.abs(original - encoded)
        errors.append(diff.mean())
        dark_errors.append(diff[dark_mask].mean())
    print(f"{name:<20} 全图平均误差 {np.mean(errors):5.2f}   暗部平均误差 {np.mean(dark_errors):5.2f}   "
          f"码率 {clip.stat().st_size * 8 / 30 / 1e6:5.1f} Mbit/s")
