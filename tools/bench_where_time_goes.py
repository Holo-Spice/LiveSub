"""Where does the 4K burn time actually go? Isolate decode, subtitle rendering and each encoder."""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from burn_with_progress import FFMPEG, subtitle_filter

VIDEO = Path(r"F:\迅雷下载\sone-054-4k\sone-054-4k.mp4")
SRT = Path(r"C:\Users\29279\LiveSub\outputs\offline\subtitle4k.zh.srt")
WORK = Path(r"C:\Users\29279\LiveSub\.tmp-bench\fast")
WORK.mkdir(parents=True, exist_ok=True)
CLIP = ["-ss", "2400", "-t", "30"]  # a 30 s window mid-file
FILTER = subtitle_filter(SRT, "FontName=Microsoft YaHei,Outline=2,Shadow=0")


def run(tag, args, expect_output=True):
    target = WORK / f"{tag}.mp4"
    target.unlink(missing_ok=True)
    command = [str(FFMPEG), "-y", "-nostdin", "-hide_banner", "-loglevel", "error", *CLIP, "-i", str(VIDEO), *args]
    if expect_output:
        command.append(str(target))
    else:
        command += ["-f", "null", "-"]
    started = time.perf_counter()
    result = subprocess.run(command, capture_output=True, text=True)
    spent = time.perf_counter() - started
    if result.returncode != 0:
        tail = [l for l in result.stderr.strip().splitlines() if l.strip()][-1:]
        return tag, None, None, tail[0][:90] if tail else "?"
    size = target.stat().st_size / 1048576 if expect_output and target.exists() else 0.0
    return tag, spent, size, None


CASES = {
    # Decode alone: no filter, no huge output.
    "decode only (null)": ["-f", "null", "-"],
    # Subtitle rendering without any encoder pressure: measure the filter's own cost.
    "subs + null (filter only)": ["-vf", FILTER, "-f", "null", "-"],
    # Encoders, with the subtitle filter in place.
    "amf quality q22": ["-vf", FILTER, "-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "22", "-qp_p", "22", "-qp_b", "22", "-pix_fmt", "yuv420p", "-an"],
    "amf speed q22": ["-vf", FILTER, "-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "22", "-qp_p", "22", "-qp_b", "22", "-pix_fmt", "yuv420p", "-an"],
    "amf balanced q22": ["-vf", FILTER, "-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", "22", "-qp_p", "22", "-qp_b", "22", "-pix_fmt", "yuv420p", "-an"],
    "hevc amf speed q24": ["-vf", FILTER, "-c:v", "hevc_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "24", "-qp_p", "24", "-qp_b", "24", "-pix_fmt", "yuv420p", "-an"],
    # x264 ultrafast for reference: is the CPU path ever competitive here?
    "libx264 ultrafast": ["-vf", FILTER, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p", "-an"],
}

for tag, args in CASES.items():
    tag_id = tag.split(" (")[0].replace(" ", "_")
    name, spent, size, error = run(tag_id, args, expect_output=not args[-1] == "-")
    if spent is None and name != "decode_only":
        # The null-output cases end with "-f null -": rerun them without an output path.
        name, spent, size, error = run(tag_id, args, expect_output=False)
    if spent is None:
        print(f"{tag:<28} 失败: {error}")
        continue
    rate = 30 / spent
    print(f"{tag:<28} {spent:5.1f}s  {rate:5.2f}x 实时" + (f"  输出 {size:6.1f} MiB" if size else ""))
