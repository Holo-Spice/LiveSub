"""One task, one local llama-server process."""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

# The instruction is user editable. An empty box uses this balanced default for films,
# television and animation; keep the WPF display copy in SubtitleView.xaml.cs in sync.
DEFAULT_PROMPT = (
    "你是专业的影视与动画字幕译者。请将下面的对白翻译为自然、简洁的简体中文，适合电影、电视剧和动画字幕。"
    "准确保留原意、人物语气、情绪、礼貌程度和人物关系，避免逐字硬译或擅自补充；人名、地名和作品术语优先使用通行中文译名，"
    "无法确定时保留原文。只输出译文，不要解释、注音、括号说明或复述原文；不得输出繁体字。"
)


def llama_args(executable: Path, model: Path, device: str, port: int, slots: int = 1) -> list[str]:
    args = [str(executable), "-m", str(model)]
    if device == "gpu":
        args += ["-ngl", "99"]
    elif device == "cpu":
        args += ["--device", "none", "-ngl", "0"]
    else:
        raise ValueError("invalid translation device")
    # Offline runs several translations at once, so the server is started with matching
    # parallel slots. The context is per request either way, so raise it with the slot count
    # instead of letting llama-server split one small context between them.
    parallel = max(1, min(8, slots))
    args += ["-c", str(max(2048, 2048 * parallel)), "--parallel", str(parallel)]
    return args + ["--jinja", "--host", "127.0.0.1", "--port", str(port)]


def llama_environment(runtime_bin: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(runtime_bin), str(runtime_bin.parent / "lib" / "llvm" / "bin"), env.get("PATH", "")])
    return env


class Translator:
    """One task, one local llama-server process.

    ``translate`` is called from several threads in offline mode. ``httpx.Client`` is
    documented as thread safe, and the server side is what actually serialises the requests
    through its slots, so no extra lock is kept here.

    ``prompt`` is the instruction put in front of every sentence; it is the one place where
    the user can steer terminology, tone and output format, because the model is a plain
    instruction-following translator.
    """

    def __init__(self, executable: Path, model: Path, runtime_bin: Path, device: str, poll=None, slots: int = 1, prompt: str = ""):
        self.prompt = (prompt or "").strip() or DEFAULT_PROMPT
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.log = tempfile.TemporaryFile(mode="w+b")
        self.client = httpx.Client(timeout=120)
        try:
            self.process = subprocess.Popen(llama_args(executable, model, device, port, slots), env=llama_environment(runtime_bin), stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                # llama-server can take up to 120 seconds to publish /health, so the stop
                # command has to be collectable from this wait as well.
                if poll is not None:
                    poll()
                if self.process.poll() is not None:
                    raise RuntimeError(f"llama-server exited {self.process.returncode}: {self.log_tail()}")
                try:
                    if self.client.get(self.url + "/health", timeout=2).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            raise TimeoutError(f"llama-server startup timed out: {self.log_tail()}")
        except BaseException:
            self.close()
            raise

    def log_tail(self) -> str:
        self.log.seek(0)
        return self.log.read()[-2000:].decode("utf-8", "replace")

    def translate(self, sentence: str) -> str:
        # A blank line separates the instruction from the sentence, exactly as the built-in
        # prompt always did, so a user-supplied instruction behaves the same way.
        prompt = f"{self.prompt}\n\n{sentence}"
        response = self.client.post(self.url + "/v1/chat/completions", json={"messages": [{"role": "system", "content": ""}, {"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 512, "stream": False})
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"].strip()
        if not text:
            raise RuntimeError("llama-server returned empty translation")
        return text

    def close(self):
        process = getattr(self, "process", None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self.client.close()
        self.log.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
