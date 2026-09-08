"""The asyncio DNS server: UDP and TCP, loopback only.

Sockets are created and bound by :func:`bind_sockets` **before** anything else happens, and
before the daemon is allowed to touch the system's resolution policy. That ordering is the
whole safety story: if port 53 is taken, the process fails here and the machine's name
resolution is byte-identical to what it was a moment earlier.

Windows notes, both of which have bitten real projects:

* ``SO_REUSEADDR`` is set only on POSIX. On Windows it does not mean "reuse a TIME_WAIT
  address", it means "let anyone steal this bound port" -- which would silently defeat the
  bind-before-apply guarantee in both directions.
* ``loop.add_signal_handler`` does not exist on the Proactor loop, so shutdown goes through
  ``signal.signal`` and a thread-safe callback. See :mod:`daemon.runner`.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import socket
from collections.abc import Iterable, Sequence

from core import log
from core.match import Zone
from resolver.cache import Cache
from resolver.query import Forward, Local, decide
from resolver.upstream import Upstream, truncate_to_udp
from resolver.wire import MAX_UDP_PAYLOAD, TCP_LENGTH_PREFIX

#: A TCP peer that opens a connection and says nothing is not worth a file descriptor.
TCP_IDLE_TIMEOUT = 5.0

TCP_BACKLOG = 64


class BindError(Exception):
    """A listening socket could not be created. Carries what the user needs to fix it."""

    def __init__(self, address: str, port: int, cause: OSError) -> None:
        super().__init__(f"{address}:{port}: {cause}")
        self.address = address
        self.port = port
        self.cause = cause

    @property
    def in_use(self) -> bool:
        # WSAEADDRINUSE on Windows, EADDRINUSE elsewhere.
        return self.cause.errno in (48, 98, 10048)

    @property
    def denied(self) -> bool:
        return self.cause.errno in (1, 13, 10013)


def _family_of(address: str) -> int:
    return socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET


def _prepare(sock: socket.socket, address: str) -> None:
    if os.name != "nt":
        # On POSIX this only relaxes TIME_WAIT. On Windows the same flag lets another
        # process take the port out from under us, so it is deliberately not set there.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if _family_of(address) == socket.AF_INET6:
        # Without this a v6 socket may also claim v4 on Linux, and the two binds collide.
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)


def bind_sockets(
    addresses: Sequence[str], port: int
) -> tuple[list[socket.socket], list[socket.socket]]:
    """Bind UDP and TCP on every address, or raise :class:`BindError` having closed all."""
    udp: list[socket.socket] = []
    tcp: list[socket.socket] = []
    try:
        for address in addresses:
            family = _family_of(address)

            datagram = socket.socket(family, socket.SOCK_DGRAM)
            _prepare(datagram, address)
            try:
                datagram.bind((address, port))
            except OSError as exc:
                datagram.close()
                raise BindError(address, port, exc) from exc
            datagram.setblocking(False)
            udp.append(datagram)

            stream = socket.socket(family, socket.SOCK_STREAM)
            _prepare(stream, address)
            try:
                stream.bind((address, port))
            except OSError as exc:
                stream.close()
                raise BindError(address, port, exc) from exc
            stream.listen(TCP_BACKLOG)
            stream.setblocking(False)
            tcp.append(stream)
    except BaseException:
        for sock in (*udp, *tcp):
            with contextlib.suppress(OSError):
                sock.close()
        raise
    return udp, tcp


class Resolver:
    """Holds the zone and answers one query at a time.

    ``apply`` is a bare attribute rebind, which is atomic under the GIL and asyncio is
    single-threaded anyway. An in-flight query has already read ``self._zone`` into a local,
    so it either completes against the old zone or the new one -- never a half-updated
    table. This works only because :class:`core.match.Zone` is immutable once built.
    """

    def __init__(self, zone: Zone, cache: Cache, upstream: Upstream, ttl: int) -> None:
        self._zone = zone
        self._cache = cache
        self._upstream = upstream
        self.ttl = ttl
        self.queries = 0
        self.answered_locally = 0
        self.forwarded = 0

    @property
    def zone(self) -> Zone:
        return self._zone

    def apply(self, zone: Zone) -> None:
        previous = self._zone
        self._zone = zone
        if previous.namespaces() != zone.namespaces():
            # A name that just became ours must not keep being served from the answer some
            # upstream gave for it a moment ago.
            self._cache.clear()

    async def handle(self, payload: bytes, *, via_tcp: bool = False) -> bytes | None:
        zone = self._zone
        self.queries += 1

        action = decide(payload, zone, self._cache, self.ttl, zone.serial or 1)
        if isinstance(action, Local):
            self.answered_locally += 1
            return action.payload

        assert isinstance(action, Forward)
        self.forwarded += 1
        answer = await self._upstream.resolve(payload, action.key, via_tcp=via_tcp)
        if answer is not None:
            return answer

        # Every upstream failed. SERVFAIL, never NXDOMAIN: a client caches a negative answer
        # and would stay broken long after the network came back.
        from dnslib import DNSRecord

        from resolver.answer import answer_servfail, formerr

        try:
            return answer_servfail(DNSRecord.parse(payload))
        except Exception:  # noqa: BLE001
            return formerr(payload)


class _UdpHandler(asyncio.DatagramProtocol):
    def __init__(self, resolver: Resolver, tasks: set[asyncio.Task]) -> None:
        self._resolver = resolver
        self._tasks = tasks
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        task = asyncio.get_running_loop().create_task(self._respond(data, addr))
        # Held in a set: a bare create_task result is only weakly referenced by the loop
        # and can be garbage-collected mid-flight. The classic asyncio footgun.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _respond(self, data: bytes, addr) -> None:
        try:
            reply = await self._resolver.handle(data, via_tcp=False)
        except Exception as exc:  # noqa: BLE001 - one bad query must not kill the listener
            log.error(f"udp handler: {exc!r}")
            return
        if reply and self._transport is not None:
            self._transport.sendto(truncate_to_udp(reply), addr)

    def error_received(self, exc: Exception) -> None:
        # An ICMP unreachable from a previous send. Logging and carrying on is correct;
        # letting it tear down the transport would take the listener with it.
        log.warn(f"udp: {exc!r}")


class Server:
    """Owns the listeners and their lifetime."""

    def __init__(
        self,
        resolver: Resolver,
        udp_sockets: Iterable[socket.socket],
        tcp_sockets: Iterable[socket.socket],
    ) -> None:
        self.resolver = resolver
        self._udp_sockets = list(udp_sockets)
        self._tcp_sockets = list(tcp_sockets)
        self._transports: list[asyncio.DatagramTransport] = []
        self._servers: list[asyncio.AbstractServer] = []
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for sock in self._udp_sockets:
            transport, _protocol = await loop.create_datagram_endpoint(
                lambda: _UdpHandler(self.resolver, self._tasks), sock=sock
            )
            self._transports.append(transport)

        for sock in self._tcp_sockets:
            server = await asyncio.start_server(self._serve_tcp, sock=sock)
            self._servers.append(server)

    async def _serve_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    header = await asyncio.wait_for(
                        reader.readexactly(TCP_LENGTH_PREFIX), TCP_IDLE_TIMEOUT
                    )
                except (TimeoutError, asyncio.IncompleteReadError):
                    return

                length = int.from_bytes(header, "big")
                if not 0 < length <= MAX_UDP_PAYLOAD * 16:
                    return
                try:
                    payload = await asyncio.wait_for(
                        reader.readexactly(length), TCP_IDLE_TIMEOUT
                    )
                except (TimeoutError, asyncio.IncompleteReadError):
                    return

                reply = await self.resolver.handle(payload, via_tcp=True)
                if not reply:
                    return
                writer.write(len(reply).to_bytes(TCP_LENGTH_PREFIX, "big") + reply)
                await writer.drain()
        except (ConnectionError, OSError):
            return
        except Exception as exc:  # noqa: BLE001
            log.error(f"tcp handler: {exc!r}")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def stop(self) -> None:
        for transport in self._transports:
            transport.close()
        self._transports.clear()

        for server in self._servers:
            server.close()
        for server in self._servers:
            with contextlib.suppress(Exception):
                await server.wait_closed()
        self._servers.clear()

        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
