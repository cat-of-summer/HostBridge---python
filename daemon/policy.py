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
from ui.i18n import t


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
        """Claim our namespaces on a link of our own.

        No real interface is enumerated or touched any more. Configuring the machine's
        actual links replaced the servers DHCP gave them and cleared their default-route
        status, which broke general name resolution outright -- see
        :mod:`system.resolved_linux`. ``excluded_adapters`` therefore has nothing left to
        exclude here; it is kept because the setting is still meaningful elsewhere.
        """
        from system import resolved_linux

        try:
            resolved_linux.execute(resolved_linux.plan_apply(namespaces, servers))
        except resolved_linux.ResolvedError as exc:
            raise PolicyError(str(exc)) from exc

        if not resolved_linux.stub_in_use():
            # Not fatal, and not a reason to refuse: resolvectl will answer, so the rules
            # are real. But the C library will not consult them, so the browser sees
            # nothing -- and saying so beats letting the user hunt for it.
            log.warn(t("error.resolved_stub_unused", path=resolved_linux.RESOLV_CONF))
        return PolicyState(
            namespaces=tuple(namespaces), links=(resolved_linux.LINK_NAME,)
        )

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

        if resolved_linux.link_present():
            self.remove(PolicyState(links=(resolved_linux.LINK_NAME,)))


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
