"""Terminal primitives for the in-place screens.

Ported from ``O:\\Projects\\ClaudeCodeAccountsSwitcher---python\\ui\\screen.py`` with one
addition -- a ``del`` key mapping, because a list of domains wants one. The rest is
deliberately unchanged: it already handles the Windows extended-key prefix, the POSIX
Escape-versus-arrow ambiguity, and restoring the terminal on the way out of an exception.

No dependency on Qt or anything else: the console build has to work on a machine where the
graphical stack cannot load at all.
"""

from __future__ import annotations

import os
import shutil
import sys

IS_WINDOWS = os.name == "nt"

ESC = "\033["
RESET = f"{ESC}0m"
DIM = f"{ESC}2m"
BOLD = f"{ESC}1m"
RED = f"{ESC}31m"
GREEN = f"{ESC}32m"
YELLOW = f"{ESC}33m"
CYAN = f"{ESC}36m"

ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
STD_OUTPUT_HANDLE = -11

#: A lone Escape arrives as "\x1b" and nothing else; an arrow as "\x1b[A". Only a short
#: wait tells them apart.
ESCAPE_TIMEOUT = 0.05

#: Falls back to a conservative width when the terminal will not say. A line longer than the
#: terminal wraps, which breaks the cursor-up arithmetic in :func:`draw` and turns the whole
#: screen into a scrolling mess -- the single most likely visual bug here.
FALLBACK_WIDTH = 100


def enable_ansi() -> None:
    """Turn on virtual terminal processing so the escape codes below mean something."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        # OR into the existing mode rather than assigning, and only when the handle really
        # is a console: GetConsoleMode returns 0 on a redirected one.
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except (AttributeError, OSError):
        pass


def width() -> int:
    try:
        return shutil.get_terminal_size().columns or FALLBACK_WIDTH
    except (OSError, ValueError):
        return FALLBACK_WIDTH


def interactive() -> bool:
    """True when both ends of the console are a terminal we can drive.

    Guarded against ``None`` and closed streams: a windowed PyInstaller build has no
    standard streams at all, and calling ``.isatty()`` on one is the crash this avoids.
    """
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _read_key_windows() -> str:
    import msvcrt

    char = msvcrt.getwch()
    if char in ("\x00", "\xe0"):
        second = msvcrt.getwch()
        return {
            "H": "up",
            "P": "down",
            "K": "left",
            "M": "right",
            "S": "del",
            "G": "home",
            "O": "end",
        }.get(second, "")
    if char == "\r":
        return "enter"
    if char == "\x1b":
        return "esc"
    if char == " ":
        return "space"
    if char in ("\x08", "\x7f"):
        return "backspace"
    if char == "\x03":
        # getwch does not raise on Ctrl+C, so it is synthesised here.
        raise KeyboardInterrupt
    return char.lower()


def _read_key_posix() -> str:
    import select
    import termios
    import tty

    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        char = sys.stdin.read(1)
        if char == "\x1b":
            ready, _, _ = select.select([sys.stdin], [], [], ESCAPE_TIMEOUT)
            if not ready:
                return "esc"
            if sys.stdin.read(1) != "[":
                return "esc"
            code = sys.stdin.read(1)
            if code == "3":
                sys.stdin.read(1)  # the trailing "~" of "\x1b[3~"
                return "del"
            return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(code, "")
        if char in ("\r", "\n"):
            return "enter"
        if char == " ":
            return "space"
        if char in ("\x7f", "\x08"):
            return "backspace"
        if char == "\x03":
            # Raw mode disables ISIG, so Ctrl+C arrives as a byte rather than a signal.
            raise KeyboardInterrupt
        return char.lower()
    finally:
        # In a finally so an exception never leaves the terminal in raw mode.
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def read_key() -> str:
    return _read_key_windows() if IS_WINDOWS else _read_key_posix()


def draw(lines: list[str], previous_height: int) -> int:
    """Repaint a block in place and return its new height.

    Moves the cursor up by the previous height once, then rewrites each line with an
    erase-to-end-of-line so a shorter line does not leave the tail of a longer one behind.
    """
    if previous_height:
        sys.stdout.write(f"{ESC}{previous_height}A")
    for line in lines:
        sys.stdout.write(f"\r{ESC}K{line}\n")
    sys.stdout.flush()
    return len(lines)


class Surface:
    """A block of lines redrawn in place, plus the cursor bookkeeping it needs.

    Keeping the height here rather than in a local of every screen loop is what lets one
    screen hand control to another: the caller that opens a nested screen -- or drops out to
    read a line of text -- calls :meth:`reset` afterwards, and the next paint writes a fresh
    block instead of scrolling over whatever was left behind.
    """

    def __init__(self) -> None:
        self._height = 0

    def paint(self, lines: list[str]) -> None:
        self._height = draw(lines, self._height)

    def reset(self) -> None:
        self._height = 0
