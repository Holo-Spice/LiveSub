"""Undo the CP936 mojibake PowerShell's Set-Content wrote over tests/test_logic.py.

Every non-ASCII literal in that file is one of the constants still present in the pre-mojibake
bytecode cache, so each damaged region is replaced by re-encoding the original to UTF-8 and
decoding it the way PowerShell's reader did.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSTS = [
    "今日は。明日も。", "今日は。", "明日も。", "。", "そうです。なるほど。", "そうです。", "なるほど。",
    "あ。い", "あ。", "い", "あい", "。そうです。", "そう", "です", "なる", "ほど",
    "你好", "再见", "错误", "原有字幕", "原有诊断", "视频.mkv", "停止",
]


def main() -> int:
    target = ROOT / "tests" / "test_logic.py"
    text = target.read_text(encoding="utf-8-sig")
    report = []
    for original in sorted(set(CONSTS), key=len, reverse=True):
        mojibake = original.encode("utf-8").decode("gbk", errors="replace")
        count = text.count(mojibake)
        if count:
            text = text.replace(mojibake, original)
        report.append(f"{count:2}  {original!r}  <-  {mojibake!r}")
    target.write_text(text, encoding="utf-8", newline="")
    leftovers = [f"{index}: {line}" for index, line in enumerate(text.splitlines(), 1) if any(ord(ch) > 0x2000 for ch in line)]
    (ROOT / "outputs" / "recovery-report.txt").write_text("\n".join(report + ["", "--- leftovers ---"] + leftovers), encoding="utf-8")
    print(f"replaced; leftover suspicious lines: {len(leftovers)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
