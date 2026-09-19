# -*- coding: utf-8 -*-
"""启动全片显卡烧录，并把进度同时写进 tools/_burn_status.txt，方便随时查看。

    python tools\start_burn.py            # 用默认设置启动
    python tools\start_burn.py --crf 20   # 改画质（AMF 的 QP，越小越好）
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
BURNER = ROOT / "tools" / "burn_with_progress.py"
STATUS = ROOT / "tools" / "_burn_status.txt"
DEFAULT_SRT = ROOT / "outputs" / "offline" / "subtitle4k.zh.srt"
DEFAULT_VIDEO = Path(r"F:\迅雷下载\sone-054-4k\sone-054-4k.mp4")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="start_burn")
    parser.add_argument("--srt", type=Path, default=DEFAULT_SRT)
    parser.add_argument("--video", type=Path, default=DEFAULT_VIDEO)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--encoder", default="hevc_amf", choices=("h264_amf", "hevc_amf", "libx264"))
    parser.add_argument("--crf", default="24")
    args = parser.parse_args(argv)

    output = args.output or args.video.with_suffix(".hard-zh.mp4")
    if output.exists():
        print(f"输出已存在，先删除或改名: {output}", file=sys.stderr)
        return 2

    command = [str(PYTHON), str(BURNER), str(args.srt), "--video", str(args.video),
               "--output", str(output), "--encoder", args.encoder, "--crf", args.crf]
    print("启动:", " ".join(command[1:]))
    # The progress lines use \r, so they are rewritten into the status file as they arrive; that
    # file is what to read for an answer to "how far along is it".
    with STATUS.open("w", encoding="utf-8") as status:
        status.write(f"开始 {time.strftime('%H:%M:%S')}  {args.encoder} QP/CRF {args.crf}\n")
        status.write(f"输出 {output}\n")
        status.flush()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", bufsize=1)
        for line in process.stdout:
            cleaned = line.rstrip("\n")
            if cleaned and not cleaned.startswith("["):
                # Non-progress lines (header and the final summary) are kept as log lines.
                status.write(cleaned + "\n")
                status.flush()
            elif cleaned:
                # Progress line: keep only the newest one, prefixed so it is easy to grep.
                status.seek(status.tell())
                status.write("\r" + cleaned)
                status.flush()
        code = process.wait()
        status.write(f"\n结束 {time.strftime('%H:%M:%S')} 退出码 {code}\n")
        status.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
