"""The domain store: the one file every surface reads and writes.

Three processes can want to change it -- the daemon, the console screen and (until the
control API lands) anything run from a terminal -- so every mutation goes through
:meth:`DomainStore.mutate`, which takes a cross-process lock, **re-reads under it**, applies
the change and writes atomically. Saving a snapshot taken before the lock is the classic way
to lose a concurrent edit, and with a resolver polling Traefik every ten seconds the race is
not theoretical.

The locking primitives are the ones ccas settled on (``core/store.py`` there): ``msvcrt``
on Windows, ``fcntl`` elsewhere, polled with a timeout rather than blocking forever, because
a wedged lock must not take the resolver down with it.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import log
from core.jsonio import read_json, write_json_atomic
from core.match import normalise
from core.migrate import SchemaTooNew, migrate
from core.model import SOURCE_MANUAL, Domain
from core.paths import domains_file
from core.version import SCHEMA_VERSION, __version__

BACKUP_KEEP = 10

LOCK_TIMEOUT = 10.0
LOCK_POLL = 0.05


class StoreLocked(Exception):
    """Another process held the lock for longer than :data:`LOCK_TIMEOUT`."""


class StoreReadOnly(Exception):
    """The file on disk was written by a newer build and must not be overwritten."""


@dataclass
class Snapshot:
    """What one read of the store produced."""

    domains: list[Domain] = field(default_factory=list)
    schema: int = SCHEMA_VERSION
    updated_at: str = ""
    read_only: bool = False
    """Set when the file came from a newer build. Mutations raise instead of clobbering."""

    migrated: bool = False
    """Set when the file on disk was an older schema and had to be upgraded in memory.

    Carried so :meth:`DomainStore.mutate` writes even if the caller changed nothing: the
    upgrade itself is a change, and it has to reach the disk or it is redone on every read.
    """

    def by_id(self, identifier: str) -> Domain | None:
        return next((d for d in self.domains if d.id == identifier), None)

    def by_name(self, name: str) -> Domain | None:
        wanted = normalise(name)
        return next((d for d in self.domains if normalise(d.name) == wanted), None)


@dataclass
class ReconcileResult:
    """What one sync pass changed, for the log and for the status line."""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    """Discovered names that collide with a manually created record, which always wins."""

    skipped_no_owner: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


def _lock_exclusive(handle: int) -> None:
    """Take an exclusive lock on the first byte, or raise OSError if it is held."""
    if os.name == "nt":
        import msvcrt

        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: int) -> None:
    if os.name == "nt":
        import msvcrt

        with contextlib.suppress(OSError):
            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(handle, fcntl.LOCK_UN)


class DomainStore:
    """Reads and writes ``domains.json``.

    Cheap to construct and holds no cached state: the daemon rebuilds its zone from a fresh
    read whenever it is told the file changed, and a store object that quietly served a
    stale list would be a bug that only shows up as "the domain I added does not resolve".
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else domains_file()

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    # ---- reading ------------------------------------------------------------------

    def load(self) -> Snapshot:
        """Read the store. A missing or unreadable file yields an empty snapshot."""
        raw = read_json(self.path)
        if not isinstance(raw, dict):
            return Snapshot()

        read_only = False
        migrated = False
        try:
            payload, migrated = migrate(raw)
        except SchemaTooNew as exc:
            log.warn(f"store: {exc}; loading read-only")
            payload, read_only = raw, True

        entries = payload.get("domains")
        domains: list[Domain] = []
        if isinstance(entries, list):
            for entry in entries:
                domain = Domain.from_dict(entry)
                if domain is not None:
                    domains.append(domain)

        updated = payload.get("updated_at")
        schema = payload.get("schema")
        return Snapshot(
            domains=domains,
            schema=schema if isinstance(schema, int) else SCHEMA_VERSION,
            updated_at=updated if isinstance(updated, str) else "",
            read_only=read_only,
            migrated=migrated,
        )

    # ---- writing ------------------------------------------------------------------

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the cross-process lock, or raise :class:`StoreLocked`."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + LOCK_TIMEOUT
        try:
            while True:
                try:
                    _lock_exclusive(handle)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise StoreLocked(str(self.lock_path)) from None
                    time.sleep(LOCK_POLL)
            try:
                yield
            finally:
                _unlock(handle)
        finally:
            os.close(handle)

    def mutate(self, change: Callable[[Snapshot], Any]) -> Any:
        """Apply ``change`` to a snapshot re-read under the lock, then save.

        ``change`` mutates ``snapshot.domains`` in place and may return anything, which is
        handed back to the caller -- typically the record it created, so the UI can select
        it without a second read.
        """
        with self.locked():
            snapshot = self.load()
            if snapshot.read_only:
                raise StoreReadOnly(str(self.path))

            before = [domain.to_dict() for domain in snapshot.domains]
            result = change(snapshot)
            after = [domain.to_dict() for domain in snapshot.domains]

            # A pass that changed nothing must not write. Every write rotates a backup, and
            # the Traefik poll runs every ten seconds: writing unconditionally burned all
            # ten backup slots in under two minutes, so the one thing they exist for -- undoing
            # a bad reconciliation over a hundred domains -- was gone before anyone noticed.
            # It also bumped the file's mtime, waking the store watcher for nothing.
            if before != after or snapshot.migrated:
                self._write(snapshot.domains)
            return result

    def _write(self, domains: Iterable[Domain]) -> None:
        from core.model import utc_now

        self._rotate_backup()
        payload = {
            "schema": SCHEMA_VERSION,
            "app_version": __version__,
            "updated_at": utc_now(),
            "domains": [domain.to_dict() for domain in domains],
        }
        write_json_atomic(self.path, payload)

    def _rotate_backup(self) -> None:
        """Keep the last :data:`BACKUP_KEEP` versions beside the store.

        A bad reconciliation pass over a hundred domains has to be undoable, and the file is
        a few kilobytes -- there is no reason to be clever about this.
        """
        if not self.path.exists():
            return
        with contextlib.suppress(OSError):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = self.path.with_name(f"{self.path.name}.{stamp}.bak")
            if not backup.exists():
                backup.write_bytes(self.path.read_bytes())

            existing = sorted(self.path.parent.glob(f"{self.path.name}.*.bak"))
            for stale in existing[:-BACKUP_KEEP]:
                with contextlib.suppress(OSError):
                    stale.unlink()

    # ---- operations the surfaces call ---------------------------------------------

    def add(self, domain: Domain) -> Domain:
        """Create a record. Raises :class:`ValueError` if the name is already taken."""

        def _change(snapshot: Snapshot) -> Domain:
            if snapshot.by_name(domain.name) is not None:
                # The normalised form, not what was typed: that is the name that actually
                # collided, and "SHOP.TEST is already taken" when the list shows shop.test
                # reads like a bug in the app rather than a duplicate.
                raise ValueError(normalise(domain.name))
            snapshot.domains.append(domain)
            return domain

        return self.mutate(_change)

    def update(self, identifier: str, **changes: Any) -> Domain | None:
        """Apply ``changes`` to one record, remembering which fields a human pinned."""

        def _change(snapshot: Snapshot) -> Domain | None:
            index = next(
                (i for i, d in enumerate(snapshot.domains) if d.id == identifier), None
            )
            if index is None:
                return None
            current = snapshot.domains[index]

            pinned = set(current.pinned_fields)
            if current.source != SOURCE_MANUAL:
                # A hand edit of a discovered record must survive the next sync pass, so
                # record which fields the user took ownership of.
                pinned.update(key for key in changes if key != "enabled")

            updated = current.touched(**changes, pinned_fields=sorted(pinned))
            snapshot.domains[index] = updated
            return updated

        return self.mutate(_change)

    def toggle(self, identifier: str, enabled: bool | None = None) -> Domain | None:
        def _change(snapshot: Snapshot) -> Domain | None:
            index = next(
                (i for i, d in enumerate(snapshot.domains) if d.id == identifier), None
            )
            if index is None:
                return None
            current = snapshot.domains[index]
            wanted = (not current.enabled) if enabled is None else enabled
            # Enabling is not a hand edit worth pinning: it is the one thing a sync pass is
            # allowed to keep managing on a discovered record.
            updated = current.touched(enabled=wanted)
            snapshot.domains[index] = updated
            return updated

        return self.mutate(_change)

    def delete(self, identifier: str) -> bool:
        def _change(snapshot: Snapshot) -> bool:
            before = len(snapshot.domains)
            snapshot.domains[:] = [d for d in snapshot.domains if d.id != identifier]
            return len(snapshot.domains) != before

        return self.mutate(_change)

    def reconcile(self, source: str, discovered: Iterable[Domain]) -> ReconcileResult:
        """Bring the records owned by ``source`` in line with what was just observed.

        The rule this enforces, and the reason it lives here rather than in each importer:

            A sync pass for source *X* may create, update or delete only records whose
            ``source`` is *X* **and** whose ``owner`` is in the set *X* just observed. It
            never touches a record belonging to another source.

        So a Traefik poll cannot delete a domain you typed by hand, and a hand edit of a
        Traefik-derived record survives the next poll because the edited field names are in
        ``pinned_fields``.
        """
        wanted = list(discovered)

        def _change(snapshot: Snapshot) -> ReconcileResult:
            result = ReconcileResult()
            mine = {d.owner: d for d in snapshot.domains if d.source == source and d.owner}
            observed: set[str] = set()

            for candidate in wanted:
                if not candidate.owner:
                    # Without an owner we could never tell later whether the record is
                    # still observed, so it could never be cleaned up. Refuse it instead.
                    result.skipped_no_owner += 1
                    continue
                observed.add(candidate.owner)

                clash = snapshot.by_name(candidate.name)
                if clash is not None and clash.source != source:
                    # A manually created name always wins; the importer reports the clash
                    # so the UI can show a badge rather than silently dropping the route.
                    result.conflicts.append(candidate.name)
                    continue

                current = mine.get(candidate.owner)
                if current is None:
                    snapshot.domains.append(candidate)
                    result.added.append(candidate.name)
                    continue

                changes = {
                    key: value
                    for key, value in (
                        ("name", candidate.name),
                        ("address", candidate.address),
                        ("note", candidate.note),
                    )
                    if key not in current.pinned_fields and getattr(current, key) != value
                }
                if changes:
                    index = snapshot.domains.index(current)
                    snapshot.domains[index] = current.touched(**changes)
                    result.updated.append(candidate.name)

            stale = [
                d
                for d in snapshot.domains
                if d.source == source and d.owner and d.owner not in observed
            ]
            if stale:
                gone = {d.id for d in stale}
                snapshot.domains[:] = [d for d in snapshot.domains if d.id not in gone]
                result.removed.extend(d.name for d in stale)

            return result

        return self.mutate(_change)
