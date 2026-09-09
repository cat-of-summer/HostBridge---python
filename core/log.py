"""Rotating plain-text log shared by every role.

Logging never raises. A daemon that dies because its log directory went read-only would
take the user's name resolution down with it, which is a far worse outcome than a lost
line of diagnostics.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from pathlib import Path

from core.paths import log_file

MAX_BYTES = 1 << 20
KEEP = 3

#: Called with (level, message) for every line, in addition to the file. The daemon
#: attaches one so the window's log view has something to show; without it that view could
#: only ever display the interface's own start-up messages and then sit there looking
#: frozen, which is exactly how it looked.
_sinks: list[Callable[[str, str], None]] = []


def add_sink(sink: Callable[[str, str], None]) -> None:
    _sinks.append(sink)


def remove_sink(sink: Callable[[str, str], None]) -> None:
    with contextlib.suppress(ValueError):
        _sinks.remove(sink)


def _rotate(path: Path) -> None:
    with contextlib.suppress(OSError):
        if not path.exists() or path.stat().st_size <= MAX_BYTES:
            return
        for index in range(KEEP - 1, 0, -1):
            older = path.with_suffix(f"{path.suffix}.{index}")
            newer = path.with_suffix(f"{path.suffix}.{index + 1}")
            if older.exists():
                with contextlib.suppress(OSError):
                    newer.unlink()
                os.replace(older, newer)
        os.replace(path, path.with_suffix(f"{path.suffix}.1"))


def write(message: str, *, level: str = "info") -> None:
    try:
        path = log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{stamp} [{os.getpid()}] {level:<5} {message}\n")
    except OSError:
        pass

    # After the file, and each sink guarded separately: a subscriber that throws must not
    # cost the line on disk, and must not stop the next subscriber from seeing it.
    for sink in tuple(_sinks):
        with contextlib.suppress(Exception):
            sink(level, message)


def warn(message: str) -> None:
    write(message, level="warn")


def error(message: str) -> None:
    write(message, level="error")
