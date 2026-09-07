"""Writing text when there may be nowhere to write it.

A PyInstaller *windowed* build has no console attached: ``sys.stdout`` and ``sys.stderr``
are ``None``, and the first ``sys.stderr.write(...)`` anywhere in the process dies with
``AttributeError: 'NoneType' object has no attribute 'write'``. That is not hypothetical --
it is exactly how the first Windows build of ``hostbridge.exe`` failed on launch.

Two separate problems, and both have to be solved or the next one bites in a new place:

* **Nothing may write to a dead stream.** :func:`ensure_streams` binds the null device to
  whichever standard stream is missing, so arbitrary library code -- argparse, warnings,
  a stray ``print`` -- can no longer crash the process.
* **A message the user needs must still reach them.** Writing to the null device would
  make the windowed binary silently do nothing. :func:`emit` therefore falls back to a
  native message box when there is no console.

Deliberately not ``AttachConsole(ATTACH_PARENT_PROCESS)``: it appears to work and then
interleaves output with the shell's own prompt in an order nobody can predict. The console
build ``hostbridge-cli.exe`` exists precisely so that text output has a real home.
"""

from __future__ import annotations

import contextlib
import os
import sys

APP_TITLE = "HostBridge"

_MB_OK = 0x0
_MB_ICONERROR = 0x10
_MB_ICONINFORMATION = 0x40
_MB_SETFOREGROUND = 0x10000

#: Whether the process started with real standard streams. Captured once, before
#: :func:`ensure_streams` substitutes anything, because afterwards it is unknowable.
_has_console: bool | None = None


def has_console() -> bool:
    """True when stdout and stderr are real streams rather than PyInstaller's ``None``."""
    if _has_console is None:
        # ensure_streams has not run yet, so the streams still tell the truth.
        return sys.stdout is not None and sys.stderr is not None
    return _has_console


def ensure_streams() -> bool:
    """Guarantee ``sys.stdout``/``sys.stderr`` are writable. Idempotent.

    Returns whether a real console was present. Call this before anything else in the
    process, including argument parsing.
    """
    global _has_console

    if _has_console is not None:
        return _has_console

    _has_console = sys.stdout is not None and sys.stderr is not None
    if not _has_console:
        for name in ("stdout", "stderr"):
            if getattr(sys, name, None) is None:
                try:
                    handle = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
                except OSError:
                    continue
                setattr(sys, name, handle)
    return _has_console


def _message_box(message: str, *, error: bool) -> bool:
    """Show a native dialog. Returns whether it was actually displayed."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        # Declared rather than inferred: without argtypes ctypes guesses ``int`` for the
        # window handle, which is 32-bit and would truncate a real HWND on win64. NULL
        # survives that by luck, and relying on luck here is how it breaks later.
        box = ctypes.windll.user32.MessageBoxW
        box.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        box.restype = ctypes.c_int

        icon = _MB_ICONERROR if error else _MB_ICONINFORMATION
        box(None, str(message), APP_TITLE, _MB_OK | icon | _MB_SETFOREGROUND)
        return True
    except (AttributeError, ImportError, OSError, ValueError):
        # No user32, or a session with no window station. Nothing more we can do here.
        return False


def emit(message: str, *, error: bool = False) -> None:
    """Put ``message`` in front of the user by whatever route this build has."""
    if has_console():
        stream = sys.stderr if error else sys.stdout
        try:
            stream.write(message if message.endswith("\n") else message + "\n")
            stream.flush()
            return
        except (AttributeError, OSError, ValueError):
            pass

    if _message_box(message, error=error):
        return

    # Last resort: the streams are at worst the null device by now, so this cannot raise
    # the AttributeError this module exists to prevent.
    with contextlib.suppress(AttributeError, OSError, ValueError):
        (sys.stderr or sys.stdout).write(f"{message}\n")
