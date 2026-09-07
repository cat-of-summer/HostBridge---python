"""The decision function, including the rules that decide whether a browser works.

Every test here is a plain call to :func:`resolver.query.decide` -- no socket, no clock, no
daemon -- which is the payoff of keeping the decision pure.
"""

from __future__ import annotations

import pytest
from dnslib import EDNS0, QTYPE, RCODE, DNSRecord

from core.match import Zone
from core.model import Domain
from resolver.cache import Cache
from resolver.query import QTYPE_ANY, QTYPE_HTTPS, Forward, Local, decide
from resolver.wire import txid

TTL = 5


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _zone(*specs) -> Zone:
    return Zone.from_domains([Domain(name=n, address=a, enabled=e) for n, a, e in specs])


def _query(name: str, qtype: str = "A", *, dnssec: bool = False) -> bytes:
    record = DNSRecord.question(name, qtype)
    if dnssec:
        record.add_ar(EDNS0(flags="do", udp_len=4096))
    return record.pack()


def _parse(payload: bytes) -> DNSRecord:
    return DNSRecord.parse(payload)


@pytest.fixture
def cache() -> Cache:
    return Cache(clock=FakeClock())


# ---- local answers ----------------------------------------------------------------


def test_an_a_query_for_our_name_is_answered_locally(cache):
    zone = _zone(("shop.test", "127.0.0.1", True))
    action = decide(_query("shop.test"), zone, cache, TTL)

    assert isinstance(action, Local)
    reply = _parse(action.payload)
    assert reply.header.rcode == RCODE.NOERROR
    assert len(reply.rr) == 1
    assert str(reply.rr[0].rdata) == "127.0.0.1"
    assert reply.rr[0].ttl == TTL


def test_the_reply_echoes_the_transaction_id(cache):
    zone = _zone(("shop.test", "127.0.0.1", True))
    request = DNSRecord.question("shop.test", "A")
    request.header.id = 0x4242
    action = decide(request.pack(), zone, cache, TTL)
    assert txid(action.payload) == 0x4242


def test_the_reply_preserves_the_case_of_the_question(cache):
    """0x20 mixed-case clients compare the echoed name byte for byte."""
    zone = _zone(("shop.test", "127.0.0.1", True))
    action = decide(_query("ShOp.TeSt"), zone, cache, TTL)
    assert "ShOp.TeSt" in str(_parse(action.payload).rr[0].rname)


def test_a_wildcard_answers_a_subdomain(cache):
    zone = _zone(("*.shop.test", "127.0.0.1", True))
    action = decide(_query("api.shop.test"), zone, cache, TTL)
    assert str(_parse(action.payload).rr[0].rdata) == "127.0.0.1"


def test_an_ipv6_record_answers_aaaa(cache):
    zone = _zone(("shop.test", "::1", True))
    action = decide(_query("shop.test", "AAAA"), zone, cache, TTL)
    reply = _parse(action.payload)
    assert reply.rr[0].rtype == QTYPE.AAAA
    assert str(reply.rr[0].rdata) == "::1"


# ---- NODATA, and why it is not NXDOMAIN -------------------------------------------


def _assert_nodata(payload: bytes, apex: str) -> None:
    reply = _parse(payload)
    assert reply.header.rcode == RCODE.NOERROR, "NXDOMAIN makes stacks give up on the name"
    assert len(reply.rr) == 0
    assert len(reply.auth) == 1
    assert reply.auth[0].rtype == QTYPE.SOA
    assert str(reply.auth[0].rname).rstrip(".") == apex


def test_aaaa_for_an_ipv4_name_is_nodata_not_nxdomain(cache):
    zone = _zone(("shop.test", "127.0.0.1", True))
    action = decide(_query("shop.test", "AAAA"), zone, cache, TTL)
    _assert_nodata(action.payload, "shop.test")


def test_the_soa_minimum_carries_our_ttl(cache):
    """MINIMUM is what a resolver caches a negative answer for; a big default would
    outlive the domain being switched back on."""
    zone = _zone(("shop.test", "127.0.0.1", True))
    action = decide(_query("shop.test", "AAAA"), zone, cache, TTL)
    soa = _parse(action.payload).auth[0]
    assert soa.ttl == TTL
    assert soa.rdata.times[4] == TTL


def test_a_wildcard_nodata_is_authoritative_for_the_wildcard_apex(cache):
    zone = _zone(("*.shop.test", "127.0.0.1", True))
    action = decide(_query("api.shop.test", "AAAA"), zone, cache, TTL)
    _assert_nodata(action.payload, "shop.test")


@pytest.mark.parametrize("qtype", ["MX", "TXT", "CAA", "NS", "SRV"])
def test_other_types_for_our_name_are_nodata_never_forwarded(cache, qtype):
    zone = _zone(("etm39.ru", "127.0.0.1", True))
    action = decide(_query("etm39.ru", qtype), zone, cache, TTL)
    assert isinstance(action, Local)
    _assert_nodata(action.payload, "etm39.ru")


def test_https_svcb_for_our_name_is_nodata_and_is_never_forwarded(cache):
    """The most damaging possible mistake in this resolver.

    Forwarding type 65 for a shadowed production domain hands Chrome the live site's SVCB
    record -- alt-svc, ECH config, maybe an IPv6 hint -- and Chrome opens HTTP/3 straight to
    production while we sit here holding a correct A record. The symptom is "DNS says
    127.0.0.1 but the browser shows the live site", which is close to undiagnosable.
    """
    zone = _zone(("etm39.ru", "127.0.0.1", True))
    request = DNSRecord.question("etm39.ru")
    request.q.qtype = QTYPE_HTTPS

    action = decide(request.pack(), zone, cache, TTL)

    assert isinstance(action, Local), "type 65 for one of our names must never be forwarded"
    _assert_nodata(action.payload, "etm39.ru")


def test_any_is_answered_with_nodata(cache):
    zone = _zone(("shop.test", "127.0.0.1", True))
    request = DNSRecord.question("shop.test")
    request.q.qtype = QTYPE_ANY
    action = decide(request.pack(), zone, cache, TTL)
    _assert_nodata(action.payload, "shop.test")


# ---- forwarding -------------------------------------------------------------------


def test_an_unknown_name_is_forwarded(cache):
    zone = _zone(("shop.test", "127.0.0.1", True))
    action = decide(_query("google.com"), zone, cache, TTL)

    assert isinstance(action, Forward)
    assert action.key[0] == "google.com"
    assert action.key[1] == QTYPE.A


def test_a_disabled_domain_is_forwarded(cache):
    """Switching a shadowed production domain off hands the real site straight back."""
    zone = _zone(("etm39.ru", "127.0.0.1", False))
    assert isinstance(decide(_query("etm39.ru"), zone, cache, TTL), Forward)


def test_a_wildcard_does_not_capture_its_own_apex(cache):
    zone = _zone(("*.shop.test", "127.0.0.1", True))
    assert isinstance(decide(_query("shop.test"), zone, cache, TTL), Forward)


def test_the_dnssec_bit_reaches_the_cache_key(cache):
    zone = _zone()
    plain = decide(_query("google.com"), zone, cache, TTL)
    signed = decide(_query("google.com", dnssec=True), zone, cache, TTL)
    assert plain.key[3] is False
    assert signed.key[3] is True


def test_a_cached_upstream_answer_is_served_locally(cache):
    zone = _zone()
    forward = decide(_query("google.com"), zone, cache, TTL)
    assert isinstance(forward, Forward)

    upstream = DNSRecord.question("google.com", "A")
    reply = upstream.reply()
    cache.put(forward.key, reply.pack(), ttl=30)

    again = decide(_query("google.com"), zone, cache, TTL)
    assert isinstance(again, Local)


# ---- malformed input --------------------------------------------------------------


def test_garbage_gets_formerr_rather_than_an_exception(cache):
    action = decide(b"\x12\x34not a dns message at all", _zone(), cache, TTL)
    assert isinstance(action, Local)
    assert _parse(action.payload).header.rcode == RCODE.FORMERR
    assert txid(action.payload) == 0x1234


def test_an_empty_packet_is_survivable(cache):
    assert isinstance(decide(b"", _zone(), cache, TTL), Local)


def test_a_non_query_opcode_gets_notimp(cache):
    request = DNSRecord.question("shop.test")
    request.header.opcode = 5  # UPDATE
    action = decide(request.pack(), _zone(("shop.test", "127.0.0.1", True)), cache, TTL)
    assert _parse(action.payload).header.rcode == RCODE.NOTIMP


def test_a_class_other_than_in_is_refused(cache):
    request = DNSRecord.question("shop.test")
    request.q.qclass = 3  # CHAOS
    action = decide(request.pack(), _zone(("shop.test", "127.0.0.1", True)), cache, TTL)
    assert _parse(action.payload).header.rcode == RCODE.REFUSED
