"""Runs the bridge protocol end to end against a real capture device.

The GUI cannot be driven by UI automation on this machine (the WPF combo boxes expose no
items), so this drives `subtitle_cli.ui_bridge` exactly the way the launcher does: the same
arguments, stdin/stdout/stderr as pipes, the stop command on stdin. It then reports which
protocol messages really arrived, which is what the UI renders.

    .venv\\Scripts\\python.exe -u tools\\probe_live_protocol.py [--device NAME] [--wait SECONDS]

The capture starts immediately and the sound is played after ``--wait`` seconds, which is the
case the long listening window exists for: the user pressing start before pressing play.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="virtual-audio-capturer")
    parser.add_argument("--wait", type=float, default=12.0, help="seconds of silence before the audio starts")
    parser.add_argument("--play-seconds", type=float, default=45.0)
    args = parser.parse_args()

    output = ROOT / "outputs" / "live" / "protocol-check.zh.srt"
    for path in (output, output.with_suffix(".jsonl")):
        path.unlink(missing_ok=True)

    command = [
        str(ROOT / ".venv" / "Scripts" / "python.exe"), "-u", "-m", "subtitle_cli.ui_bridge", "live",
        "--audio-device", args.device, "--source-lang", "ja", "--mt-model", "7b", "--mt-device", "gpu",
        "--mt-slots", "1", "--output", str(output),
    ]
    environment = dict(**__import__("os").environ, PYTHONIOENCODING="utf-8")
    process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=environment)

    player: subprocess.Popen | None = None
    messages: list[dict] = []
    started = time.monotonic()

    def play_after_delay() -> None:
        nonlocal player
        time.sleep(args.wait)
        print(f"[{time.monotonic() - started:6.1f}s] starting playback", flush=True)
        player = subprocess.Popen(
            [str(ROOT / "tools" / "downloads" / "ffmpeg-extracted" / "ffmpeg-9.0.1-essentials_build" / "bin" / "ffplay.exe"),
             "-hide_banner", "-loglevel", "error", "-nodisp", "-autoexit", "-loop", "0", str(ROOT / "testdata" / "source-ja.flac")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    threading.Thread(target=play_after_delay, daemon=True).start()

    def read() -> None:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                messages.append({"type": "unparsable", "raw": line[:200]})
                continue
            messages.append(message)
            if message["type"] == "listening":
                print(f"[{time.monotonic() - started:6.1f}s] listening waited={message['waited_seconds']} remaining={message['remaining_seconds']}", flush=True)
            elif message["type"] == "progress":
                print(f"[{time.monotonic() - started:6.1f}s] progress {message}", flush=True)
            elif message["type"] == "subtitle":
                print(f"[{time.monotonic() - started:6.1f}s] subtitle #{message['index']} {message['start_ms']}-{message['end_ms']} {message['text']}", flush=True)
            elif message["type"] == "status":
                print(f"[{time.monotonic() - started:6.1f}s] status {message.get('stage') or message.get('detail')}", flush=True)
            elif message["type"] == "finished":
                print(f"[{time.monotonic() - started:6.1f}s] finished {message}", flush=True)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()

    deadline = time.monotonic() + args.wait + args.play_seconds
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.5)

    print(f"[{time.monotonic() - started:6.1f}s] sending stop", flush=True)
    try:
        process.stdin.write('{"command":"stop"}\n')
        process.stdin.flush()
    except (BrokenPipeError, ValueError):
        pass
    try:
        process.wait(timeout=90)
    except subprocess.TimeoutExpired:
        process.kill()
    reader.join(timeout=5)
    if player is not None:
        player.terminate()

    kinds: dict[str, int] = {}
    for message in messages:
        kinds[message["type"]] = kinds.get(message["type"], 0) + 1
    print("\nmessage kinds:", kinds)
    print("listening messages:", [m for m in messages if m["type"] == "listening"])
    print("subtitles:", [f"#{m['index']} {m['text']}" for m in messages if m["type"] == "subtitle"][:6])
    print("finished:", [m for m in messages if m["type"] == "finished"])
    if output.is_file():
        print(f"srt lines: {len(output.read_text(encoding='utf-8').splitlines())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
