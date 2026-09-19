"""Line-oriented UI messages around the same CLI task and cleanup path."""

from __future__ import annotations

import io
import json
import os
import sys
import threading
from pathlib import Path

from .audio import SAMPLE_RATE
from .cli import StartupCancelled, arguments, run_task
from .pipeline import PipelineFailure


class StopRequests:
    """The launcher's stop command, collected without parking a thread on the pipe.

    The bridge used to own a daemon thread blocked in ``for line in sys.stdin``. Windows was
    then seen to leave ``LoadLibraryExW`` waiting inside ntdll for the whole 600 second
    startup budget with no CPU and no I/O at all (py-spy: RtlSleepConditionVariableCS under
    LdrLoadDll) whenever a native extension was loaded while that thread sat in the pipe
    read: a loader deadlock, not a slow load. The pipe is switched to non-blocking instead
    and drained from the loops that already poll for a stop, so no thread is ever blocked on
    stdin while a DLL is being loaded.

    End of input is not a stop: a closed, piped or empty stdin used to abort a live capture
    within a second and report "已停止" with zero subtitles. Only an explicit
    ``{"command":"stop"}`` stops the task.
    """

    def __init__(self, stream=None) -> None:
        self.stop = threading.Event()
        self._stream = stream if stream is not None else sys.stdin
        self._lock = threading.Lock()
        self._buffer = ""
        self._pollable = False
        try:
            os.set_blocking(self._stream.fileno(), False)
            self._pollable = True
        except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
            # Not a pollable handle: keep the old blocking reader thread so stop still works.
            threading.Thread(target=self._read_blocking, name="livesub-ui-input", daemon=True).start()

    def poll(self) -> None:
        """Collect whatever has already arrived. Never blocks."""
        if not self._pollable:
            return
        with self._lock:
            while True:
                try:
                    chunk = os.read(self._stream.fileno(), 4096)
                except BlockingIOError:
                    break
                except OSError:
                    self._pollable = False
                    break
                if not chunk:
                    self._pollable = False  # end of input is not a stop request
                    break
                self._buffer += chunk.decode("utf-8", "replace")
            while "\n" in self._buffer:
                line, _, self._buffer = self._buffer.partition("\n")
                self._accept(line)

    def _accept(self, line: str) -> None:
        try:
            # A byte-order mark from the parent would otherwise make the stop command
            # unparseable, and it would be ignored without a trace.
            if json.loads(line.lstrip("\ufeff")).get("command") == "stop":
                self.stop.set()
        except (ValueError, AttributeError):
            return

    def _read_blocking(self) -> None:
        try:
            for line in self._stream:
                self._accept(line)
        except OSError:
            return


def main(argv=None) -> int:
    args = arguments(argv)
    protocol = sys.stdout
    sys.stdout = sys.stderr
    lock = threading.Lock()
    requests = StopRequests()
    last_capture_second = -1
    capture_format: str | None = None
    capture_quality: str | None = None

    def send(kind: str, **fields) -> None:
        with lock:
            protocol.write(json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n")
            protocol.flush()

    def captured(samples: int, format: str | None = None, quality: str | None = None) -> None:
        nonlocal last_capture_second, capture_format, capture_quality
        if format is not None:
            capture_format = format
        if quality is not None:
            capture_quality = quality
        seconds = samples // SAMPLE_RATE
        if seconds > last_capture_second:
            last_capture_second = seconds
            send("status", stage="capturing", captured_seconds=seconds, capture_format=capture_format, capture_quality=capture_quality)

    def detail(label: str, seconds: float | None) -> None:
        # One item inside the model load. The stage line stays as it is and the UI shows this
        # underneath it, so a slow start reads "正在导入 wtpsplit · 已用 5 秒" instead of
        # freezing on a single word that cannot be told apart from a hang.
        send("status", detail=label, seconds=seconds)

    def progress(state: dict) -> None:
        # Offline mode only: measured audio seconds against the length of the file, plus the
        # throughput so far. Every field comes from the run itself; nothing is estimated
        # before there is a measurement to estimate from.
        send("progress", **state)

    def listening(state: dict) -> None:
        send("listening", **state)

    def status(stage: str) -> None:
        # The bridge owns the "listening" wording, because that one is not a pipeline stage:
        # it is the wait for the user's audio to actually start.
        if stage == "listening":
            return
        send("status", stage=stage)

    try:
        result = run_task(
            args,
            Path.cwd(),
            stop_requested=requests.stop,
            poll=requests.poll,
            on_status=status,
            on_capture=captured,
            on_detail=detail,
            on_progress=progress,
            on_listening=listening,
            on_subtitle=lambda index, start, end, text: send(
                "subtitle", index=index, start_ms=start, end_ms=end, text=text
            ),
        )
        message = {
            "completed": "生成完成",
            "stopped": "已停止，保留已保存字幕",
            "incomplete": "已停止，残余表达未确认完整；字幕不完整",
        }[result]
        send("finished", result=result, message=message)
        return 0
    except StartupCancelled as exc:
        # The load was abandoned mid-import, so its worker thread is still inside a native
        # call. Report the stop and leave without finalising the interpreter.
        send("finished", result="stopped", message=f"已停止：加载 {exc} 时中断，未生成新字幕")
        sys.stdout = protocol
        protocol.flush()
        os._exit(0)
    except Exception as exc:
        stage = exc.stage if isinstance(exc, PipelineFailure) else "output" if isinstance(exc, FileExistsError) else "startup"
        send("finished", result="failed", message=str(exc), stage=stage)
        return 1
    finally:
        sys.stdout = protocol


if __name__ == "__main__":
    raise SystemExit(main())
