"""Schema migrations for ``domains.json``.

One function per version step, applied in order. The store calls :func:`migrate` before
building any :class:`core.model.Domain`, so a migration works on plain dictionaries and
never has to keep an old dataclass definition alive.

A file written by a *newer* HostBridge is never rewritten: downgrading and silently
dropping fields the newer build cared about is worse than refusing to touch it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.version import SCHEMA_VERSION


class SchemaTooNew(Exception):
    """The file was written by a newer build. Load it read-only, do not save over it."""

    def __init__(self, found: int) -> None:
        super().__init__(f"domains.json schema {found} is newer than {SCHEMA_VERSION}")
        self.found = found


def _detect_version(raw: dict[str, Any]) -> int:
    """Version of a payload that may predate the field itself."""
    version = raw.get("schema")
    if isinstance(version, int) and version > 0:
        return version
    # No schema key at all: the very first shape this file ever had.
    return 1


#: ``{from_version: upgrade}``. Each entry returns the payload at ``from_version + 1``.
#: Empty while SCHEMA_VERSION is 1 -- the first migration lands here when the record shape
#: first changes, and the tests below it exist from day one so the mechanism is never
#: exercised for the first time under pressure.
_MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def migrate(raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Bring ``raw`` up to :data:`core.version.SCHEMA_VERSION`.

    Returns the payload and whether anything changed, so the caller can skip writing -- and
    skip rotating a backup -- when the file was already current.
    """
    found = _detect_version(raw)
    if found > SCHEMA_VERSION:
        raise SchemaTooNew(found)

    changed = False
    payload = raw
    while found < SCHEMA_VERSION:
        upgrade = _MIGRATIONS.get(found)
        if upgrade is None:
            # A gap in the chain is a programming error, not user data being odd. Stamping
            # the current version is the least destructive way out: nothing is dropped.
            break
        payload = upgrade(payload)
        found += 1
        changed = True

    if payload.get("schema") != SCHEMA_VERSION:
        payload = dict(payload)
        payload["schema"] = SCHEMA_VERSION
        changed = True

    return payload, changed
