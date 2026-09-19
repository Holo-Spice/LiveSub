"""How long is left on a running burn? MP4 headers are not rewritten while encoding, so the
estimate uses bytes written against the measured output rate (13.9 Mbit/s at CRF 20)."""

import subprocess
import sys
import time
from pathlib import Path

TARGET = Path(r"F:\迅雷下载\sone-054-4k\sone-054-4k.hard-zh.mp4")
TOTAL_SECONDS = 7233.7
MEASURED_MBIT = 13.9  # 30 s sample at veryfast/crf20 on this source
EXPECTED_GIB = MEASURED_MBIT * TOTAL_SECONDS / 8 / 1024


def main() -> int:
    if not TARGET.exists():
        print("还没有输出文件")
        return 1
    first = TARGET.stat().st_size
    time.sleep(20)
    second = TARGET.stat().st_size
    written = second / 1073741824
    per_second = (second - first) / 20 / 1048576
    print(f"已写入 {written:.2f} GiB / 预计全长 {EXPECTED_GIB:.1f} GiB  ({written / EXPECTED_GIB * 100:.1f}%)")
    print(f"当前写入速率 {per_second * 8:.1f} Mbit/s")
    if per_second <= 0:
        print("没有新增数据：编码可能已经结束，或在等 F 盘写入")
        return 0
    remaining_gib = max(0.0, EXPECTED_GIB - written)
    print(f"预计剩余约 {remaining_gib * 1024 / per_second / 3600:.2f} 小时")
    return 0


if __name__ == "__main__":
    sys.exit(main())
