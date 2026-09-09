"""Linux name-resolution policy through systemd-resolved routing domains.

The Linux counterpart of :mod:`system.nrpt_win`, and it has to match NRPT's defining
property: **the machine's own DNS configuration is never replaced.** Losing HostBridge must
cost the user its own domains and nothing else.

Getting that right took running it. The obvious shape -- point the real network link at our
resolver and give it routing domains -- is wrong twice over, and both faults are silent:

* ``resolvectl dns eth0 127.0.0.1`` **replaces** that link's servers. The ones DHCP gave it
  are gone, so nothing is left to answer anything else.
* setting only routing domains on a link clears its ``DefaultRoute`` status, so resolved no
  longer considers it for names outside those domains.

Together they produce ``example.com: No appropriate name servers or networks for name
found`` -- the machine's general name resolution broken by a tool whose whole promise is
not to touch it. Measured, not reasoned about: our domains resolved perfectly while the
internet did not.

So we claim a link of our own instead. A dummy interface carries our resolver and our
routing domains, real links are never touched, and the result is additive in exactly the way
NRPT is. Three details are load-bearing:

* the link needs an **address**, or resolved reports ``Current Scopes: none`` and ignores it
  entirely -- an up-but-addressless dummy is invisible to it;
* the address is a link-local one on a ``/32``, so it cannot route or collide with anything;
* ``resolvectl revert`` plus deleting the interface is the whole undo, and deleting the
  interface alone would be enough.

Written as a planner -- the functions return command lines and do not run them -- so the
whole thing is compared as text in a golden test, on any platform, with nothing executed.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from core import log
from system.run import CommandError, run

IS_LINUX = os.name == "posix"

SYS_CLASS_NET = Path("/sys/class/net")

#: Our own interface. Named after the application so a puzzled administrator running
#: ``ip link`` can tell at a glance what put it there.
LINK_NAME = "hostbridge0"

#: Link-local (RFC 3927) and a single address, so it is unroutable by construction and
#: cannot clash with any network the machine is actually on. resolved needs *an* address to
#: give the link a DNS scope; it never needs to be reachable.
LINK_ADDRESS = "169.254.53.1/32"

TIMEOUT = 20.0


class ResolvedError(Exception):
    """systemd-resolved is absent or refused a change."""


@dataclass(frozen=True)
class Plan:
    links: tuple[str, ...]
    namespaces: tuple[str, ...]
    commands: tuple[tuple[str, ...], ...]

    @property
    def empty(self) -> bool:
        return not self.commands


def routing_domain(namespace: str) -> str:
    """Turn one of our namespaces into a resolvectl routing domain.

    ``.foo.test`` (our spelling of a wildcard suffix) becomes ``~foo.test``; an exact name
    becomes ``~name``. The tilde is what makes it *routing* rather than a search domain: it
    directs matching queries at this link's resolver without appending the suffix to bare
    names the user types.
    """
    return "~" + namespace.lstrip(".")


def plan_apply(namespaces: Iterable[str], servers: Sequence[str]) -> Plan:
    """Create our link if needed, and give it our resolver and our namespaces.

    ``ip link add`` is harmless to repeat -- it fails when the interface exists, and the
    executor tolerates that -- so applying a changed namespace set costs the same commands
    as the first time.
    """
    namespace_list = list(dict.fromkeys(namespaces))
    if not namespace_list:
        return Plan(links=(), namespaces=(), commands=())

    commands: list[tuple[str, ...]] = [
        ("ip", "link", "add", LINK_NAME, "type", "dummy"),
        ("ip", "link", "set", LINK_NAME, "up"),
        ("ip", "addr", "add", LINK_ADDRESS, "dev", LINK_NAME),
        ("resolvectl", "dns", LINK_NAME, *servers),
        (
            "resolvectl",
            "domain",
            LINK_NAME,
            *(routing_domain(ns) for ns in namespace_list),
        ),
    ]
    return Plan(
        links=(LINK_NAME,),
        namespaces=tuple(namespace_list),
        commands=tuple(commands),
    )


def plan_remove(links: Iterable[str] = ()) -> Plan:
    """Take our link away again.

    ``links`` is accepted and ignored beyond deciding whether there is anything to do: a
    state file written by an older build names the real interfaces it configured, and the
    only correct thing to do with those now is leave them alone -- reverting a link we no
    longer touch would discard settings that are not ours.
    """
    return Plan(
        links=(LINK_NAME,),
        namespaces=(),
        commands=(
            ("resolvectl", "revert", LINK_NAME),
            ("ip", "link", "del", LINK_NAME),
        ),
    )


# ---- the impure half ---------------------------------------------------------------


def available() -> bool:
    """Whether resolvectl is present and answering."""
    if not IS_LINUX:
        return False
    try:
        return run(["resolvectl", "--version"], timeout=TIMEOUT).ok
    except CommandError:
        return False


def link_present() -> bool:
    return (SYS_CLASS_NET / LINK_NAME).exists()


#: Commands whose failure means "already in the state we wanted". ``ip`` has no --idempotent
#: worth relying on across distributions, so the plan stays declarative and the executor
#: knows which refusals are not refusals: creating a link that exists, adding an address it
#: already has, and removing either when they are already gone.
TOLERATED = ("exists", "cannot find device", "no such device", "not found")


def _tolerable(argv: Sequence[str], output: str) -> bool:
    if argv[0] != "ip":
        return False
    lowered = output.lower()
    return any(phrase in lowered for phrase in TOLERATED)


def execute(plan: Plan) -> None:
    """Run a plan, stopping at the first refusal that actually means something."""
    if plan.empty:
        return
    for argv in plan.commands:
        try:
            completed = run(argv, timeout=TIMEOUT)
        except CommandError as exc:
            raise ResolvedError(str(exc)) from exc
        if completed.ok:
            continue
        if _tolerable(argv, completed.output):
            continue
        raise ResolvedError(completed.output or f"{argv[0]} exited {completed.returncode}")
    log.write(f"resolved: {len(plan.commands)} commands on {', '.join(plan.links)}")


#: resolved's own stub listeners. If ``/etc/resolv.conf`` does not name one of these, the C
#: library never consults resolved at all, and our routing domains -- correctly registered,
#: visible in ``resolvectl status`` -- are read by nobody.
STUB_ADDRESSES = ("127.0.0.53", "127.0.0.54")

RESOLV_CONF = Path("/etc/resolv.conf")


def stub_in_use() -> bool:
    """Whether ordinary programs actually resolve through systemd-resolved.

    Worth asking because the failure is invisible from our side: ``resolvectl query`` shows
    our domains resolving beautifully while ``getent hosts`` -- and therefore curl, and the
    browser -- returns nothing, because those go through the C library, which reads
    ``/etc/resolv.conf``. A machine whose ``resolv.conf`` was replaced by something else
    (a container runtime, a VPN client, a hand edit) has resolved running and unconsulted.
    """
    try:
        text = RESOLV_CONF.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True  # unreadable: assume the ordinary configuration rather than cry wolf
    servers = [
        line.split()[1]
        for line in text.splitlines()
        if line.strip().startswith("nameserver") and len(line.split()) > 1
    ]
    if not servers:
        return True
    return any(server in STUB_ADDRESSES for server in servers)
