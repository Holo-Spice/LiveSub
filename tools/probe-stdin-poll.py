"""Can the launcher's stdin pipe be polled without parking a thread in a blocking read?

A thread blocked in a pipe read while another thread loads a native extension has been seen
to leave LoadLibraryExW waiting in ntdll forever (zero CPU, zero I/O). If a pipe can be made
non-blocking, the stop command can be collected from the existing polling loops instead of
from a thread that is always blocked.

    .venv\\Scripts\\python.exe -u probe-stdin-poll.py
"""

from __future__ import annotations

import os
import sys
import time

T0 = time.monotonic()


def log(message: str) -> None:
    print(f"[{time.monotonic() - T0:6.2f}s] {message}", flush=True)


def main() -> int:
    fd = sys.stdin.fileno()
    log(f"stdin fd={fd} blocking={os.get_blocking(fd)} isatty={sys.stdin.isatty()}")
    try:
        os.set_blocking(fd, False)
        log(f"set_blocking(False) ok -> blocking={os.get_blocking(fd)}")
    except OSError as exc:
        log(f"set_blocking(False) failed: {exc!r}")
        return 1

    buffer = b""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            time.sleep(0.25)
            continue
        except OSError as exc:
            log(f"os.read failed: {exc!r}")
            return 1
        if not chunk:
            log("os.read returned EOF")
            break
        buffer += chunk
        log(f"read {chunk!r}")
        if buffer.endswith(b"\n"):
            break
    log(f"buffer={buffer!r}")

    try:
        os.set_blocking(fd, True)
        log(f"set_blocking(True) ok -> blocking={os.get_blocking(fd)}")
    except OSError as exc:
        log(f"set_blocking(True) failed: {exc!r}")
        return 1
    log("STDIN_POLL_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
