"""Tighten filesystem permissions on files that must not be world-readable.

One file here is genuinely sensitive: ``daemon.json`` carries the control-API bearer
token, and any process that can read it can add a resolution rule. That file is written
with ``harden=True`` and :func:`harden_file` really does call ``icacls`` for it.

Everything else relies on :func:`harden_dir` over the state directory, applied once when
the daemon starts, because on Windows every file-level call is an external process and the
domain store is rewritten on every Docker event. This is also why ccas only ever hardened
directories on Windows -- that was the right instinct.
"""

from __future__ import annotations

import os
from pathlib import Path

from system.run import CommandError, run
from ui.i18n import t

IS_WINDOWS = os.name == "nt"

DIR_MODE = 0o700
FILE_MODE = 0o600


class PermissionWarning(Exception):
    """Permissions could not be tightened. Callers decide whether that is fatal."""


def _current_user() -> str:
    for key in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(key)
        if value:
            domain = os.environ.get("USERDOMAIN")
            if domain and key == "USERNAME":
                return f"{domain}\\{value}"
            return value
    try:
        return os.getlogin()
    except OSError as exc:
        raise PermissionWarning(t("error.no_user")) from exc


def _icacls(target: Path, grant: str) -> None:
    try:
        completed = run(["icacls", str(target), "/inheritance:r", "/grant:r", grant])
    except CommandError as exc:
        raise PermissionWarning(t("error.icacls_failed", path=target, error=exc)) from exc
    if not completed.ok:
        raise PermissionWarning(
            t(
                "error.icacls_returned",
                code=completed.returncode,
                path=target,
                output=completed.output,
            )
        )


def harden_dir(path: str | os.PathLike[str]) -> None:
    target = Path(path)
    if not target.exists():
        return
    if not IS_WINDOWS:
        os.chmod(target, DIR_MODE)
        return
    _icacls(target, f"{_current_user()}:(OI)(CI)F")


def harden_file(path: str | os.PathLike[str]) -> None:
    target = Path(path)
    if not target.exists():
        return
    if not IS_WINDOWS:
        os.chmod(target, FILE_MODE)
        return
    _icacls(target, f"{_current_user()}:F")


def is_world_readable(path: str | os.PathLike[str]) -> bool:
    if IS_WINDOWS:
        # Reading back an ACL to answer this properly needs pywin32, and the answer would
        # only ever be used for a warning. Reported as "no" rather than guessed.
        return False
    target = Path(path)
    if not target.exists():
        return False
    return bool(target.stat().st_mode & 0o077)
