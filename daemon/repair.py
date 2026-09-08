"""Undo whatever a previous run left behind, without starting anything.

Runs with no daemon, no Qt and no valid store: it has to work when everything else is
broken, which is the entire point of it existing.

Two independent recovery paths, because either alone has a hole:

* the **state file**, which names exactly what we claimed -- fast, and precise;
* a **scan for our marker**, which finds rules from a build whose state file was deleted,
  never written, or came from a newer version.

Both run, in that order. Finding nothing to do is success: exiting non-zero for "there was
no damage" would train the user to ignore the command.
"""

from __future__ import annotations

import contextlib

from app.output import emit
from core import log
from core.config import DaemonSettings
from core.paths import daemon_file
from daemon import netstate
from daemon.policy import Policy, PolicyError, PolicyState, detect
from ui.i18n import t


def repair(policy: Policy | None = None, *, quiet: bool = False) -> int:
    """Remove every rule this application may have left in place. Returns an exit code."""
    settings = DaemonSettings.load()
    policy = policy or detect(settings.excluded_adapters)

    removed: list[str] = []
    failures: list[str] = []

    state = netstate.read()
    if state is not None and (state.confirmed or state.intent or state.links):
        wanted = PolicyState(
            namespaces=tuple(dict.fromkeys([*state.confirmed, *state.intent])),
            links=tuple(state.links),
        )
        try:
            policy.remove(wanted)
        except PolicyError as exc:
            failures.append(str(exc))
        else:
            removed.extend(wanted.namespaces)

    try:
        policy.remove_all()
    except PolicyError as exc:
        failures.append(str(exc))

    policy.flush()

    netstate.clear()
    with contextlib.suppress(OSError):
        daemon_file().unlink()

    if failures:
        for failure in failures:
            emit(t("error.repair_failed", error=failure), error=True)
        return 5

    log.write(f"repair: removed {len(removed)} recorded namespaces")
    if not quiet:
        if removed:
            emit(t("cli.repair_removed", count=len(removed)))
        else:
            emit(t("cli.repair_nothing"))
    return 0
