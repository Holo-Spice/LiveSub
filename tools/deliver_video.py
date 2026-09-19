"""Subtitle delivery: burn into the video, or package it for a phone.

Two different jobs, so two different commands.

    # hard subtitles: the text is part of the picture, any player shows them
    python tools/deliver_video.py burn  outputs/offline/1.zh.srt --video testdata/1.mp4 --margin-v 120

    # soft subtitles in MKV: no re-encode, the phone player picks the track
    python tools/deliver_video.py mux   outputs/offline/1.zh.srt --video testdata/1.mp4

    # serve the folder so the phone can pull it over WiFi
    python tools/deliver_video.py serve outputs/offline

Burning is the only way subtitles survive an arbitrary player; muxing is instant and lossless and
is what to use when the phone has VLC/MX Player. `--encoder` chooses the H.264 encoder: `amf` uses
the AMD GPU (much faster, this machine's RX 9070 XT), `x264` is the safe default. Use `--margin-v`
to lift the new cues above subtitles that are already baked into the picture.
"""

from __future__ import annotations

import argparse
import re
import socket
import subprocess
import sys
from pathlib import Path

FFMPEG = Path(__file__).resolve().parents[1] / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
# Only the font family and the outline are set, and that is deliberate. `force_style` is applied in
# libass's script space (PlayResY, 288 by default), NOT in the video's pixels: on a 3840x2160 file
# `FontSize=48` renders text ~360 px tall and `MarginV=60` pushes it 700+ px up the screen, i.e. a
# subtitle across the middle of the picture. Measured on a 4K source, the untouched default is the
# correct one — 106 px tall (4.9 % of the frame) sitting 80 px above the bottom edge — while every
# explicit size made it worse. Pass --font-size / --margin-v only when you have a reason.
SUBTITLE_STYLE = "FontName=Microsoft YaHei,Outline=2,Shadow=0"
# libass script-space units, for the rare case where the default needs adjusting. Roughly: 16 is the
# default text height (about 5 % of the frame), 100 is a third of the way up the picture.
DEFAULT_FONT_SIZE = 16
DEFAULT_MARGIN_V = 20


def has_embedded_subtitles(video: Path) -> bool:
    """True when the container already carries a subtitle track.

    Burning on top of one would show two sets of text at once, and muxing would add a third. The
    stream list comes from `ffmpeg -i`, because this build ships no ffprobe.
    """
    return bool(re.search(r"Stream #\d+:\d+.*: Subtitle:", stream_listing(video)))


def stream_listing(video: Path) -> str:
    return subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(video)], capture_output=True, text=True).stderr


def scan(path: Path, video: Path | None = None):
    """List the media in a directory, pairing each video with a subtitle of the same name."""
    rows = []
    for entry in sorted(path.iterdir()):
        if entry.suffix.lower() not in (".srt", ".mkv", ".mp4", ".webm", ".mov", ".flv", ".ts"):
            continue
        if video is not None and entry.resolve() != video.resolve():
            continue
        size = entry.stat().st_size
        if entry.suffix.lower() == ".srt":
            rows.append((entry, f"{size:>10,} B  {count_cues(entry):>5} 条字幕"))
        else:
            rows.append((entry, f"{size / 1048576:>7.1f} MiB  {probe_duration(entry):>7}"))
    return rows


def count_cues(srt: Path) -> int:
    return len(re.findall(r"^\d+\s*$", srt.read_text(encoding="utf-8-sig"), flags=re.MULTILINE))


def probe_duration(video: Path) -> str:
    """Duration from `ffmpeg -i`, because this build ships no ffprobe."""
    result = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(video)], capture_output=True, text=True)
    match = re.search(r"Duration: (\d+:\d\d:\d\d\.\d+)", result.stderr)
    return match.group(1) if match else "?"


def subtitle_filter(srt: Path, style: str = SUBTITLE_STYLE) -> str:
    """The libass filter, with the path escaped the way FFmpeg's filter parser needs.

    A Windows path is `C:\\dir\\file.srt` to FFmpeg: the drive colon and every backslash must be
    escaped, and the whole argument is quoted so the commas inside the style list are not read as
    filter separators.
    """
    path = str(srt.resolve()).replace("\\", "/")
    path = path.replace(":", r"\:")
    return f"subtitles=filename='{path}':force_style='{style}'"


def burn(video: Path, srt: Path, output: Path, encoder: str, crf: str, style: str) -> int:
    args = [
        str(FFMPEG), "-y", "-nostdin", "-hide_banner",
        "-i", str(video),
        "-vf", subtitle_filter(srt, style),
        "-c:v", encoder, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-movflags", "+faststart",  # the phone can start playing before the file is fully copied
        "-metadata:s:v", "title=LiveSub burned subtitles",
        str(output),
    ]
    if encoder == "libx264":
        args[args.index("-pix_fmt"):args.index("-pix_fmt")] = ["-preset", "medium", "-crf", crf]
    elif encoder in ("h264_amf", "hevc_amf"):
        args[args.index("-pix_fmt"):args.index("-pix_fmt")] = ["-quality", "balanced", "-rc", "cqp", "-qp_i", crf, "-qp_p", crf]
    print("运行:", " ".join(args[:6]), "…")
    return subprocess.call(args)


def mux(video: Path, srt: Path, output: Path, language: str) -> int:
    """MKV with the subtitle as a real track: no video re-encode, no quality loss, no waiting."""
    args = [
        str(FFMPEG), "-y", "-nostdin", "-hide_banner",
        "-i", str(video), "-i", str(srt),
        "-map", "0", "-map", "1:0",
        "-c", "copy", "-c:s", "srt",
        f"-metadata:s:s:0", f"language={language}",
        "-disposition:s:0", "default",
        "-metadata:s:s:0", "title=简体中文",
        str(output),
    ]
    print("运行:", " ".join(args[:6]), "…")
    return subprocess.call(args)


def local_addresses() -> list[str]:
    """Every IPv4 this machine answers on, so the phone gets a URL that works on the same WiFi."""
    found = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))  # no packet is sent; this only asks the routing table
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        address = info[4][0]
        if address not in found and not address.startswith("127."):
            found.append(address)
    return found


def serve(directory: Path, port: int) -> int:
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(*args, directory=str(directory), **kwargs)
    with ThreadingHTTPServer(("0.0.0.0", port), handler) as server:
        print(f"目录: {directory}")
        for address in local_addresses():
            print(f"手机浏览器打开: http://{address}:{port}/")
        print("同一 WiFi 下有效；Ctrl+C 结束。手机浏览器长按文件即可下载。")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="deliver_video", description="把字幕烧进视频，或打包给手机")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (("burn", "把字幕烧进画面（硬字幕，任何播放器都能看）"), ("mux", "字幕作为内封轨道（软字幕，瞬间完成、无画质损失）")):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("srt", type=Path)
        item.add_argument("--video", type=Path, required=True)
        item.add_argument("--output", type=Path)
        item.add_argument("--language", default="chi")
        if name == "burn":
            item.add_argument("--encoder", default="libx264", choices=("libx264", "h264_amf", "hevc_amf", "libx265"))
            item.add_argument("--crf", default="20")
            item.add_argument("--style", default=SUBTITLE_STYLE)
            # libass script-space units (PlayResY=288), not pixels: 16 is the default text height.
            # Raise only to avoid subtitles already baked into the picture.
            item.add_argument("--font-size", type=int, default=None, help="字幕字号（libass 单位，默认约 16；不要按像素填）")
            item.add_argument("--margin-v", type=int, default=None, help="字幕距底部（libass 单位，默认约 20；填 100 会到画面三分之一处）")

    listing = sub.add_parser("list", help="列出目录里的视频与字幕")
    listing.add_argument("directory", type=Path, nargs="?", default=Path("outputs/offline"))

    server = sub.add_parser("serve", help="在本机开一个 HTTP 服务，手机浏览器下载")
    server.add_argument("directory", type=Path, nargs="?", default=Path("outputs/offline"))
    server.add_argument("--port", type=int, default=8765)

    args = parser.parse_args(argv)
    if args.command == "list":
        for entry, detail in scan(args.directory):
            print(f"{entry.name:<48} {detail}")
        return 0
    if args.command == "serve":
        return serve(args.directory, args.port)

    if not args.srt.is_file():
        print(f"找不到字幕文件: {args.srt}", file=sys.stderr)
        return 2
    if not args.video.is_file():
        print(f"找不到视频文件: {args.video}", file=sys.stderr)
        return 2
    default_suffix = ".hard.mp4" if args.command == "burn" else ".soft.mkv"
    output = args.output or args.video.with_suffix(default_suffix)
    if output.exists():
        print(f"输出已存在，先删除或改名: {output}", file=sys.stderr)
        return 2
    if args.command == "burn":
        style = args.style
        if args.font_size is not None:
            style += f",FontSize={args.font_size}"
        if args.margin_v is not None:
            style += f",MarginV={args.margin_v}"
        if has_embedded_subtitles(args.video):
            print("提示: 这个视频自带字幕轨道，烧进去会同时看到两套字幕。", file=sys.stderr)
        code = burn(args.video, args.srt, output, args.encoder, args.crf, style)
    else:
        code = mux(args.video, args.srt, output, args.language)
    if code == 0:
        print(f"完成: {output}  ({output.stat().st_size / 1048576:.1f} MiB)")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
