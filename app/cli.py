"""Command-line surface and role dispatch.

``argparse`` rather than typer, which dockerbundle uses: the surface here is a handful of
mutually exclusive role flags, not a command tree, and the daemon should not carry a
third-party CLI framework into a process running as SYSTEM.
"""

from __future__ import annotations

import sys

from app.output import emit
from app.roles import Role
from core.version import __version__
from ui.i18n import t

PROGRAM = "hostbridge"


def _build_parser():
    import argparse

    class Parser(argparse.ArgumentParser):
        """Route help and usage errors through :func:`emit`.

        argparse writes straight to ``sys.stdout``/``sys.stderr``. In a windowed build
        those are the null device, so ``--help`` and every argument error would vanish
        without a trace -- and before :func:`ensure_streams` existed, they crashed.
        """

        def _print_message(self, message, file=None):  # noqa: ARG002 - argparse hook
            if message:
                emit(message, error=file is not None and file is sys.stderr)

    parser = Parser(
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
    emit(t("cli.role_unavailable", role=role.value), error=True)
    return 4


def dispatch(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.lang:
        from ui import i18n

        i18n.set_language(args.lang)

    role = _role_of(args)

    if role is Role.VERSION:
        emit(f"{PROGRAM} {__version__}")
        return 0

    if role is Role.STATUS:
        return _status()

    if role is Role.REPAIR:
        return _repair(quiet=args.quiet)

    if role is Role.DAEMON_FOREGROUND:
        return _daemon_foreground()

    # Roles are wired up milestone by milestone; each arrives with its own module rather
    # than as a branch bolted onto this function.
    return _not_yet(role)


def _status() -> int:
    from daemon.status import describe

    for line in describe():
        emit(line)
    return 0


def _repair(*, quiet: bool = False) -> int:
    from daemon.repair import repair

    return repair(quiet=quiet)


def _daemon_foreground() -> int:
    """Run the resolver in this process until interrupted.

    Imported here rather than at module scope so that ``--version`` and ``--status`` do not
    pay for asyncio, dnslib and the resolver on every invocation.
    """
    import asyncio

    from daemon.runner import Runner, run_forever

    runner = Runner()
    try:
        return asyncio.run(run_forever(runner))
    except KeyboardInterrupt:
        # asyncio.run re-raises this after cancelling the loop; run_forever's finally has
        # already removed the rules, so there is nothing left to clean up here.
        return 130
