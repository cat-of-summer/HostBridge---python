"""Relaying the queries we do not own, opaquely and without stampedes.

Two properties matter here and both are load-bearing:

**Opacity.** The datagram that goes upstream is the client's own bytes with only the
two-octet transaction id replaced, and the reply handed back is the upstream's own bytes
with the client's id restored. Nothing is re-serialised, so EDNS0 options, the DNSSEC DO
bit, RRSIG records and record types this build has never heard of all survive. A local dev
resolver that quietly breaks DNSSEC is worse than no resolver.

**Deduplication.** A browser opening one page fires A, AAAA and HTTPS for a dozen hosts at
once. Without joining identical in-flight queries, a single page load becomes a query storm
against the upstream resolver.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import time
from collections.abc import Callable, Iterable, Sequence

from core import log
from resolver.answer import min_ttl
from resolver.cache import Cache, CacheKey
from resolver.wire import (
    MAX_UDP_PAYLOAD,
    TCP_LENGTH_PREFIX,
    is_truncated,
    random_txid,
    txid,
    with_txid,
)

DEFAULT_TIMEOUT = 2.0

#: How long a server that timed out or failed is passed over. Long enough to stop hammering
#: a dead resolver, short enough that a blip does not exile it for the session.
COLD_SECONDS = 30.0


def sanitise_upstreams(
    servers: Iterable[str], listen_addresses: Iterable[str] = ()
) -> list[str]:
    """Drop anything that would point this resolver back at itself.

    A captured configuration can already contain our own address -- most obviously after a
    crash, when the rules from the previous run are still in place. Forwarding there is an
    instant infinite loop that takes the machine's name resolution with it, so the filter is
    unconditional rather than best-effort.
    """
    ours = {str(address) for address in listen_addresses}
    keep: list[str] = []
    for raw in servers:
        candidate = (raw or "").strip()
        if not candidate or candidate in ours:
            continue
        try:
            parsed = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if parsed.is_loopback or parsed.is_unspecified:
            continue
        if str(parsed) not in keep:
            keep.append(str(parsed))
    return keep


class _UdpQuery(asyncio.DatagramProtocol):
    """One question, one answer. Closed as soon as the reply arrives or we give up."""

    def __init__(self, future: asyncio.Future) -> None:
        self._future = future

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ARG002
        if not self._future.done():
            self._future.set_result(data)

    def error_received(self, exc: Exception) -> None:
        # An ICMP port-unreachable arrives here. Treat it as this server failing rather
        # than as a fatal error, so the next server is tried.
        if not self._future.done():
            self._future.set_exception(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None and not self._future.done():
            self._future.set_exception(exc)


class Upstream:
    """Forwards to the captured system resolvers, in order, with failover."""

    def __init__(
        self,
        servers: Sequence[str],
        cache: Cache,
        *,
        port: int = 53,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.servers = list(servers)
        self.port = port
        self.timeout = timeout
        self._cache = cache
        self._clock = clock
        self._cold: dict[str, float] = {}
        self._inflight: dict[CacheKey, asyncio.Future] = {}

    # ---- server health ------------------------------------------------------------

    def _order(self) -> list[str]:
        """Healthy servers first, cold ones after, original order preserved within each."""
        now = self._clock()
        warm = [s for s in self.servers if self._cold.get(s, 0.0) <= now]
        cold = [s for s in self.servers if self._cold.get(s, 0.0) > now]
        return warm + cold

    def _mark_cold(self, server: str) -> None:
        self._cold[server] = self._clock() + COLD_SECONDS

    def _mark_warm(self, server: str) -> None:
        self._cold.pop(server, None)

    # ---- the wire -----------------------------------------------------------------

    async def _ask_udp(self, server: str, payload: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _UdpQuery(future), remote_addr=(server, self.port)
        )
        try:
            transport.sendto(payload)
            return await asyncio.wait_for(future, self.timeout)
        finally:
            transport.close()

    async def _ask_tcp(self, server: str, payload: bytes) -> bytes:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(server, self.port), self.timeout
        )
        try:
            writer.write(len(payload).to_bytes(TCP_LENGTH_PREFIX, "big") + payload)
            await writer.drain()
            header = await asyncio.wait_for(
                reader.readexactly(TCP_LENGTH_PREFIX), self.timeout
            )
            length = int.from_bytes(header, "big")
            return await asyncio.wait_for(reader.readexactly(length), self.timeout)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _query_once(self, payload: bytes, *, via_tcp: bool) -> bytes | None:
        """Try each server in turn. ``None`` when every one of them failed."""
        for server in self._order():
            try:
                if via_tcp:
                    answer = await self._ask_tcp(server, payload)
                else:
                    answer = await self._ask_udp(server, payload)
                    if is_truncated(answer):
                        # The answer did not fit in a datagram; ask the same server again
                        # over TCP rather than handing the client a truncated reply.
                        answer = await self._ask_tcp(server, payload)
            except (TimeoutError, OSError, asyncio.IncompleteReadError) as exc:
                log.warn(f"upstream {server}: {exc}")
                self._mark_cold(server)
                continue

            self._mark_warm(server)
            return answer

        return None

    # ---- the public entry point ---------------------------------------------------

    async def resolve(
        self, payload: bytes, key: CacheKey, *, via_tcp: bool = False
    ) -> bytes | None:
        """Forward one query. Identical in-flight questions share a single upstream query."""
        client_id = txid(payload)

        pending = self._inflight.get(key)
        if pending is not None:
            # shield: this joiner's own cancellation must not cancel the shared query and
            # strand everybody else waiting on it.
            try:
                answer = await asyncio.shield(pending)
            except (asyncio.CancelledError, Exception):  # noqa: B902
                return None
            return with_txid(answer, client_id) if answer else None

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._inflight[key] = future
        try:
            answer = await self._query_once(with_txid(payload, random_txid()), via_tcp=via_tcp)
            if answer:
                self._cache.put(key, answer, min_ttl(answer))
            if not future.done():
                future.set_result(answer)
            return with_txid(answer, client_id) if answer else None
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            self._inflight.pop(key, None)


def truncate_to_udp(payload: bytes) -> bytes:
    """Guard against handing a datagram client more than it asked for.

    Never reached for a local answer -- ours are tiny -- but a forwarded reply from a server
    that ignored the advertised EDNS0 size would otherwise be dropped silently by the OS.
    """
    return payload if len(payload) <= MAX_UDP_PAYLOAD else payload[:MAX_UDP_PAYLOAD]
