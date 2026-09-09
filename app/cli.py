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
    parser.add_argument(
        "--owner-pid", metavar="PID", type=int, default=0, help=t("cli.help_owner_pid")
    )

    roles = parser.add_mutually_exclusive_group()
    roles.add_argument("--gui", action="store_true", help=t("cli.help_gui"))
    roles.add_argument("--console", action="store_true", help=t("cli.help_console"))
    roles.add_argument("--service", action="store_true", help=t("cli.help_service"))
    roles.add_argument(
        "--daemon-foreground", action="store_true", help=t("cli.help_daemon_foreground")
    )
    roles.add_argument("--repair", action="store_true", help=t("cli.help_repair"))
    roles.add_argument("--status", action="store_true", help=t("cli.help_status"))
    roles.add_argument("--install-service", action="store_true", help=t("cli.help_install"))
    roles.add_argument("--uninstall-service", action="store_true", help=t("cli.help_uninstall"))
    roles.add_argument("--service-start", action="store_true", help=t("cli.help_service_start"))
    roles.add_argument(
        "--install-desktop", action="store_true", help=t("cli.help_install_desktop")
    )
    roles.add_argument(
        "--uninstall-desktop", action="store_true", help=t("cli.help_uninstall_desktop")
    )
    return parser


def _role_of(args) -> Role:
    if args.version:
        return Role.VERSION
    for flag, role in (
        ("console", Role.CONSOLE),
        ("service", Role.SERVICE),
        ("daemon_foreground", Role.DAEMON_FOREGROUND),
        ("repair", Role.REPAIR),
        ("status", Role.STATUS),
        ("install_service", Role.INSTALL_SERVICE),
        ("uninstall_service", Role.UNINSTALL_SERVICE),
        ("install_desktop", Role.INSTALL_DESKTOP),
        ("uninstall_desktop", Role.UNINSTALL_DESKTOP),
        ("service_start", Role.SERVICE_START),
    ):
        if getattr(args, flag):
            return role

    # No role asked for. A console build started from a terminal -- which is what a double
    # click on hostbridge-cli.exe looks like -- opens the screen rather than printing and
    # vanishing. The windowed build has no console at all and goes to the GUI.
    from ui.screen import interactive

    return Role.CONSOLE if interactive() else Role.GUI


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

    if role is Role.CONSOLE:
        from ui.console import main as console_main

        return console_main()

    if role is Role.GUI:
        return _gui()

    if role is Role.STATUS:
        return _status()

    if role is Role.REPAIR:
        return _repair(quiet=args.quiet)

    if role is Role.DAEMON_FOREGROUND:
        return _daemon_foreground(owner_pid=args.owner_pid)

    if role is Role.SERVICE:
        return _service()

    if role in (Role.INSTALL_SERVICE, Role.UNINSTALL_SERVICE):
        return _manage_service(install=role is Role.INSTALL_SERVICE)

    if role is Role.SERVICE_START:
        return _start_service()

    if role in (Role.INSTALL_DESKTOP, Role.UNINSTALL_DESKTOP):
        return _manage_desktop(install=role is Role.INSTALL_DESKTOP)

    # Roles are wired up milestone by milestone; each arrives with its own module rather
    # than as a branch bolted onto this function.
    return _not_yet(role)


def _manage_desktop(*, install: bool) -> int:
    """Add or remove the menu entry and the autostart entry.

    Deliberately not elevated: both files live in the user's own home, and asking for a
    password to add a shortcut would be the wrong trade. It is also why this is refused
    outright elsewhere rather than emulated -- Windows has its own mechanisms and a
    half-working imitation would be worse than an honest no.
    """
    if sys.platform == "win32":
        emit(t("desktop.windows_unsupported"), error=True)
        return 4

    from system import desktop_linux
    from system.elevate import own_executable

    executable, prefix = own_executable(windowed=True)
    command = " ".join([executable, *prefix]) if prefix else executable

    try:
        if install:
            plan = desktop_linux.plan_install(command)
            written = desktop_linux.execute(plan)
            for path in written:
                emit(t("desktop.installed", path=path))
        else:
            removed = desktop_linux.execute(desktop_linux.plan_uninstall())
            if not removed:
                emit(t("desktop.nothing_to_remove"))
            for path in removed:
                emit(t("desktop.removed", path=path))
    except OSError as exc:
        emit(t("desktop.failed", error=exc), error=True)
        return 5
    return 0


def _service() -> int:
    """Run under the platform's service manager.

    On Windows that means handing the process to the SCM, which has to happen within about
    thirty seconds of start or the service fails with error 1053. On Linux systemd simply
    starts the process, so this is the ordinary daemon path.
    """
    if sys.platform != "win32":
        # No owner pid here, deliberately: a service must outlive every window.
        return _daemon_foreground()

    try:
        from daemon.winservice import run as service_run
    except ImportError as exc:
        emit(t("service.pywin32_missing", error=exc), error=True)
        return 4
    return service_run()


def _start_service() -> int:
    """Start an already-installed service. The elevated half of the window's button."""
    from daemon import service

    try:
        service.start()
    except service.NotElevated as exc:
        emit(str(exc), error=True)
        return 5
    except Exception as exc:  # noqa: BLE001 - the platform's refusal is what matters
        emit(t("service.failed", error=exc), error=True)
        return 5
    return 0


def _manage_service(*, install: bool) -> int:
    from daemon import service

    try:
        if install:
            service.install()
            emit(t("service.installed"))
        else:
            service.uninstall()
            emit(t("service.removed"))
    except service.NotElevated as exc:
        emit(str(exc), error=True)
        return 5
    except service.ServiceUnavailable as exc:
        emit(str(exc), error=True)
        return 4
    except Exception as exc:  # noqa: BLE001 - the platform's refusal is what matters
        emit(t("service.failed", error=exc), error=True)
        return 5
    return 0


def _gui() -> int:
    """Open the desktop window.

    Imported here rather than at module scope: PySide6 costs a noticeable fraction of a
    second to import, and --version, --status and the daemon must not pay for it.
    """
    try:
        from ui.app import run
    except ImportError as exc:
        emit(t("error.gui_unavailable", error=exc), error=True)
        return 4
    return run()


def _status() -> int:
    from daemon.status import describe

    for line in describe():
        emit(line)
    return 0


def _repair(*, quiet: bool = False) -> int:
    from daemon.repair import repair

    return repair(quiet=quiet)


def _daemon_foreground(owner_pid: int = 0) -> int:
    """Run the resolver in this process until interrupted.

    Imported here rather than at module scope so that ``--version`` and ``--status`` do not
    pay for asyncio, dnslib and the resolver on every invocation.
    """
    import asyncio

    from daemon.runner import Runner, run_forever

    runner = Runner(owner_pid=owner_pid)
    try:
        return asyncio.run(run_forever(runner))
    except KeyboardInterrupt:
        # asyncio.run re-raises this after cancelling the loop; run_forever's finally has
        # already removed the rules, so there is nothing left to clean up here.
        return 130
