r"""Registering the resolver as a Windows service.

Two decisions here are the ones that matter.

**The service is our own frozen exe, not a wrapper.** A plain executable registered with
``sc create`` and no SCM handshake fails with error 1053, "the service did not respond in a
timely fashion", because something has to answer ``StartServiceCtrlDispatcher``. NSSM would
mean shipping and licensing a third-party binary and giving up ``PRESHUTDOWN``, which is
what lets us take the resolution rules down before Windows stops. pywin32 is already a
dependency on this platform and its dispatcher handles both.

**The collected build is what makes this work at all.** A onefile bundle unpacks to a
temporary directory and re-execs a *child* process; the SCM would be tracking the parent
while our code runs in the child, so ``sc stop`` signals the wrong process and killing it
strands ``_MEI…`` directories. That is the reason ``build/hostbridge.spec`` collects rather
than folding into one file.

Written as a planner, like :mod:`system.nrpt_win`: the functions return command lines and do
not run them, so what would be registered is compared as text in a golden test on any
platform.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from core import log
from system.run import CommandError, run

IS_WINDOWS = os.name == "nt"

SERVICE_NAME = "HostBridge"
DISPLAY_NAME = "HostBridge local DNS"
DESCRIPTION = "Answers virtual development domains and forwards everything else."

#: Both are needed before we can bind or register a rule, and starting first would only
#: mean a retry loop.
DEPENDENCIES = "Dnscache/Tcpip"

#: Restart three times with a widening gap, then give up. The counter resets after a minute
#: of health, so a service that recovers is not left one crash from being abandoned.
FAILURE_ACTIONS = "restart/5000/restart/10000/restart/30000"
FAILURE_RESET_SECONDS = 60

TIMEOUT = 60.0


class ServiceError(Exception):
    """The service control manager refused a change."""


@dataclass(frozen=True)
class ServicePlan:
    commands: tuple[tuple[str, ...], ...]

    @property
    def empty(self) -> bool:
        return not self.commands


def plan_install(executable: str) -> ServicePlan:
    """Register the service.

    Note the trailing spaces in ``binPath= `` and friends: ``sc.exe`` requires the space
    *after* the equals sign and rejects the argument without it. It reads like a typo and
    is not one.
    """
    return ServicePlan(
        commands=(
            (
                "sc",
                "create",
                SERVICE_NAME,
                "binPath= ",
                f'"{executable}" --service',
                "start= ",
                "auto",
                "DisplayName= ",
                DISPLAY_NAME,
                "depend= ",
                DEPENDENCIES,
            ),
            ("sc", "description", SERVICE_NAME, DESCRIPTION),
            (
                "sc",
                "failure",
                SERVICE_NAME,
                "reset= ",
                str(FAILURE_RESET_SECONDS),
                "actions= ",
                FAILURE_ACTIONS,
            ),
        )
    )


def plan_uninstall() -> ServicePlan:
    return ServicePlan(
        commands=(
            ("sc", "stop", SERVICE_NAME),
            ("sc", "delete", SERVICE_NAME),
        )
    )


def plan_start() -> ServicePlan:
    return ServicePlan(commands=(("sc", "start", SERVICE_NAME),))


def plan_stop() -> ServicePlan:
    return ServicePlan(commands=(("sc", "stop", SERVICE_NAME),))


# ---- the impure half ---------------------------------------------------------------


def installed() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return run(("sc", "query", SERVICE_NAME), timeout=TIMEOUT).ok
    except CommandError:
        return False


def running() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        completed = run(("sc", "query", SERVICE_NAME), timeout=TIMEOUT)
    except CommandError:
        return False
    # The state line is localised, but the numeric code beside it is not -- which is why
    # this looks for "4" rather than for the word "RUNNING".
    return completed.ok and " 4 " in completed.stdout.replace("\t", " ")


def execute(plan: ServicePlan, *, tolerate_failure: bool = False) -> None:
    """Run a plan. ``sc stop`` on an already-stopped service is not an error worth raising."""
    if plan.empty:
        return
    if not IS_WINDOWS:
        raise ServiceError("Windows services are a Windows mechanism")

    for argv in plan.commands:
        try:
            completed = run(argv, timeout=TIMEOUT)
        except CommandError as exc:
            raise ServiceError(str(exc)) from exc
        if not completed.ok and not tolerate_failure:
            raise ServiceError(completed.output or f"{argv[1]} exited {completed.returncode}")
        if not completed.ok:
            log.warn(f"service: {' '.join(argv[:2])} -> {completed.output}")
