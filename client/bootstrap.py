"""Getting a resolver running when there is not one.

Shared by the window and the console screen so both offer the same thing in the same order,
and so the decision of *how* to start it lives in one place:

1. if the service is installed, start it -- that is what the user chose, and starting it
   keeps the resolver alive after the window closes;
2. otherwise run the daemon in the foreground of an elevated process, which is the same
   thing the service would run and needs no installation.

Both paths raise a consent prompt. A dismissed prompt is reported as its own outcome rather
than as a failure: the user said no, and telling them something broke would be wrong.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from client.api import Bridge
from core import log
from ui.i18n import t

#: How long to wait for the daemon to publish its details after being started. Generous:
#: the first start also captures the upstream resolvers and registers the namespaces.
READY_TIMEOUT_SECONDS = 20.0
POLL_SECONDS = 0.4


@dataclass(frozen=True)
class StartResult:
    started: bool
    cancelled: bool = False
    message: str = ""

    @classmethod
    def ok(cls) -> StartResult:
        return cls(started=True, message=t("bootstrap.started"))

    @classmethod
    def refused(cls) -> StartResult:
        return cls(started=False, cancelled=True, message=t("bootstrap.cancelled"))

    @classmethod
    def failed(cls, reason: str) -> StartResult:
        return cls(started=False, message=t("bootstrap.failed", error=reason))


def start_resolver() -> StartResult:
    """Ask for the resolver to be started. Returns as soon as it is asked, not when ready."""
    from daemon import service
    from system.elevate import run_elevated

    try:
        current = service.state()
    except Exception:  # noqa: BLE001 - a broken service manager must not stop the fallback
        current = None

    if current is not None and current.supported and current.installed:
        outcome = run_elevated(["--service-start"], wait=True)
    else:
        outcome = run_elevated(["--daemon-foreground"], wait=False)

    if outcome.cancelled:
        return StartResult.refused()
    if not outcome.started:
        return StartResult.failed(outcome.reason)
    log.write("bootstrap: resolver start requested")
    return StartResult.ok()


def wait_until_online(timeout: float = READY_TIMEOUT_SECONDS) -> Bridge | None:
    """Poll until a daemon answers, or give up. Returns the connected bridge."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        bridge = Bridge.connect()
        if bridge.online:
            return bridge
        time.sleep(POLL_SECONDS)
    return None
