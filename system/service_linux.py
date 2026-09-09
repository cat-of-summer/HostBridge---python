"""Registering the resolver as a systemd unit.

The unit is generated rather than shipped as a file, because its ``ExecStart`` has to point
at wherever the user unpacked the archive. It is compared as text in a golden test, so an
edit to it is a deliberate change to a checked-in expectation rather than something noticed
in production.

Two lines carry weight:

* ``ExecStopPost`` runs ``--repair``. Belt and braces: even a ``SIGKILL`` followed by a unit
  stop leaves the resolution rules removed, so a killed resolver cannot leave a namespace
  pointing at a socket nobody holds.
* ``AmbientCapabilities=CAP_NET_BIND_SERVICE`` is what allows port 53 without the unit
  needing anything else from root -- though root is still required for ``resolvectl``, which
  is why the unit does not drop privileges.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from core import log
from system.run import CommandError, run

IS_LINUX = os.name == "posix"

SERVICE_NAME = "hostbridge"
UNIT_NAME = f"{SERVICE_NAME}.service"
UNIT_PATH = Path("/etc/systemd/system") / UNIT_NAME

TIMEOUT = 60.0

UNIT_TEMPLATE = """\
[Unit]
Description=HostBridge local DNS for virtual development domains
Documentation=https://github.com/cat-of-summer/HostBridge---python
After=network-online.target systemd-resolved.service
Wants=network-online.target

[Service]
Type=simple
ExecStart={executable} --service
ExecStopPost={executable} --repair --quiet
Restart=on-failure
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE CAP_NET_ADMIN
StateDirectory={state_directory}

[Install]
WantedBy=multi-user.target
"""


class ServiceError(Exception):
    """systemd refused a change, or is not present."""


@dataclass(frozen=True)
class ServicePlan:
    unit: str = ""
    commands: tuple[tuple[str, ...], ...] = ()

    @property
    def empty(self) -> bool:
        return not self.commands and not self.unit


def render_unit(executable: str, state_directory: str = SERVICE_NAME) -> str:
    """The unit file, exactly as it would be written."""
    return UNIT_TEMPLATE.format(executable=executable, state_directory=state_directory)


def plan_install(executable: str) -> ServicePlan:
    return ServicePlan(
        unit=render_unit(executable),
        commands=(
            ("systemctl", "daemon-reload"),
            ("systemctl", "enable", UNIT_NAME),
            ("systemctl", "start", UNIT_NAME),
        ),
    )


def plan_uninstall() -> ServicePlan:
    return ServicePlan(
        commands=(
            ("systemctl", "stop", UNIT_NAME),
            ("systemctl", "disable", UNIT_NAME),
            ("systemctl", "daemon-reload"),
        )
    )


def plan_start() -> ServicePlan:
    return ServicePlan(commands=(("systemctl", "start", UNIT_NAME),))


def plan_stop() -> ServicePlan:
    return ServicePlan(commands=(("systemctl", "stop", UNIT_NAME),))


# ---- the impure half ---------------------------------------------------------------


def available() -> bool:
    if not IS_LINUX:
        return False
    try:
        return run(("systemctl", "--version"), timeout=TIMEOUT).ok
    except CommandError:
        return False


def installed() -> bool:
    return UNIT_PATH.is_file()


def running() -> bool:
    if not IS_LINUX:
        return False
    try:
        completed = run(("systemctl", "is-active", UNIT_NAME), timeout=TIMEOUT)
    except CommandError:
        return False
    return completed.stdout.strip() == "active"


def execute(plan: ServicePlan, *, tolerate_failure: bool = False) -> None:
    if plan.empty:
        return
    if not IS_LINUX:
        raise ServiceError("systemd units are a Linux mechanism")

    if plan.unit:
        try:
            UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            UNIT_PATH.write_text(plan.unit, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise ServiceError(f"could not write {UNIT_PATH}: {exc}") from exc

    for argv in plan.commands:
        try:
            completed = run(argv, timeout=TIMEOUT)
        except CommandError as exc:
            raise ServiceError(str(exc)) from exc
        if not completed.ok and not tolerate_failure:
            raise ServiceError(completed.output or f"{argv[0]} exited {completed.returncode}")
        if not completed.ok:
            log.warn(f"service: {' '.join(argv)} -> {completed.output}")


def remove_unit() -> None:
    """Delete the unit file after the stop/disable commands have run."""
    try:
        UNIT_PATH.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ServiceError(f"could not remove {UNIT_PATH}: {exc}") from exc
