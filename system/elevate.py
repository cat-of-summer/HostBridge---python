"""Running one of our own commands with administrator or root rights.

The elevation boundary itself is written down in :mod:`app.roles`; this module is only the
mechanism. Two rules shape it:

* **The executable carries no administrator manifest.** An ordinary launch must never raise
  a UAC prompt, so elevation is asked for per action -- installing the service, repairing,
  starting the resolver -- and never for opening the window.
* **This cannot go through :func:`system.run.run`.** That helper waits for the process and
  captures its output, which is exactly wrong for a daemon meant to outlive the caller, and
  ``ShellExecuteExW`` is not a ``subprocess`` call at all. So this module owns its own seam,
  :func:`_shell_execute` and :func:`_spawn_detached`, guarded by the same kind of autouse
  fixture in the test suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from core import log

IS_WINDOWS = os.name == "nt"

#: ShellExecuteExW asks for the process handle back, so the caller can wait for it.
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100

SW_HIDE = 0
SW_SHOWNORMAL = 1

#: The user dismissed the UAC prompt. Reported as its own outcome, because "you said no" is
#: not a failure and should not be shown as one.
ERROR_CANCELLED = 1223

WAIT_TIMEOUT_MS = 120_000


class ElevationError(Exception):
    """The elevated process could not be started."""


@dataclass(frozen=True)
class Elevation:
    started: bool
    cancelled: bool = False
    exit_code: int | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.started and self.exit_code in (None, 0)


def is_elevated() -> bool:
    """Whether this process already has the rights it would otherwise ask for."""
    if not IS_WINDOWS:
        return os.geteuid() == 0
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def own_executable(*, windowed: bool = True) -> tuple[str, list[str]]:
    """The command that re-runs this application, and any arguments it needs first.

    Frozen, there are two binaries side by side. The daemon is started from the **windowed**
    one deliberately: an elevated console build would park a console window on the user's
    desktop for as long as the resolver runs, and its output already reaches them through
    the log and the event stream.
    """
    if getattr(sys, "frozen", False):
        current = Path(sys.executable)
        if windowed:
            # hostbridge-cli.exe -> hostbridge.exe, and a no-op when already windowed.
            sibling = current.with_name(current.name.replace("-cli", ""))
            if sibling.exists():
                return str(sibling), []
        return str(current), []

    # From source: the interpreter plus main.py, so the developer flow works too.
    root = Path(__file__).resolve().parent.parent
    return sys.executable, [str(root / "main.py")]


# ---- the seams the tests replace ---------------------------------------------------


def _shell_execute(verb: str, file: str, parameters: str, show: int, wait: bool) -> Elevation:
    """The one ShellExecuteExW call. Windows only."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):  # noqa: N801 - the Win32 name
        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        )

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.hwnd = None
    info.lpVerb = verb
    info.lpFile = file
    info.lpParameters = parameters
    info.lpDirectory = None
    info.nShow = show

    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error() or ctypes.GetLastError()
        if code == ERROR_CANCELLED:
            return Elevation(started=False, cancelled=True, reason="the prompt was dismissed")
        return Elevation(started=False, reason=f"ShellExecuteExW failed with {code}")

    kernel32 = ctypes.windll.kernel32
    handle = info.hProcess
    try:
        if not wait or not handle:
            return Elevation(started=True)
        kernel32.WaitForSingleObject(handle, WAIT_TIMEOUT_MS)
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return Elevation(started=True, exit_code=int(code.value))
        return Elevation(started=True)
    finally:
        if handle:
            kernel32.CloseHandle(handle)


def _spawn_detached(argv: list[str]) -> Elevation:
    """Start a process that outlives this one. POSIX only.

    Not :func:`system.run.run`: that waits and captures, and a resolver started this way has
    to keep running after the window that asked for it is closed.
    """
    try:
        subprocess.Popen(  # noqa: S603 - argv is a list, never a shell string
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        return Elevation(started=False, reason=f"{argv[0]}: not found")
    except OSError as exc:
        return Elevation(started=False, reason=str(exc))
    return Elevation(started=True)


# ---- what callers use ---------------------------------------------------------------


def run_elevated(arguments: list[str], *, wait: bool, windowed: bool = True) -> Elevation:
    """Run this application again with ``arguments``, elevated.

    ``wait`` for an action that finishes and has a result worth reporting -- installing the
    service, repairing. Not for the daemon, which is supposed to keep running.
    """
    executable, prefix = own_executable(windowed=windowed)
    argv = [executable, *prefix, *arguments]

    if is_elevated():
        # Already elevated: no prompt, just start it. This is the path taken when the
        # console build is run from an administrator prompt.
        if wait:
            from system.run import CommandError, run

            try:
                completed = run(argv, timeout=WAIT_TIMEOUT_MS / 1000)
            except CommandError as exc:
                return Elevation(started=False, reason=str(exc))
            return Elevation(started=True, exit_code=completed.returncode)
        return _spawn_detached(argv)

    if IS_WINDOWS:
        parameters = subprocess.list2cmdline([*prefix, *arguments])
        result = _shell_execute("runas", executable, parameters, SW_HIDE, wait)
    else:
        result = _spawn_detached(["pkexec", *argv])
        if not result.started:
            # No polkit agent is common on a minimal window manager. Saying what to type is
            # more use than a failure the user cannot act on.
            result = Elevation(
                started=False,
                reason=f"could not elevate; run by hand: sudo {' '.join(argv)}",
            )

    log.write(f"elevate: {' '.join(arguments)} -> started={result.started} {result.reason}")
    return result
