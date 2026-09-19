"""Replace the lines PowerShell's CP936 round trip damaged beyond byte-level recovery.

Each replacement is the literal still present in the pre-mojibake bytecode cache, or the byte
sequence the damaged fragment decodes to.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXES = {
    47: '    text = "今日は。明日も。"',
    48: '    sentences = ["今日は。", "明日も。"]',
    49: '    japanese_units = lambda value: [char for char in value if char not in "。"]',
    56: '    text = "そうです。なるほど。"',
    57: '    sentences = ["そうです。", "なるほど。"]',
    62: '        return ["そう", "です", "なる", "ほど"]',
    64: '    result = map_sentences(text, sentences, aligned(["そう", "です", "なる", "ほど"]), context_sensitive_units)',
    73: '    result = map_sentences("あ。い", ["あ。", "い"], aligned(["あい"]), lambda _: ["あい"])',
    75: '        ("あ。い", 0, 3, 0, 0.5)',
    77: '    result = map_sentences("。そうです。", ["。", "そうです。"], aligned(["そう", "です"]), lambda _: ["そう", "です"])',
    78: '    assert [part.text for part in result] == ["。そうです。"]',
    101: '        writer.append(3.1234, 4.5, "再见", {"source_language": "English"})',
    103: '            writer.append(4.0, 5.0, "错误", {})',
    134: '    code = ui_bridge.main(["offline", "--input", str(tmp_path / "视频.mkv"), "--output", str(tmp_path / "out.srt")])',
    194: '    code = ui_bridge.main(["offline", "--input", "视频.mkv", "--output", "out.srt"])',
    282: '            stop.set()  # the user presses 停止 while the models are still loading',
}


def main() -> int:
    target = ROOT / "tests" / "test_logic.py"
    lines = target.read_text(encoding="utf-8").splitlines()
    for number, replacement in FIXES.items():
        lines[number - 1] = replacement
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    remaining = [f"{index}: {line}" for index, line in enumerate(lines, 1) if any(ord(ch) > 0x2FFF or 0xE000 <= ord(ch) <= 0xF8FF for ch in line)]
    print(f"repaired {len(FIXES)} lines; suspicious lines left: {len(remaining)}")
    for entry in remaining:
        print(entry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
