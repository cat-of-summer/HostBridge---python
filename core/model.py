"""The domain record and its serialised form.

No I/O lives here on purpose: the daemon, the console screen and the GUI table model all
hold these objects, and a dataclass that cannot touch the disk is one that cannot surprise
any of them.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

#: Where a record came from. The reconciliation rule in :mod:`core.store` keys off this:
#: a sync pass for one source may never touch a record belonging to another.
SOURCE_MANUAL = "manual"
SOURCE_DOCKER = "docker"
SOURCE_TRAEFIK = "traefik"
SOURCES = (SOURCE_MANUAL, SOURCE_DOCKER, SOURCE_TRAEFIK)

DEFAULT_ADDRESS = "127.0.0.1"


def utc_now() -> str:
    """An ISO-8601 timestamp in UTC, to the second.

    Seconds are enough — this is for showing a user when a record last changed, and a
    microsecond tail only makes ``domains.json`` noisier in a diff.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Domain:
    """One name the resolver answers for.

    ``id`` rather than ``name`` is the identity: renaming a domain in the GUI must not look
    like deleting one row and inserting another, or the table selection jumps and any
    pending edit is lost.
    """

    name: str
    id: str = field(default_factory=new_id)

    enabled: bool = True
    """User intent, persisted. A disabled domain falls through to the upstream resolver,
    which is what hands the real production site back the moment you switch it off."""

    address: str = DEFAULT_ADDRESS
    source: str = SOURCE_MANUAL
    owner: str = ""
    """Container id or Traefik router name. Empty for a manually created record."""

    pinned_fields: list[str] = field(default_factory=list)
    """Fields the user edited by hand on a discovered record. A sync pass skips these
    forever after, so retargeting a Traefik-derived domain is not undone on the next poll."""

    note: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    # ---- derived, never persisted -------------------------------------------------

    @property
    def is_wildcard(self) -> bool:
        return self.name.startswith("*.")

    @property
    def is_discovered(self) -> bool:
        return self.source != SOURCE_MANUAL

    def touched(self, **changes: Any) -> Domain:
        """Return a copy with ``changes`` applied and ``updated_at`` refreshed."""
        return replace(self, updated_at=utc_now(), **changes)

    # ---- serialisation ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> Domain | None:
        """Build a record from stored JSON, or ``None`` when it is unusable.

        Returning ``None`` rather than raising is deliberate: one corrupt entry in
        ``domains.json`` must cost the user that entry, not the whole file.
        """
        if not isinstance(raw, dict):
            return None

        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            return None

        source = raw.get("source")
        if source not in SOURCES:
            source = SOURCE_MANUAL

        address = raw.get("address")
        if not isinstance(address, str) or not address:
            address = DEFAULT_ADDRESS

        pinned = raw.get("pinned_fields")
        pinned_fields = (
            [item for item in pinned if isinstance(item, str)] if isinstance(pinned, list) else []
        )

        def _text(key: str) -> str:
            value = raw.get(key)
            return value if isinstance(value, str) else ""

        identifier = _text("id") or new_id()
        created = _text("created_at") or utc_now()

        return cls(
            name=name.strip(),
            id=identifier,
            enabled=bool(raw.get("enabled", True)),
            address=address,
            source=source,
            owner=_text("owner"),
            pinned_fields=pinned_fields,
            note=_text("note"),
            created_at=created,
            updated_at=_text("updated_at") or created,
        )
