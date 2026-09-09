"""End-to-end resolver tests over real sockets on ephemeral ports.

Marked ``network`` rather than ``admin``: nothing here binds port 53 or touches system
policy, so these run in the container and on both CI runners -- which is the point, because
they are the tests that prove the sockets actually work.

Written as plain sync tests driving ``asyncio.run``, deliberately: it reads the way the
daemon actually starts, and it saves a dependency on pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
from dnslib import EDNS0, QTYPE, RCODE, RR, A, DNSRecord

from core.match import Zone
from core.model import Domain
from resolver.cache import Cache
from resolver.server import BindError, Resolver, Server, bind_sockets
from resolver.upstream import Upstream, sanitise_upstreams

pytestmark = pytest.mark.network

TTL = 5


def _free_port() -> int:
    """A port free for **both** protocols, which is what the resolver needs.

    Probing only UDP is not enough and produced a real flake: the two protocols have
    separate port spaces, so the kernel would happily hand back a UDP port whose TCP side
    another still-running test was listening on, and the bind failed there instead.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
        datagram.bind(("127.0.0.1", 0))
        port = datagram.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
            stream.bind(("127.0.0.1", port))
        return port


def _zone(*specs) -> Zone:
    return Zone.from_domains([Domain(name=n, address=a, enabled=e) for n, a, e in specs])


class FakeUpstream(asyncio.DatagramProtocol):
    """Counts what it was asked and replies from a script."""

    def __init__(self) -> None:
        self.requests: list[bytes] = []
        self.delay = 0.0
        self._transport = None

    def connection_made(self, transport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.requests.append(data)
        asyncio.get_running_loop().create_task(self._reply(data, addr))

    async def _reply(self, data: bytes, addr) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        request = DNSRecord.parse(data)
        reply = request.reply()
        reply.add_answer(RR(request.q.qname, QTYPE.A, ttl=60, rdata=A("203.0.113.7")))
        if self._transport is not None:
            self._transport.sendto(reply.pack(), addr)


async def _start_fake_upstream() -> tuple[FakeUpstream, int, asyncio.DatagramTransport]:
    loop = asyncio.get_running_loop()
    protocol = FakeUpstream()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol, local_addr=("127.0.0.1", 0)
    )
    return protocol, transport.get_extra_info("sockname")[1], transport


async def _ask_udp(port: int, request: DNSRecord, timeout: float = 3.0) -> DNSRecord:
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    class _Client(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):  # noqa: ARG002
            if not future.done():
                future.set_result(data)

    transport, _ = await loop.create_datagram_endpoint(
        _Client, remote_addr=("127.0.0.1", port)
    )
    try:
        transport.sendto(request.pack())
        return DNSRecord.parse(await asyncio.wait_for(future, timeout))
    finally:
        transport.close()


async def _ask_tcp(port: int, request: DNSRecord, timeout: float = 3.0) -> DNSRecord:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        payload = request.pack()
        writer.write(len(payload).to_bytes(2, "big") + payload)
        await writer.drain()
        header = await asyncio.wait_for(reader.readexactly(2), timeout)
        body = await asyncio.wait_for(
            reader.readexactly(int.from_bytes(header, "big")), timeout
        )
        return DNSRecord.parse(body)
    finally:
        writer.close()
        await writer.wait_closed()


class Harness:
    """A running resolver plus a fake upstream, torn down together."""

    def __init__(self, zone: Zone) -> None:
        self.zone = zone
        self.port = _free_port()

    async def __aenter__(self) -> Harness:
        self.upstream_proto, upstream_port, self._upstream_transport = (
            await _start_fake_upstream()
        )
        self.cache = Cache()
        self.upstream = Upstream(["127.0.0.1"], self.cache, port=upstream_port, timeout=2.0)
        self.resolver = Resolver(self.zone, self.cache, self.upstream, TTL)
        udp, tcp = bind_sockets(["127.0.0.1"], self.port)
        self.server = Server(self.resolver, udp, tcp)
        await self.server.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.server.stop()
        self._upstream_transport.close()


# ---- local answers ----------------------------------------------------------------


def test_a_local_domain_is_answered_over_udp():
    async def scenario():
        async with Harness(_zone(("shop.test", "127.0.0.1", True))) as harness:
            reply = await _ask_udp(harness.port, DNSRecord.question("shop.test", "A"))
            assert reply.header.rcode == RCODE.NOERROR
            assert str(reply.rr[0].rdata) == "127.0.0.1"
            assert harness.upstream_proto.requests == [], "a local name must not go upstream"

    asyncio.run(scenario())


def test_a_local_domain_is_answered_over_tcp():
    async def scenario():
        async with Harness(_zone(("shop.test", "127.0.0.1", True))) as harness:
            reply = await _ask_tcp(harness.port, DNSRecord.question("shop.test", "A"))
            assert str(reply.rr[0].rdata) == "127.0.0.1"

    asyncio.run(scenario())


def test_a_wildcard_is_answered_end_to_end():
    async def scenario():
        async with Harness(_zone(("*.shop.test", "10.1.2.3", True))) as harness:
            reply = await _ask_udp(harness.port, DNSRecord.question("a.b.shop.test", "A"))
            assert str(reply.rr[0].rdata) == "10.1.2.3"

    asyncio.run(scenario())


# ---- forwarding -------------------------------------------------------------------


def test_an_unknown_name_is_forwarded_and_the_answer_relayed():
    async def scenario():
        async with Harness(_zone(("shop.test", "127.0.0.1", True))) as harness:
            reply = await _ask_udp(harness.port, DNSRecord.question("google.com", "A"))
            assert str(reply.rr[0].rdata) == "203.0.113.7"
            assert len(harness.upstream_proto.requests) == 1

    asyncio.run(scenario())


def test_the_client_transaction_id_is_restored_on_a_forwarded_answer():
    async def scenario():
        async with Harness(_zone()) as harness:
            request = DNSRecord.question("google.com", "A")
            request.header.id = 0x7A7A
            reply = await _ask_udp(harness.port, request)
            assert reply.header.id == 0x7A7A

            forwarded = DNSRecord.parse(harness.upstream_proto.requests[0])
            assert forwarded.header.id != 0x7A7A, "the id put on the wire must be our own"

    asyncio.run(scenario())


def test_forwarding_is_opaque_so_edns_options_survive():
    """Re-serialising a query is how a local resolver quietly breaks DNSSEC."""

    async def scenario():
        async with Harness(_zone()) as harness:
            request = DNSRecord.question("google.com", "A")
            request.add_ar(EDNS0(flags="do", udp_len=4096))
            await _ask_udp(harness.port, request)

            sent = harness.upstream_proto.requests[0]
            original = request.pack()
            assert sent[2:] == original[2:], "only the transaction id may differ"

    asyncio.run(scenario())


def test_identical_concurrent_queries_share_one_upstream_query():
    """One page load fires A, AAAA and HTTPS for a dozen hosts; without dedup that is a
    query storm against the upstream resolver."""

    async def scenario():
        async with Harness(_zone()) as harness:
            harness.upstream_proto.delay = 0.15
            requests = [DNSRecord.question("google.com", "A") for _ in range(20)]
            for index, request in enumerate(requests):
                request.header.id = 0x100 + index

            replies = await asyncio.gather(
                *(_ask_udp(harness.port, request) for request in requests)
            )

            assert len(harness.upstream_proto.requests) == 1
            assert all(str(reply.rr[0].rdata) == "203.0.113.7" for reply in replies)
            assert sorted(reply.header.id for reply in replies) == [
                0x100 + index for index in range(20)
            ], "every joiner gets its own transaction id back"

    asyncio.run(scenario())


def test_a_second_identical_query_is_served_from_cache():
    async def scenario():
        async with Harness(_zone()) as harness:
            await _ask_udp(harness.port, DNSRecord.question("google.com", "A"))
            await _ask_udp(harness.port, DNSRecord.question("google.com", "A"))
            assert len(harness.upstream_proto.requests) == 1
            assert harness.cache.stats.hits == 1

    asyncio.run(scenario())


def test_servfail_when_every_upstream_is_unreachable():
    async def scenario():
        async with Harness(_zone()) as harness:
            harness.upstream.port = _free_port()  # nothing is listening there
            harness.upstream.timeout = 0.3
            reply = await _ask_udp(harness.port, DNSRecord.question("google.com", "A"))
            assert reply.header.rcode == RCODE.SERVFAIL

    asyncio.run(scenario())


# ---- live zone swap ---------------------------------------------------------------


def test_the_zone_can_be_replaced_while_the_server_runs():
    async def scenario():
        async with Harness(_zone()) as harness:
            first = await _ask_udp(harness.port, DNSRecord.question("late.test", "A"))
            assert str(first.rr[0].rdata) == "203.0.113.7", "unknown at first, so forwarded"

            harness.resolver.apply(_zone(("late.test", "127.0.0.55", True)))

            second = await _ask_udp(harness.port, DNSRecord.question("late.test", "A"))
            assert str(second.rr[0].rdata) == "127.0.0.55"

    asyncio.run(scenario())


def test_swapping_the_zone_clears_a_now_stale_cached_answer():
    async def scenario():
        async with Harness(_zone()) as harness:
            await _ask_udp(harness.port, DNSRecord.question("late.test", "A"))
            assert len(harness.cache) == 1

            harness.resolver.apply(_zone(("late.test", "127.0.0.55", True)))
            assert len(harness.cache) == 0

    asyncio.run(scenario())


# ---- binding ----------------------------------------------------------------------


def test_binding_a_taken_port_raises_a_bind_error_naming_it():
    port = _free_port()
    udp, tcp = bind_sockets(["127.0.0.1"], port)
    try:
        with pytest.raises(BindError) as caught:
            bind_sockets(["127.0.0.1"], port)
        assert caught.value.port == port
        assert caught.value.address == "127.0.0.1"
        assert caught.value.in_use
    finally:
        for sock in (*udp, *tcp):
            sock.close()


def test_a_failed_bind_leaves_no_socket_behind():
    """The bind-before-apply guarantee depends on a partial failure closing everything."""
    port = _free_port()
    udp, tcp = bind_sockets(["127.0.0.1"], port)
    try:
        with pytest.raises(BindError):
            bind_sockets(["127.0.0.1", "127.0.0.1"], port)
    finally:
        for sock in (*udp, *tcp):
            sock.close()


# ---- loop protection --------------------------------------------------------------


def test_sanitise_upstreams_drops_loopback_and_our_own_addresses():
    kept = sanitise_upstreams(
        ["127.0.0.1", "::1", "192.168.1.1", "8.8.8.8", "", "not-an-ip", "192.168.1.1"],
        listen_addresses=["192.168.1.1"],
    )
    assert kept == ["8.8.8.8"]


def test_sanitise_upstreams_keeps_order_and_deduplicates():
    assert sanitise_upstreams(["8.8.8.8", "1.1.1.1", "8.8.8.8"]) == ["8.8.8.8", "1.1.1.1"]
