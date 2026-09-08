"""Windows name-resolution policy: register our namespaces, and nothing else.

The NRPT is a table in the DNS Client that maps a namespace to a resolver, and it is
consulted **above** per-adapter settings. That is what makes it the right mechanism here: a
VPN or TUN adapter with its own DNS cannot pre-empt it, and because the target is
``127.0.0.1`` -- loopback, which never enters the routing table -- a tunnel never sees the
query either.

Crucially, the machine's general DNS configuration is not touched at all. Losing HostBridge
breaks only the namespaces it claimed; it can never cost the user name resolution.

**Cmdlets rather than direct registry writes.** The value layout under ``DnsPolicyConfig``
is thinly documented and includes a ``ConfigOptions`` bitmask that would have to be guessed;
``Add-DnsClientNrptRule`` gets it right by construction. The usual objection is cost, and it
does not survive measurement: a ``powershell -NoProfile`` round trip on the target machine
is 262 ms, and every change is batched into one invocation. NRPT churn is low anyway --
rules follow the *set of namespaces*, and enabling or disabling a domain does not change it,
because a disabled domain falls through to upstream inside our own resolver.

Note the branch: local rules live under
``HKLM\\SYSTEM\\CurrentControlSet\\Services\\Dnscache\\Parameters\\DnsPolicyConfig``. The
``SOFTWARE\\Policies\\...\\DNSClient`` branch is for rules pushed by Group Policy; writing
there would disguise our rules as policy and the first ``gpupdate`` would delete them.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from core import log
from system.run import CommandError, run

IS_WINDOWS = os.name == "nt"

#: Stamped on every rule we create. This is how our rules are found again after a crash,
#: independently of any state file, and how we know never to touch anyone else's.
MARKER = "HostBridge"

#: Windows PowerShell 5.1 is present on every supported system; ``pwsh`` is not, and is
#: absent on the target machine.
POWERSHELL = "powershell"

#: Long enough for a batch of rule changes plus PowerShell start-up, short enough that a
#: wedged call does not hold up daemon shutdown.
TIMEOUT = 60.0


class NrptError(Exception):
    """A rule change failed. The caller decides whether that is fatal."""


@dataclass(frozen=True)
class Plan:
    """What a change would do. Pure data, so it can be compared in a golden test."""

    add: tuple[str, ...]
    remove: tuple[str, ...]
    script: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.add and not self.remove


def _quote(value: str) -> str:
    """Single-quote for PowerShell, doubling any embedded quote.

    Namespaces reach here already validated by :mod:`core.match`, so this is belt and
    braces -- but a quoting helper that is only correct for trusted input is a trap for the
    next person who reuses it.
    """
    return "'" + str(value).replace("'", "''") + "'"


def _servers_literal(servers: Sequence[str]) -> str:
    return ",".join(_quote(server) for server in servers)


def _remove_statement(namespace: str) -> str:
    """Delete our rule for one namespace, leaving anyone else's alone.

    The ``Comment`` filter is not optional: a corporate Group Policy can have NRPT rules in
    the same table, and deleting one would break the user's VPN in a way they would never
    connect back to this application.
    """
    return (
        "Get-DnsClientNrptRule -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Comment -eq {_quote(MARKER)} -and "
        f"$_.Namespace -eq {_quote(namespace)} }} | "
        "Remove-DnsClientNrptRule -Force -ErrorAction SilentlyContinue"
    )


def _add_statement(namespace: str, servers: Sequence[str]) -> str:
    return (
        f"Add-DnsClientNrptRule -Namespace {_quote(namespace)} "
        f"-NameServers {_servers_literal(servers)} "
        f"-Comment {_quote(MARKER)} "
        f"-DisplayName {_quote(f'{MARKER} {namespace}')} "
        "-ErrorAction Stop"
    )


def plan_apply(
    namespaces: Iterable[str], servers: Sequence[str], present: Iterable[str]
) -> Plan:
    """Diff what should exist against what does, and script the difference.

    Pure: ``present`` is passed in rather than read, so the whole thing is golden-testable
    on Linux without a registry in sight. Applying the same desired set twice yields an
    empty plan, which is what makes the operation idempotent.
    """
    desired = list(dict.fromkeys(namespaces))
    existing = list(dict.fromkeys(present))

    to_add = [ns for ns in desired if ns not in existing]
    to_remove = [ns for ns in existing if ns not in desired]

    script: list[str] = []
    # Removals first: a namespace that is both removed and re-added -- because its servers
    # changed -- must not end up with two rules racing each other.
    script.extend(_remove_statement(ns) for ns in to_remove)
    script.extend(_add_statement(ns, servers) for ns in to_add)

    return Plan(add=tuple(to_add), remove=tuple(to_remove), script=tuple(script))


def plan_remove(namespaces: Iterable[str]) -> Plan:
    """Script the removal of our rules for ``namespaces``."""
    targets = list(dict.fromkeys(namespaces))
    return Plan(
        add=(),
        remove=tuple(targets),
        script=tuple(_remove_statement(ns) for ns in targets),
    )


def plan_remove_all() -> Plan:
    """Script the removal of every rule carrying our marker.

    The recovery path that does not depend on a state file: after a hard kill, whatever we
    created is still identifiable by its ``Comment``.
    """
    statement = (
        "Get-DnsClientNrptRule -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Comment -eq {_quote(MARKER)} }} | "
        "Remove-DnsClientNrptRule -Force -ErrorAction SilentlyContinue"
    )
    return Plan(add=(), remove=("*",), script=(statement,))


def to_argv(script: Sequence[str]) -> tuple[str, ...]:
    """Wrap a script into a single PowerShell invocation.

    One invocation for the whole batch: start-up is the dominant cost, and paying it once
    for twenty rules instead of twenty times is the difference between imperceptible and a
    visible stall.
    """
    return (
        POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "; ".join(script),
    )


# ---- the impure half ---------------------------------------------------------------


def read_present() -> list[str]:
    """The namespaces we currently have rules for. Empty when NRPT is unavailable."""
    if not IS_WINDOWS:
        return []
    argv = to_argv(
        [
            "Get-DnsClientNrptRule -ErrorAction SilentlyContinue | "
            f"Where-Object {{ $_.Comment -eq {_quote(MARKER)} }} | "
            "Select-Object -ExpandProperty Namespace"
        ]
    )
    try:
        completed = run(argv, timeout=TIMEOUT)
    except CommandError as exc:
        log.warn(f"nrpt: could not read current rules: {exc}")
        return []
    if not completed.ok:
        log.warn(f"nrpt: reading rules returned {completed.returncode}: {completed.output}")
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def execute(plan: Plan) -> None:
    """Run a plan. Raises :class:`NrptError` if PowerShell reported failure."""
    if plan.empty or not plan.script:
        return
    if not IS_WINDOWS:
        raise NrptError("NRPT rules are a Windows mechanism")

    try:
        completed = run(to_argv(plan.script), timeout=TIMEOUT)
    except CommandError as exc:
        raise NrptError(str(exc)) from exc

    if not completed.ok:
        raise NrptError(completed.output or f"powershell exited {completed.returncode}")
    log.write(f"nrpt: +{len(plan.add)} -{len(plan.remove)} rules")
