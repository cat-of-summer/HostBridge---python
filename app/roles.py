"""Which role this process is playing, and which of them need elevation.

This module is the single written-down statement of the elevation boundary. Keeping it in
one place means a new entry point cannot quietly acquire administrative behaviour: adding a
role forces a decision about :data:`ELEVATED_ROLES`.
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    GUI = "gui"
    CONSOLE = "console"
    SERVICE = "service"
    DAEMON_FOREGROUND = "daemon-foreground"
    REPAIR = "repair"
    STATUS = "status"
    INSTALL_SERVICE = "install-service"
    UNINSTALL_SERVICE = "uninstall-service"
    SERVICE_START = "service-start"
    INSTALL_DESKTOP = "install-desktop"
    UNINSTALL_DESKTOP = "uninstall-desktop"
    VERSION = "version"


#: Roles that cannot do their job without administrator or root. The GUI is deliberately
#: absent, and stays absent even though opening the window now asks for a resolver straight
#: away: the prompt belongs to the child process it spawns for :data:`Role.DAEMON_FOREGROUND`,
#: which is the one that needs the privilege. The executable carries no administrator
#: manifest, so the window itself keeps running as the user -- which is what lets it read
#: that user's browser profiles and write to their home.
ELEVATED_ROLES = frozenset(
    {
        Role.SERVICE,
        Role.DAEMON_FOREGROUND,
        Role.REPAIR,
        Role.INSTALL_SERVICE,
        Role.UNINSTALL_SERVICE,
        Role.SERVICE_START,
    }
)


def needs_elevation(role: Role) -> bool:
    return role in ELEVATED_ROLES
