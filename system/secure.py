"""Tighten filesystem permissions on files that must not be world-readable.

Two files here are genuinely sensitive: ``daemon.json`` carries the control-API bearer
token, and any process that can read it can add a resolution rule. This is why
:func:`harden_file` really does call ``icacls`` on Windows, unlike its ccas ancestor where
the Windows branch was a no-op -- ccas only ever hardened directories.
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
