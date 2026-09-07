"""Cache for answers that came from upstream.

**Only upstream answers are cached.** A local answer is a dict lookup and building it costs
under a microsecond, so caching one would buy nothing and would add a second place that has
to be invalidated when a domain is toggled -- the kind of asymmetry that turns into "I
switched it off and it still resolves".

The clock is injected so expiry is testable without sleeping: the whole suite runs in a
container and a test that waits five seconds for a TTL is a test people start skipping.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from resolver.wire import rcode, with_txid

#: ``(qname, qtype, qclass, dnssec_ok)``. The DO bit belongs in the key because a
#: DNSSEC-aware client and a plain one must not be served each other's answers.
CacheKey = tuple[str, int, int, bool]

MAX_ENTRIES = 4096

#: Upstream TTLs are clamped: a record claiming a week would outlive any change the user
#: makes, and one claiming zero would defeat the cache entirely.
MIN_TTL = 1
MAX_TTL = 3600

#: A failure is cached only briefly. An upstream hiccup must not persist as a stale
#: SERVFAIL long after the network came back.
MAX_NEGATIVE_TTL = 5


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    expired: int = 0
    evicted: int = 0
    stored: int = 0


@dataclass(frozen=True)
class _Entry:
    payload: bytes
    """The upstream response with its transaction id zeroed; callers patch in their own."""

    expires_at: float


class Cache:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self._clock = clock
        self._max_entries = max_entries
        self._entries: OrderedDict[CacheKey, _Entry] = OrderedDict()
        self.stats = CacheStats()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: CacheKey, request_txid: int) -> bytes | None:
        """Return a cached answer already stamped with ``request_txid``, or ``None``."""
        entry = self._entries.get(key)
        if entry is None:
            self.stats.misses += 1
            return None

        if entry.expires_at <= self._clock():
            del self._entries[key]
            self.stats.expired += 1
            self.stats.misses += 1
            return None

        self.stats.hits += 1
        return with_txid(entry.payload, request_txid)

    def put(self, key: CacheKey, payload: bytes, ttl: int) -> None:
        """Store an upstream response. ``ttl`` is the minimum TTL across its records."""
        if rcode(payload) != 0:
            ttl = min(ttl, MAX_NEGATIVE_TTL)
        ttl = max(MIN_TTL, min(int(ttl), MAX_TTL))

        # Zero the id on the way in: what is stored has to be answerable to any future
        # client, and leaving one client's id in the cache is how a stale-looking reply
        # gets dropped by the next one.
        self._entries[key] = _Entry(payload=with_txid(payload, 0), expires_at=self._clock() + ttl)
        self._entries.move_to_end(key)
        self.stats.stored += 1

        # Plain insertion-order eviction rather than LRU: a developer machine resolves a
        # few dozen distinct names, so the bookkeeping LRU needs would never pay for itself.
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            self.stats.evicted += 1

    def clear(self) -> None:
        self._entries.clear()
