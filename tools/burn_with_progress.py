# -*- coding: utf-8 -*-
r"""烧字幕进视频，带实时进度条。

    python tools\burn_with_progress.py 字幕.srt --video 视频.mp4
    python tools\burn_with_progress.py 字幕.srt --video 视频.mp4 --encoder h264_amf --crf 22

进度来自 ffmpeg 自己的 `-progress` 输出，不是按时间猜的：`out_time_us` 是已编码的媒体时长，
`speed` 是当前编码速度。剩余时间按已编码比例换算，速度快时估算会跟着变准。

编码器：
    h264_amf  AMD 显卡硬件编码，最快（本机实测约 0.13x 实时，2 小时素材约 22 分钟），画质与
              libx264 veryfast 同级（PSNR 34.93 vs 34.94 dB）
    libx264   软件编码，最慢但兼容性最好
不推荐 libx264 ultrafast：暗部压缩痕迹明显变多（相邻像素跳变 0.71 -> 0.91），文件还大一倍。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
DEFAULT_STYLE = "FontName=Microsoft YaHei,Outline=2,Shadow=0"

ENCODERS = {
    # name: extra ffmpeg arguments. CRF maps to -crf for libx264 and to QP for the AMF encoders.
    # `-quality speed` is not a quality compromise here: measured on this 4K source, the quality,
    # balanced and speed tiers all produce the same error against the source (3.83 whole-frame,
    # 4.99 in dark areas) at the same 14.3 Mbit/s, while quality runs at 1.56x realtime and speed
    # at 2.70x. The tier only changed the time.
    "h264_amf": lambda quality: ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp",
                                 "-qp_i", quality, "-qp_p", quality, "-qp_b", quality],
    # HEVC at the same measured error, 9.5 Mbit/s instead of 14.3 and 3.60x realtime instead of
    # 2.70x. `-profile:v main` keeps it 8-bit 4:2:0, which is what Android devices decode.
    "hevc_amf": lambda quality: ["-c:v", "hevc_amf", "-quality", "speed", "-rc", "cqp",
                                 "-qp_i", quality, "-qp_p", quality, "-qp_b", quality,
                                 "-profile:v", "main"],
    "libx264": lambda quality: ["-c:v", "libx264", "-preset", "veryfast", "-crf", quality],
}


def probe_seconds(video: Path) -> float:
    """Source duration in seconds, from `ffmpeg -i` (this build ships no ffprobe)."""
    import re

    result = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(video)], capture_output=True, text=True)
    match = re.search(r"Duration: (\d+):(\d\d):(\d\d)\.(\d+)", result.stderr)
    if not match:
        return 0.0
    hours, minutes, seconds = (int(value) for value in match.groups()[:3])
    return hours * 3600 + minutes * 60 + seconds


def subtitle_filter(srt: Path, style: str) -> str:
    """FFmpeg's filter parser needs the drive colon and backslashes escaped; the quoting keeps the
    commas inside the style list from being read as filter separators."""
    path = str(srt.resolve()).replace("\\", "/").replace(":", "\\:")
    return f"subtitles=filename='{path}':force_style='{style}'"


def human(seconds: float) -> str:
    if seconds <= 0 or seconds != seconds:
        return "--:--"
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def bar(fraction: float, width: int = 28) -> str:
    """ASCII only: this runs in a GBK console where block characters raise UnicodeEncodeError."""
    filled = max(0, min(width, int(fraction * width)))
    return "#" * filled + "-" * (width - filled)


def main(argv=None) -> int:
    # A Windows console defaults to GBK here, which cannot encode the block characters a progress
    # bar wants; asking for UTF-8 keeps the bar itself out of the failure path.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="burn_with_progress", description="烧字幕进视频并显示进度")
    parser.add_argument("srt", type=Path)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--encoder", default="hevc_amf", choices=tuple(ENCODERS))
    parser.add_argument("--crf", default="24", help="libx264 是 CRF，AMF 是 QP；数值越小画质越好")
    parser.add_argument("--style", default=DEFAULT_STYLE)
    parser.add_argument("--audio-bitrate", default="192k")
    parser.add_argument("--limit", type=float, default=None, help="只编码前 N 秒（试跑用）")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出文件")
    args = parser.parse_args(argv)

    for path, label in ((args.srt, "字幕"), (args.video, "视频")):
        if not path.is_file():
            print(f"找不到{label}文件: {path}", file=sys.stderr)
            return 2

    output = args.output or args.video.with_suffix(".hard-zh.mp4")
    if output.exists() and not args.overwrite:
        print(f"输出已存在: {output}（加 --overwrite 覆盖）", file=sys.stderr)
        return 2
    if output.resolve() == args.video.resolve():
        print("输出不能和输入是同一个文件", file=sys.stderr)
        return 2

    total = probe_seconds(args.video)
    if args.limit:
        total = min(total, args.limit) if total else args.limit
    free = shutil.disk_usage(output.parent).free / 1073741824 if output.parent.exists() else 0.0
    print(f"输入   : {args.video}")
    print(f"字幕   : {args.srt}")
    print(f"输出   : {output}")
    print(f"编码器 : {args.encoder}  QP/CRF {args.crf}   源时长 {human(total)}   目标盘剩余 {free:.1f} GiB")
    if free and free < 2.0:
        print("警告：目标盘剩余空间不足 2 GiB，编码很可能中途失败。", file=sys.stderr)

    command = [
        str(FFMPEG), "-y", "-nostdin", "-hide_banner",
        "-loglevel", "error", "-stats_period", "1",
        "-progress", "pipe:1", "-nostats",
        "-i", str(args.video),
        "-vf", subtitle_filter(args.srt, args.style),
        *ENCODERS[args.encoder](str(args.crf)),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", args.audio_bitrate, "-ac", "2",
        "-movflags", "+faststart",
    ]
    if args.limit:
        command += ["-t", str(args.limit)]
    command.append(str(output))

    started = time.perf_counter()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    encoded = 0.0
    speed = 0.0
    frames = 0
    try:
        for line in process.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us":
                try:
                    encoded = int(value) / 1_000_000
                except ValueError:
                    pass
            elif key == "speed":
                try:
                    speed = float(value.rstrip("x"))
                except ValueError:
                    pass
            elif key == "frame":
                frames = int(value or 0)
            elif key == "progress":
                elapsed = time.perf_counter() - started
                fraction = min(1.0, encoded / total) if total else 0.0
                eta = (total - encoded) / speed if (speed > 0 and total) else 0.0
                size = output.stat().st_size / 1048576 if output.exists() else 0.0
                sys.stdout.write(
                    f"\r[{bar(fraction)}] {fraction * 100:5.1f}%  "
                    f"{human(encoded)}/{human(total)}  速度 {speed:5.2f}x  "
                    f"已用 {human(elapsed)}  剩余 {human(eta)}  {size:7.0f} MiB   ")
                sys.stdout.flush()
                if value == "end":
                    break
    except KeyboardInterrupt:
        process.terminate()
        print("\n已中断（Ctrl+C），删除未完成的输出。")
        process.wait(timeout=30)
        output.unlink(missing_ok=True)
        return 130

    code = process.wait()
    stderr = process.stderr.read() if process.stderr else ""
    print()
    if code != 0:
        tail = [line for line in stderr.strip().splitlines() if line.strip()][-4:]
        print("编码失败，ffmpeg 输出：", file=sys.stderr)
        for line in tail:
            print("  " + line, file=sys.stderr)
        output.unlink(missing_ok=True)
        return code

    spent = time.perf_counter() - started
    size = output.stat().st_size / 1073741824
    print(f"完成: {output}")
    print(f"  用时 {human(spent)}（{total / spent:.2f}x 实时）  帧数 {frames}  大小 {size:.2f} GiB")
    # The moov atom only lands at the end when +faststart is used, so this also proves the file is
    # finalised rather than merely large.
    check = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(output)], capture_output=True, text=True)
    import re

    duration = re.search(r"Duration: (\d+:\d\d:\d\d\.\d+)", check.stderr)
    streams = len(re.findall(r"Stream #\d+:\d+", check.stderr))
    print(f"  校验: 时长 {duration.group(1) if duration else '?'}  流数 {streams}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
