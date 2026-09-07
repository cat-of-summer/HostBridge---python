"""The decision: answer this query ourselves, or forward it.

A pure function over (bytes, zone, cache, clock-free settings). No socket, no clock of its
own, no I/O -- which is what lets the whole match ladder, the NODATA rules and the cache
interaction be tested in a container with a plain equality assertion.
"""

from __future__ import annotations

from dataclasses import dataclass

from dnslib import CLASS, OPCODE, QTYPE, DNSRecord

from core.match import Zone, normalise
from resolver import answer as answers
from resolver.cache import CacheKey
from resolver.wire import txid as read_txid

#: RFC 8482 discourages answering ANY meaningfully; here it would also mean guessing which
#: of our record types the client wanted.
QTYPE_ANY = 255

#: Type 65, HTTPS/SVCB. Called out by name because forwarding it is the single most
#: damaging mistake this module could make -- see :func:`decide`.
QTYPE_HTTPS = 65


@dataclass(frozen=True)
class Local:
    """Answer built by us; send these bytes straight back."""

    payload: bytes


@dataclass(frozen=True)
class Forward:
    """Relay upstream. ``key`` is the cache key the response should be stored under."""

    key: CacheKey
    txid: int


Action = Local | Forward


def _dnssec_ok(record: DNSRecord) -> bool:
    """Whether the client set the EDNS0 DO bit.

    Read off the OPT record's TTL field, where the DO flag is bit 15, rather than through a
    dnslib convenience that has moved between releases.
    """
    try:
        for rr in record.ar:
            if rr.rtype == QTYPE.OPT:
                return bool((int(rr.ttl) >> 15) & 1)
    except (AttributeError, TypeError, ValueError):
        return False
    return False


def cache_key(record: DNSRecord) -> CacheKey:
    question = record.q
    return (
        normalise(str(question.qname)),
        int(question.qtype),
        int(question.qclass),
        _dnssec_ok(record),
    )


def decide(payload: bytes, zone: Zone, cache, ttl: int, serial: int = 1) -> Action:
    """Choose how to handle one query.

    The rule that makes local domains work in a browser: **once a name matches an enabled
    record, no query for that name is ever forwarded, whatever its type.** AAAA, MX, TXT and
    above all HTTPS/SVCB get NODATA instead.

    Forwarding HTTPS for a shadowed production domain would hand Chrome the real site's SVCB
    record -- its alt-svc hints, its ECH config, possibly an IPv6 address -- and Chrome would
    open an HTTP/3 connection straight to production while this resolver sat here holding a
    perfectly correct A record. The symptom is "DNS says 127.0.0.1 but the browser shows the
    live site", which is close to undiagnosable from the outside.
    """
    try:
        record = DNSRecord.parse(payload)
    except Exception:  # noqa: BLE001 - dnslib raises a variety of parse errors
        return Local(answers.formerr(payload))

    if record.header.opcode != OPCODE.QUERY:
        return Local(answers.answer_notimp(record))

    if len(record.questions) != 1:
        # Multiple questions in one message is legal to encode and understood by nobody.
        return Local(answers.formerr(payload))

    question = record.q
    if int(question.qclass) != CLASS.IN:
        return Local(answers.answer_refused(record))

    found = zone.match(str(question.qname))
    if found is None:
        key = cache_key(record)
        request_txid = read_txid(payload)
        cached = cache.get(key, request_txid)
        if cached is not None:
            return Local(cached)
        return Forward(key=key, txid=request_txid)

    qtype = int(question.qtype)
    wants_v6 = qtype == QTYPE.AAAA
    have_v6 = answers.is_ipv6(found.address)

    if qtype != QTYPE_ANY and wants_v6 == have_v6 and qtype in (QTYPE.A, QTYPE.AAAA):
        return Local(answers.answer_address(record, found.address, ttl))

    return Local(answers.answer_nodata(record, found.apex, ttl, serial))
