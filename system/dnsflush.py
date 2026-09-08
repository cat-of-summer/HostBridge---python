"""Clearing the operating system's DNS cache after a rule change.

Without this a name the user just switched on keeps returning whatever the resolver cached
for it a moment earlier, and the change looks broken for as long as the old TTL lasts.
"""

from __future__ import annotations

import os

from core import log
from system.run import CommandError, run

IS_WINDOWS = os.name == "nt"


def _flush_windows() -> bool:
    """``DnsFlushResolverCache`` through ctypes rather than ``ipconfig /flushdns``.

    The GUI is a windowed binary, and every subprocess it starts flashes a console window at
    the user. A direct call to the API avoids the process entirely -- and it is the same
    call ``ipconfig`` makes.
    """
    try:
        import ctypes

        # Declared, not inferred: an undeclared ctypes call guesses the signature, and
        # guessing right by luck is how it breaks on the next architecture.
        flush = ctypes.windll.dnsapi.DnsFlushResolverCache
        flush.argtypes = []
        flush.restype = ctypes.c_int
        return bool(flush())
    except (AttributeError, OSError, ValueError) as exc:
        log.warn(f"dnsflush: {exc!r}")
        return False


def _flush_posix() -> bool:
    """systemd-resolved caches; plain glibc does not, so a failure here is not an error."""
    try:
        completed = run(["resolvectl", "flush-caches"], timeout=10.0)
    except CommandError:
        return False
    return completed.ok


def flush() -> bool:
    """Flush the system resolver cache. Returns whether it is believed to have worked."""
    return _flush_windows() if IS_WINDOWS else _flush_posix()
