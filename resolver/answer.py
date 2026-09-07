"""Building the replies we generate ourselves.

Only *local* answers are built here. Anything forwarded upstream is relayed as opaque bytes
(see :mod:`resolver.wire`), so this module is the entire set of messages HostBridge
composes, and it is small on purpose.

The shape that matters most is NODATA. When a name is ours, **no query for it is ever
forwarded, whatever the type** -- see :mod:`resolver.query` for why -- so every type other
than the one we hold an address for has to be answered here, and answered correctly.
"""

from __future__ import annotations

import ipaddress

from dnslib import AAAA, QTYPE, RCODE, RR, SOA, A, DNSRecord

#: Mail exchange for the synthesised SOA. ``.invalid`` is guaranteed by RFC 6761 never to
#: resolve, which is exactly right for an address that must never be contacted.
SOA_MNAME = "ns.hostbridge.invalid."
SOA_RNAME = "admin.hostbridge.invalid."

SOA_REFRESH = 3600
SOA_RETRY = 600
SOA_EXPIRE = 86400


def is_ipv6(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).version == 6
    except ValueError:
        return False


def _soa(apex: str, ttl: int, serial: int) -> RR:
    """A synthetic SOA for the AUTHORITY section.

    The MINIMUM field carries our own TTL, because MINIMUM is what a resolver uses to cache
    a negative answer. Leaving it at some large default would mean a NODATA answer outlived
    the domain being toggled back on.
    """
    return RR(
        rname=apex,
        rtype=QTYPE.SOA,
        ttl=ttl,
        rdata=SOA(
            SOA_MNAME,
            SOA_RNAME,
            (serial, SOA_REFRESH, SOA_RETRY, SOA_EXPIRE, ttl),
        ),
    )


def answer_address(request: DNSRecord, address: str, ttl: int) -> bytes:
    """An A or AAAA answer, whichever matches ``address``."""
    reply = request.reply()
    rtype = QTYPE.AAAA if is_ipv6(address) else QTYPE.A
    rdata = AAAA(address) if is_ipv6(address) else A(address)
    # request.q.qname verbatim rather than a re-parsed string: a client using 0x20 mixed-case
    # encoding compares the echoed name byte for byte and treats a normalised one as a spoof.
    reply.add_answer(RR(rname=request.q.qname, rtype=rtype, ttl=ttl, rdata=rdata))
    return reply.pack()


def answer_nodata(request: DNSRecord, apex: str, ttl: int, serial: int = 1) -> bytes:
    """NOERROR with an empty answer section and an SOA in AUTHORITY.

    Not NXDOMAIN. Telling a client the name does not exist when we are holding an A record
    for it makes some stacks give up on the name entirely rather than try another type, and
    the browser then reports the site as unreachable rather than falling back.
    """
    reply = request.reply()
    reply.add_auth(_soa(apex, ttl, serial))
    return reply.pack()


def answer_servfail(request: DNSRecord) -> bytes:
    """Used when upstream could not be reached.

    Deliberately not NXDOMAIN: a client caches a negative answer and would stay broken after
    the network came back, whereas SERVFAIL is retried.
    """
    reply = request.reply()
    reply.header.rcode = RCODE.SERVFAIL
    return reply.pack()


def answer_refused(request: DNSRecord) -> bytes:
    reply = request.reply()
    reply.header.rcode = RCODE.REFUSED
    return reply.pack()


def answer_notimp(request: DNSRecord) -> bytes:
    reply = request.reply()
    reply.header.rcode = RCODE.NOTIMP
    return reply.pack()


def formerr(payload: bytes) -> bytes:
    """A FORMERR built from a packet we could not parse.

    Hand-assembled from the header bytes because by definition dnslib could not read it:
    echo the id, set QR and the rcode, and zero every count so nothing dangles.
    """
    from resolver.wire import HEADER_LENGTH, TXID_LENGTH

    if len(payload) < TXID_LENGTH:
        return b""
    header = bytearray(HEADER_LENGTH)
    header[0:TXID_LENGTH] = payload[0:TXID_LENGTH]
    header[2] = 0x80  # QR=1, opcode 0, not authoritative
    header[3] = RCODE.FORMERR
    return bytes(header)


def min_ttl(payload: bytes, default: int = 60) -> int:
    """The smallest TTL across an upstream response, for the cache.

    Parsing here is throwaway: the bytes we cache and later return are the originals, so
    opacity is preserved -- this only reads how long we may keep them.
    """
    try:
        record = DNSRecord.parse(payload)
    except Exception:  # noqa: BLE001 - dnslib raises a zoo of parse errors
        return default

    ttls = [
        rr.ttl
        for section in (record.rr, record.auth, record.ar)
        for rr in section
        if getattr(rr, "ttl", None) is not None and rr.rtype != QTYPE.OPT
    ]
    return min(ttls) if ttls else default
