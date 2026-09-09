"""Installing, removing and inspecting the background service.

One interface over two mechanisms, the same shape as :mod:`daemon.policy`, so the caller --
the CLI, and later a button in the window -- never branches on the platform itself.

Everything here needs administrator or root. The caller is expected to have arrived through
:func:`system.elevate.run_elevated`, and :func:`ensure_elevated` is the check that says so
plainly rather than letting the platform return a confusing refusal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from core import log
from system.elevate import is_elevated, own_executable
from ui.i18n import t


class ServiceUnavailable(Exception):
    """No service mechanism exists on this machine."""


class NotElevated(Exception):
    """The action needs rights this process does not have."""


@dataclass(frozen=True)
class ServiceState:
    supported: bool
    installed: bool = False
    running: bool = False
    mechanism: str = ""


def _backend():
    if os.name == "nt":
        from system import service_win

        return service_win
    from system import service_linux

    if not service_linux.available():
        raise ServiceUnavailable("systemd was not found")
    return service_linux


def mechanism() -> str:
    return "windows-service" if os.name == "nt" else "systemd"


def state() -> ServiceState:
    """What the service manager currently reports. Never raises."""
    try:
        backend = _backend()
    except ServiceUnavailable:
        return ServiceState(supported=False)
    return ServiceState(
        supported=True,
        installed=backend.installed(),
        running=backend.running(),
        mechanism=mechanism(),
    )


def ensure_elevated() -> None:
    if not is_elevated():
        raise NotElevated(t("service.needs_elevation"))


def install() -> None:
    """Register the service and start it."""
    ensure_elevated()
    backend = _backend()
    executable, prefix = own_executable(windowed=True)
    if prefix:
        # Running from source, where the command is an interpreter plus a script. A service
        # registered against a checkout would break the moment it moved, so this refuses
        # rather than registering something that will rot.
        raise ServiceUnavailable(t("service.needs_build"))

    if backend.installed():
        log.write("service: already installed, reinstalling")
        uninstall()

    backend.execute(backend.plan_install(executable))
    log.write(f"service: installed at {executable}")


def uninstall() -> None:
    ensure_elevated()
    backend = _backend()
    # Stopping a service that is not running is not a failure worth raising.
    backend.execute(backend.plan_uninstall(), tolerate_failure=True)
    remove_unit = getattr(backend, "remove_unit", None)
    if remove_unit is not None:
        remove_unit()
    log.write("service: removed")


def start() -> None:
    ensure_elevated()
    backend = _backend()
    backend.execute(backend.plan_start())


def stop() -> None:
    ensure_elevated()
    backend = _backend()
    backend.execute(backend.plan_stop(), tolerate_failure=True)
