"""Read the project's text files as strict UTF-8 and report the first CJK run found."""

import json
import pathlib
import sys

names = [
    "launcher/SubtitleOverlayWindow.xaml",
    "launcher/SubtitleOverlayWindow.xaml.cs",
    "launcher/SubtitleView.xaml",
    "launcher/SubtitleView.xaml.cs",
    "launcher/TaskRunner.cs",
    "src/subtitle_cli/cli.py",
    "src/subtitle_cli/pipeline.py",
]

report = {}
for name in names:
    raw = pathlib.Path(name).read_bytes()
    try:
        text = raw.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError as exc:
        text = raw.decode("gbk", "replace")
        encoding = f"NOT-UTF8: {exc}"
    run = []
    for char in text:
        if "\u4e00" <= char <= "\u9fff":
            run.append(char)
            if len(run) == 6:
                break
    report[name] = {
        "encoding": encoding,
        "content": run,
        "codepoints": [hex(ord(char)) for char in run],
        "has_bom": raw[:3] == b"\xef\xbb\xbf",
    }

pathlib.Path("outputs/encoding-check.json").write_text(json.dumps(report, ensure_ascii=True, indent=1), encoding="utf-8")
sys.stdout.write("written\n")
