"""What the resolver is doing, as plain lines.

Shared by ``--status`` and, later, by the console screen and the GUI status bar, so there is
one description of the daemon's state rather than three that drift apart.

Reads only files -- the state file and the store -- so it works whether or not a daemon is
running, which is exactly when a user reaches for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.config import DaemonSettings
from core.store import DomainStore
from daemon import netstate
from ui.i18n import t


@dataclass
class Status:
    running: bool = False
    pid: int = 0
    mechanism: str = ""
    listen: list[str] = field(default_factory=list)
    port: int = 53
    upstreams: list[str] = field(default_factory=list)
    claimed: list[str] = field(default_factory=list)
    stale: bool = False
    """A state file exists but its process is gone: rules may be left behind."""

    total: int = 0
    enabled: int = 0


def collect(store: DomainStore | None = None) -> Status:
    store = store or DomainStore()
    settings = DaemonSettings.load()
    snapshot = store.load()

    status = Status(
        listen=[settings.listen_address],
        port=settings.listen_port,
        total=len(snapshot.domains),
        enabled=sum(1 for domain in snapshot.domains if domain.enabled),
    )

    state = netstate.read()
    if state is not None:
        alive = netstate.pid_alive(state.pid)
        status.running = alive
        status.stale = not alive and bool(state.confirmed or state.intent)
        status.pid = state.pid
        status.mechanism = state.mechanism
        status.listen = state.listen or status.listen
        status.port = state.port
        status.upstreams = state.upstreams
        status.claimed = state.confirmed

    return status


def describe(status: Status | None = None) -> list[str]:
    """Render the status as lines for a terminal or a message box."""
    status = status or collect()
    lines: list[str] = []

    if status.running:
        lines.append(t("cli.status_running", pid=status.pid))
    elif status.stale:
        lines.append(t("cli.status_stale"))
    else:
        lines.append(t("cli.status_stopped"))

    listen = ", ".join(f"{address}:{status.port}" for address in status.listen)
    lines.append(t("cli.status_listen", listen=listen))
    lines.append(t("cli.status_upstreams", servers=", ".join(status.upstreams) or "-"))
    lines.append(
        t("cli.status_rules", count=len(status.claimed), mechanism=status.mechanism or "-")
    )
    lines.append(t("cli.status_domains", enabled=status.enabled, total=status.total))
    return lines
