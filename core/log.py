"""Rotating plain-text log shared by every role.

Logging never raises. A daemon that dies because its log directory went read-only would
take the user's name resolution down with it, which is a far worse outcome than a lost
line of diagnostics.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from core.paths import log_file

MAX_BYTES = 1 << 20
KEEP = 3


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


def warn(message: str) -> None:
    write(message, level="warn")


def error(message: str) -> None:
    write(message, level="error")
