"""Isolate what makes the transformers->sklearn->scipy DLL load stall in the UI path.

The launcher path parks inside LoadLibraryExW of a native extension (py-spy:
ntdll!RtlEnterCriticalSection under ntdll!LdrLoadDll), with no CPU and no I/O, freezing the
whole interpreter. A single-threaded probe of the same imports never stalls, so this walks
the structural differences of the UI path: a second thread already blocked on stdin, and
doing the import on a worker thread instead of the main thread. `--reader` is the shape that
reproduces it; the fix removed that thread from subtitle_cli.ui_bridge. A hard 150 s exit
keeps a stall cheap — note it cannot fire when the deadlock holds the GIL.

    .venv\\Scripts\\python.exe -u tools\\probe-loader-stall.py            # main thread, no reader
    .venv\\Scripts\\python.exe -u tools\\probe-loader-stall.py --reader   # + thread blocked on stdin
    .venv\\Scripts\\python.exe -u tools\\probe-loader-stall.py --worker   # import in a worker thread
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAULT = ROOT / "outputs" / "probe-loader-stall-fault.txt"


def hard_stop(seconds: float) -> None:
    threading.Thread(target=lambda: (time.sleep(seconds), os._exit(9)), daemon=True).start()


def load() -> None:
    started = time.monotonic()
    from transformers import AutoModelForMultimodalLM, AutoModelForTokenClassification, AutoProcessor, WhisperFeatureExtractor  # noqa: F401

    print(f"import_ok seconds={time.monotonic() - started:.3f}", flush=True)


def main() -> int:
    reader = "--reader" in sys.argv
    worker = "--worker" in sys.argv
    faulthandler.dump_traceback_later(60, repeat=True, file=open(FAULT, "w", encoding="utf-8"))
    hard_stop(150)
    if reader:
        # Exactly what subtitle_cli.ui_bridge starts before the model load: a daemon thread
        # parked on a blocking read of the stdin pipe the launcher keeps open.
        threading.Thread(target=lambda: sys.stdin.read(), name="fake-ui-input", daemon=True).start()
    started = time.monotonic()
    if worker:
        box: list = []
        thread = threading.Thread(target=lambda: box.append(load()), name="fake-model-load", daemon=True)
        thread.start()
        thread.join()
    else:
        load()
    print(f"mode reader={reader} worker={worker} total={time.monotonic() - started:.3f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
