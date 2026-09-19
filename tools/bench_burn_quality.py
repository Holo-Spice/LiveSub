"""Quality-vs-speed for the burn, measured on the user's own 4K source.

Answers "can we make it faster without wrecking the picture?" with PSNR/SSIM instead of opinion.
"""

import re
import subprocess
import time
from pathlib import Path

FFMPEG = r"C:\Users\29279\LiveSub\tools\ffmpeg\bin\ffmpeg.exe"
VIDEO = r"F:\迅雷下载\sone-054-4k\sone-054-4k.mp4"
SRT = Path(r"C:\Users\29279\LiveSub\outputs\offline\subtitle4k.zh.srt")
WORK = Path(r"C:\Users\29279\LiveSub\.tmp-bench\speed")
WORK.mkdir(parents=True, exist_ok=True)
FILTER = "subtitles=filename='" + str(SRT).replace("\\", "/").replace(":", "\\:") + "'"
CLIP = ["-ss", "3000", "-t", "30"]  # mid-file, 30 s
CASES = {
    "veryfast crf20": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"],
    "ultrafast crf20": ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20"],
    "ultrafast crf18": ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18"],
    "h264_amf q22": ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "22", "-qp_p", "22"],
}

for name, encoder in CASES.items():
    tag = name.replace(" ", "_")
    out = WORK / f"{tag}.mp4"
    out.unlink(missing_ok=True)
    args = [FFMPEG, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", *CLIP, "-i", VIDEO,
            "-vf", FILTER, *encoder, "-pix_fmt", "yuv420p", "-an", str(out)]
    started = time.perf_counter()
    result = subprocess.run(args, capture_output=True, text=True)
    spent = time.perf_counter() - started
    if result.returncode != 0:
        tail = [l for l in result.stderr.strip().splitlines() if l.strip()][-1:]
        print(f"{name:<16} 失败: {tail[0][:100] if tail else '?'}")
        continue
    # Compare against the same 30 s of the untouched source.
    quality = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "info", *CLIP, "-i", VIDEO, "-i", str(out),
         "-lavfi", "[0:v][1:v]psnr", "-f", "null", "-"],
        capture_output=True, text=True)
    psnr = re.search(r"average:([\d.]+)", quality.stderr)
    size = out.stat().st_size / 1048576
    full_hours = spent / 30 * 7233.7 / 3600
    print(f"{name:<16} 30s 用时 {spent:5.1f}s  PSNR {psnr.group(1) if psnr else '?':>6} dB  "
          f"码率约 {size / 30 * 8:6.1f} Mbit/s  |  全长 {full_hours:4.2f} 小时, 约 {size / 30 * 7233.7 / 1024:5.1f} GiB")
