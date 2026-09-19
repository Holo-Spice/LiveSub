"""Does the AMD hardware encoder lose more than libx264 in dark, flat areas (banding/blocking)?"""

import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

FF = r"C:\Users\29279\LiveSub\tools\ffmpeg\bin\ffmpeg.exe"
SRC = r"F:\迅雷下载\sone-054-4k\sone-054-4k.mp4"
W = Path(r"C:\Users\29279\LiveSub\.tmp-bench\speed")


def frame(source, offset, tag):
    """`offset` is seconds into a clip that itself starts at 3000 s; the untouched source is seeked."""
    path = W / f"{tag}.png"
    if "sone-054" in str(source):
        command = [FF, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", str(3000 + offset), "-i", str(source)]
    else:
        command = [FF, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", str(offset), "-i", str(source)]
    subprocess.run(command + ["-frames:v", "1", str(path)], check=True)
    return np.asarray(Image.open(path).convert("L"), dtype=np.int16)


original = frame(SRC, 15, "cmp_orig")
dark_mask = original < 40
print(f"暗部像素占比 {dark_mask.mean() * 100:.1f}%")
for name in ("h264_amf_q22", "veryfast_crf20", "ultrafast_crf20"):
    encoded = frame(W / f"{name}.mp4", 15, f"cmp_{name}")
    diff = np.abs(original - encoded)
    dark_error = float(diff[dark_mask].mean()) if dark_mask.any() else float("nan")
    # Flat-area check: mean absolute difference between neighbouring pixels inside dark regions.
    flat = float(np.abs(np.diff(original, axis=1))[dark_mask[:, :-1]].mean())
    flat_enc = float(np.abs(np.diff(encoded, axis=1))[dark_mask[:, :-1]].mean())
    print(f"{name:<16} 全图平均误差 {diff.mean():5.2f}  暗部平均误差 {dark_error:5.2f}  "
          f"暗部相邻像素跳变 {flat:.2f} -> {flat_enc:.2f}")
