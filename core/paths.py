"""Filesystem locations for bundled resources and for state.

Resources (``lang/``, ``assets/``) ship inside the PyInstaller bundle and are extracted to
``sys._MEIPASS`` at runtime, so every lookup has to go through :func:`resource_dir` rather
than ``__file__`` arithmetic.

State lives in two places, because the daemon and the GUI run as different principals. A
service running as SYSTEM or root has no meaningful ``Path.home()``, and the GUI cannot
write to the machine directory -- so anything the daemon owns goes to :func:`machine_home`
and only the user's own preferences go to :func:`user_home`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "hostbridge"

USER_HOME_ENV = "HOSTBRIDGE_HOME"
MACHINE_HOME_ENV = "HOSTBRIDGE_MACHINE_HOME"


def _bundle_root() -> Path | None:
    """Return the PyInstaller extraction directory, or ``None`` when running from source."""
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else None


def project_root() -> Path:
    """Return the source tree root (the directory holding ``main.py``)."""
    return Path(__file__).resolve().parent.parent


def resource_dir(name: str) -> Path:
    """Return the directory of a bundled resource set, e.g. ``"lang"`` or ``"assets"``."""
    root = _bundle_root() or project_root()
    return root.joinpath(*name.split("/"))


def user_home() -> Path:
    """Per-user preferences: language, theme, window geometry."""
    override = os.environ.get(USER_HOME_ENV)
    if override:
        return Path(override)
    return Path.home() / f".{APP_NAME}"


def machine_home() -> Path:
    """Daemon-owned state: the domain store, the applied-rules record, the control token."""
    override = os.environ.get(MACHINE_HOME_ENV)
    if override:
        return Path(override)
    if os.name == "nt":
        # ProgramData rather than a per-user directory: the service runs as SYSTEM and the
        # store has to survive a change of interactive user.
        base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
        return Path(base) / "HostBridge"
    return Path("/var/lib") / APP_NAME


def config_file() -> Path:
    return user_home() / "config.json"


def domains_file() -> Path:
    return machine_home() / "domains.json"


def netstate_file() -> Path:
    return machine_home() / "netstate.json"


def daemon_file() -> Path:
    return machine_home() / "daemon.json"


def log_file() -> Path:
    return machine_home() / f"{APP_NAME}.log"
