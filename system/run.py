"""The single seam through which this application starts an external process.

Everything that shells out -- ``netsh``, ``icacls``, ``resolvectl``, ``sc``, ``pkexec`` --
goes through :func:`run`. That is not tidiness for its own sake: the autouse fixture in
``tests/conftest.py`` monkeypatches this one function to raise, which is what guarantees no
unit test can fire a real ``netsh`` at the developer's machine.

``CREATE_NO_WINDOW`` matters for the same reason the DNS cache is flushed through ctypes
rather than ``ipconfig``: the GUI is built as a windowed binary, and without the flag every
subprocess pops a console window in the user's face.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from core import log

DEFAULT_TIMEOUT = 30.0

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class CommandError(Exception):
    """A command could not be started, timed out, or returned non-zero under ``check``."""


@dataclass(frozen=True)
class Completed:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """Whichever stream carried the message, preferring stderr."""
        return self.stderr.strip() or self.stdout.strip()


def _spawn(
    argv: tuple[str, ...],
    *,
    capture_output: bool,
    text: bool,
    encoding: str,
    errors: str,
    timeout: float,
    input: str | None,  # noqa: A002 - mirrors the subprocess keyword
    creationflags: int,
):
    """The one call to subprocess in this application.

    Split out so the test suite can neutralise it with a single monkeypatch, no matter how
    a caller imported :func:`run`.
    """
    return subprocess.run(  # noqa: S603 - argv is a tuple, never a shell string
        argv,
        capture_output=capture_output,
        text=text,
        encoding=encoding,
        errors=errors,
        timeout=timeout,
        input=input,
        creationflags=creationflags,
    )


def run(
    argv: list[str] | tuple[str, ...],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    check: bool = False,
    input_text: str | None = None,
) -> Completed:
    """Run ``argv`` and capture its output. Never uses a shell."""
    argv = tuple(str(a) for a in argv)
    try:
        completed = _spawn(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            input=input_text,
            creationflags=_NO_WINDOW if os.name == "nt" else 0,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"{argv[0]}: not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"{argv[0]}: timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise CommandError(f"{argv[0]}: {exc}") from exc

    result = Completed(
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )

    if result.returncode != 0:
        log.write(f"run: {' '.join(argv)} -> {result.returncode} {result.output}")
    if check and not result.ok:
        raise CommandError(f"{argv[0]} exited {result.returncode}: {result.output}")
    return result
