"""The record of what we changed, written before we change it.

``netstate.json`` exists so that ``--repair`` can undo our work when the daemon did not get
the chance to. Two ordering rules make it worth having:

* It is written **before** the policy is applied, so it always holds a superset of what was
  actually claimed. A crash halfway through applying leaves orphans that are still listed.
* ``clean_shutdown`` is only set on the way out. Finding it false on start-up means the last
  run died, and the leftovers are cleared before anything new is applied.

It carries no secret, so it is written unhardened -- and it is rewritten on every zone
change, which would otherwise mean an ``icacls`` process each time on Windows.
"""

from __future__ import annotations

import contextlib
import os
import platform
from dataclasses import asdict, dataclass, field
from typing import Any

from core.jsonio import read_json, write_json_atomic
from core.model import utc_now
from core.paths import netstate_file
from core.version import NETSTATE_VERSION, __version__


@dataclass
class NetState:
    version: int = NETSTATE_VERSION
    app_version: str = __version__
    pid: int = 0
    started_at: str = ""
    platform: str = ""
    mechanism: str = ""

    listen: list[str] = field(default_factory=list)
    port: int = 53
    upstreams: list[str] = field(default_factory=list)

    intent: list[str] = field(default_factory=list)
    """Namespaces we were about to claim. Always a superset of :attr:`confirmed`."""

    confirmed: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    clean_shutdown: bool = False

    @classmethod
    def start(cls, *, mechanism: str, listen: list[str], port: int, upstreams: list[str]):
        return cls(
            pid=os.getpid(),
            started_at=utc_now(),
            platform=platform.system().lower(),
            mechanism=mechanism,
            listen=list(listen),
            port=port,
            upstreams=list(upstreams),
        )


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def read() -> NetState | None:
    """The last recorded state, or ``None`` when there is nothing usable to read."""
    raw = read_json(netstate_file())
    if not isinstance(raw, dict):
        return None

    version = raw.get("version")
    if not isinstance(version, int) or version > NETSTATE_VERSION:
        # A file from a newer build: its fields may mean something else, so do not guess.
        # Repair falls back to scanning for our marker, which needs no file at all.
        return None

    state = NetState()
    state.version = version
    for key in ("app_version", "platform", "mechanism", "started_at"):
        value = raw.get(key)
        if isinstance(value, str):
            setattr(state, key, value)
    for key in ("listen", "upstreams", "intent", "confirmed", "links"):
        setattr(state, key, _strings(raw.get(key)))
    if isinstance(raw.get("pid"), int):
        state.pid = raw["pid"]
    if isinstance(raw.get("port"), int):
        state.port = raw["port"]
    state.clean_shutdown = bool(raw.get("clean_shutdown", False))
    return state


def write(state: NetState) -> None:
    write_json_atomic(netstate_file(), asdict(state))


def clear() -> None:
    with contextlib.suppress(OSError):
        netstate_file().unlink()


def pid_alive(pid: int) -> bool:
    """Whether the process that wrote a state file is still running.

    Used to tell "another daemon is up" from "the last one was killed and left rules
    behind", which are the same file but call for opposite actions.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # noqa: N806
        STILL_ACTIVE = 259  # noqa: N806
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; we simply may not signal it.
        return True
    return True
