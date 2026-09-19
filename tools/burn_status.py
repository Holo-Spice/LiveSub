# -*- coding: utf-8 -*-
r"""查看烧录进度。

    python tools\burn_status.py            # 看一次
    python tools\burn_status.py --watch    # 每 10 秒刷新，Ctrl+C 退出

数据来自正在运行的编码进程写入的 tools\_burn_status.txt，里面是 ffmpeg 自己的
`-progress` 输出：百分比、已编码时长、速度、剩余时间都是实测值，不是按大小猜的。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS = ROOT / "tools" / "_burn_status.txt"
TARGET_DEFAULT = Path(r"F:\迅雷下载\sone-054-4k\sone-054-4k.hard-zh.mp4")


def latest_progress(text: str) -> str | None:
    """The newest progress line: they are written with \\r, so the file holds them all."""
    matches = re.findall(r"\[[#-]+\][^\r\n]*", text)
    return matches[-1].strip() if matches else None


def report(target: Path) -> int:
    if not STATUS.exists():
        print("还没有进度文件；先运行 python tools\\start_burn.py 启动烧录。")
        return 1
    text = STATUS.read_text(encoding="utf-8", errors="replace")
    header = [line for line in text.splitlines() if line and not line.startswith("[")]
    line = latest_progress(text)
    size = target.stat().st_size / 1073741824 if target.exists() else 0.0
    print("--- 任务 ---")
    for item in header[:3]:
        print("  " + item)
    print("--- 进度 ---")
    print("  " + (line if line else "还没有进度数据"))
    print(f"  输出文件 {size:.2f} GiB  ({target})")
    if "退出码" in text:
        code = re.search(r"退出码 (\d+)", text)
        print(f"  已结束，退出码 {code.group(1) if code else '?'}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="burn_status")
    parser.add_argument("--watch", action="store_true", help="持续刷新")
    parser.add_argument("--every", type=float, default=10.0, help="刷新间隔秒数")
    parser.add_argument("--target", type=Path, default=TARGET_DEFAULT)
    args = parser.parse_args(argv)
    if not args.watch:
        return report(args.target)
    try:
        while True:
            print("\033[2J\033[H", end="")  # clear the console so the bar stays readable
            report(args.target)
            time.sleep(args.every)
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
