"""Which role this process is playing, and which of them need elevation.

This module is the single written-down statement of the elevation boundary. Keeping it in
one place means a new entry point cannot quietly acquire administrative behaviour: adding a
role forces a decision about :data:`ELEVATED_ROLES`.
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    GUI = "gui"
    SERVICE = "service"
    DAEMON_FOREGROUND = "daemon-foreground"
    REPAIR = "repair"
    STATUS = "status"
    INSTALL_SERVICE = "install-service"
    UNINSTALL_SERVICE = "uninstall-service"
    VERSION = "version"


#: Roles that cannot do their job without administrator or root. The GUI is deliberately
#: absent: a normal launch must never raise a UAC prompt, so the executable carries no
#: administrator manifest and the GUI re-launches itself elevated only for these.
ELEVATED_ROLES = frozenset(
    {
        Role.SERVICE,
        Role.DAEMON_FOREGROUND,
        Role.REPAIR,
        Role.INSTALL_SERVICE,
        Role.UNINSTALL_SERVICE,
    }
)


def needs_elevation(role: Role) -> bool:
    return role in ELEVATED_ROLES
