"""Command-line surface and role dispatch.

``argparse`` rather than typer, which dockerbundle uses: the surface here is a handful of
mutually exclusive role flags, not a command tree, and the daemon should not carry a
third-party CLI framework into a process running as SYSTEM.
"""

from __future__ import annotations

import sys

from app.roles import Role
from core.version import __version__
from ui.i18n import t

PROGRAM = "hostbridge"


def _build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=t("cli.description"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="store_true", help=t("cli.help_version"))
    parser.add_argument("--lang", metavar="CODE", default="", help=t("cli.help_lang"))
    parser.add_argument("--quiet", action="store_true", help=t("cli.help_quiet"))

    roles = parser.add_mutually_exclusive_group()
    roles.add_argument("--gui", action="store_true", help=t("cli.help_gui"))
    roles.add_argument("--service", action="store_true", help=t("cli.help_service"))
    roles.add_argument(
        "--daemon-foreground", action="store_true", help=t("cli.help_daemon_foreground")
    )
    roles.add_argument("--repair", action="store_true", help=t("cli.help_repair"))
    roles.add_argument("--status", action="store_true", help=t("cli.help_status"))
    roles.add_argument("--install-service", action="store_true", help=t("cli.help_install"))
    roles.add_argument("--uninstall-service", action="store_true", help=t("cli.help_uninstall"))
    return parser


def _role_of(args) -> Role:
    if args.version:
        return Role.VERSION
    for flag, role in (
        ("service", Role.SERVICE),
        ("daemon_foreground", Role.DAEMON_FOREGROUND),
        ("repair", Role.REPAIR),
        ("status", Role.STATUS),
        ("install_service", Role.INSTALL_SERVICE),
        ("uninstall_service", Role.UNINSTALL_SERVICE),
    ):
        if getattr(args, flag):
            return role
    return Role.GUI


def _not_yet(role: Role) -> int:
    sys.stderr.write(t("cli.role_unavailable", role=role.value) + "\n")
    return 4


def dispatch(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.lang:
        from ui import i18n

        i18n.set_language(args.lang)

    role = _role_of(args)

    if role is Role.VERSION:
        sys.stdout.write(f"{PROGRAM} {__version__}\n")
        return 0

    # Roles are wired up milestone by milestone; each arrives with its own module rather
    # than as a branch bolted onto this function.
    return _not_yet(role)
