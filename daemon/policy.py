"""One interface over the two platform mechanisms for claiming a namespace.

Windows gets NRPT rules, Linux gets systemd-resolved routing domains. They are spelled
completely differently but they mean the same thing and they fail in the same places, so the
runner talks to this and never to either directly -- which also makes it the seam a test
replaces with a recorder.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field

from core import log
from system import dnsflush


class PolicyError(Exception):
    """The platform refused a change. Usually means "not elevated"."""


@dataclass
class PolicyState:
    """What is currently claimed, so shutdown and repair know what to undo."""

    namespaces: tuple[str, ...] = ()
    links: tuple[str, ...] = field(default_factory=tuple)


class Policy:
    """Base class, and the null implementation used where nothing is supported."""

    name = "none"

    def present(self) -> list[str]:
        return []

    def apply(self, namespaces: Sequence[str], servers: Sequence[str]) -> PolicyState:
        raise PolicyError("no name-resolution policy mechanism is available here")

    def remove(self, state: PolicyState) -> None:
        raise PolicyError("no name-resolution policy mechanism is available here")

    def remove_all(self) -> None:
        return None

    def flush(self) -> bool:
        return dnsflush.flush()


class WindowsPolicy(Policy):
    name = "nrpt"

    def present(self) -> list[str]:
        from system import nrpt_win

        return nrpt_win.read_present()

    def apply(self, namespaces: Sequence[str], servers: Sequence[str]) -> PolicyState:
        from system import nrpt_win

        plan = nrpt_win.plan_apply(namespaces, servers, present=self.present())
        try:
            nrpt_win.execute(plan)
        except nrpt_win.NrptError as exc:
            raise PolicyError(str(exc)) from exc
        return PolicyState(namespaces=tuple(namespaces))

    def remove(self, state: PolicyState) -> None:
        from system import nrpt_win

        try:
            nrpt_win.execute(nrpt_win.plan_remove(state.namespaces))
        except nrpt_win.NrptError as exc:
            raise PolicyError(str(exc)) from exc

    def remove_all(self) -> None:
        from system import nrpt_win

        try:
            nrpt_win.execute(nrpt_win.plan_remove_all())
        except nrpt_win.NrptError as exc:
            log.warn(f"policy: removing leftover rules failed: {exc}")


class ResolvedPolicy(Policy):
    name = "systemd-resolved"

    def __init__(self, excluded: Sequence[str] = ()) -> None:
        self._excluded = tuple(excluded)

    def apply(self, namespaces: Sequence[str], servers: Sequence[str]) -> PolicyState:
        from system import resolved_linux

        links = resolved_linux.links(self._excluded)
        if not links:
            raise PolicyError("no candidate network links were found")
        try:
            resolved_linux.execute(resolved_linux.plan_apply(links, namespaces, servers))
        except resolved_linux.ResolvedError as exc:
            raise PolicyError(str(exc)) from exc
        return PolicyState(namespaces=tuple(namespaces), links=tuple(links))

    def remove(self, state: PolicyState) -> None:
        from system import resolved_linux

        if not state.links:
            return
        try:
            resolved_linux.execute(resolved_linux.plan_remove(state.links))
        except resolved_linux.ResolvedError as exc:
            raise PolicyError(str(exc)) from exc

    def remove_all(self) -> None:
        from system import resolved_linux

        links = resolved_linux.links(self._excluded)
        if links:
            self.remove(PolicyState(links=tuple(links)))


def detect(excluded: Sequence[str] = ()) -> Policy:
    """Pick the mechanism this machine actually has.

    Returning the null :class:`Policy` rather than raising is deliberate: the resolver still
    works when it is queried directly, which is exactly the state ``--daemon-foreground``
    lands in when run without elevation, and telling the user that is more useful than
    refusing to start.
    """
    if os.name == "nt":
        return WindowsPolicy()

    from system import resolved_linux

    if resolved_linux.available():
        return ResolvedPolicy(excluded)

    log.warn("policy: systemd-resolved not found; names will resolve only on direct query")
    return Policy()
