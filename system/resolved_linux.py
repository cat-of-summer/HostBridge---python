"""Linux name-resolution policy through systemd-resolved routing domains.

The Linux counterpart of :mod:`system.nrpt_win`, and it works the same way for the same
reason: per-link **routing domains** (the ``~`` prefix) send only matching suffixes to our
resolver and leave everything else on its existing path. The machine's general DNS
configuration is never replaced, so losing HostBridge costs the user its own domains and
nothing more.

``resolvectl domain`` replaces the whole list for a link in one call, so a change is a
single command regardless of how many namespaces there are, and ``resolvectl revert`` is a
clean single undo.

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

#: Never a candidate: loopback carries no upstream, and these are virtual bridges whose
#: resolvers belong to containers rather than to the host.
SKIP_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "vmnet", "tun", "tap", "wg")

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


def plan_apply(
    links: Iterable[str], namespaces: Iterable[str], servers: Sequence[str]
) -> Plan:
    """Point ``links`` at ``servers`` for ``namespaces`` only."""
    link_list = list(dict.fromkeys(links))
    namespace_list = list(dict.fromkeys(namespaces))

    commands: list[tuple[str, ...]] = []
    for link in link_list:
        commands.append(("resolvectl", "dns", link, *servers))
        commands.append(
            ("resolvectl", "domain", link, *(routing_domain(ns) for ns in namespace_list))
        )

    return Plan(
        links=tuple(link_list),
        namespaces=tuple(namespace_list),
        commands=tuple(commands),
    )


def plan_remove(links: Iterable[str]) -> Plan:
    """Undo everything we set on ``links``.

    ``revert`` restores whatever the link had before, which is exactly right and is why
    nothing has to be remembered about the previous configuration.
    """
    link_list = list(dict.fromkeys(links))
    return Plan(
        links=tuple(link_list),
        namespaces=(),
        commands=tuple(("resolvectl", "revert", link) for link in link_list),
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


def links(excluded: Sequence[str] = ()) -> list[str]:
    """Candidate links: real interfaces that are up, minus the virtual ones."""
    if not SYS_CLASS_NET.is_dir():
        return []
    found: list[str] = []
    for entry in sorted(SYS_CLASS_NET.iterdir()):
        name = entry.name
        if name in excluded or name.startswith(SKIP_PREFIXES):
            continue
        try:
            state = (entry / "operstate").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if state == "up":
            found.append(name)
    return found


def execute(plan: Plan) -> None:
    """Run a plan, stopping at the first refusal."""
    if plan.empty:
        return
    for argv in plan.commands:
        try:
            completed = run(argv, timeout=TIMEOUT)
        except CommandError as exc:
            raise ResolvedError(str(exc)) from exc
        if not completed.ok:
            raise ResolvedError(completed.output or f"{argv[0]} exited {completed.returncode}")
    log.write(f"resolved: {len(plan.commands)} commands on {', '.join(plan.links)}")
