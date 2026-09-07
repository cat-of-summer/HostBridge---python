"""Byte-level helpers for DNS messages.

Everything here works on the raw packet rather than a parsed object, and that is the point.
A forwarded query goes upstream and comes back **byte for byte** except for its two-octet
transaction id, so EDNS0 options, the DNSSEC DO bit, RRSIG records and record types nobody
has heard of all survive untouched. Re-serialising a message through any library is where a
local dev resolver quietly starts breaking DNSSEC.
"""

from __future__ import annotations

import secrets

#: Header layout: the transaction id is the first two octets, big-endian.
TXID_OFFSET = 0
TXID_LENGTH = 2
HEADER_LENGTH = 12

#: A DNS message over TCP is preceded by its length as a two-octet big-endian integer.
TCP_LENGTH_PREFIX = 2

MAX_UDP_PAYLOAD = 4096


def txid(payload: bytes) -> int:
    """The transaction id of ``payload``. Zero for a message too short to have one."""
    if len(payload) < TXID_OFFSET + TXID_LENGTH:
        return 0
    return int.from_bytes(payload[TXID_OFFSET : TXID_OFFSET + TXID_LENGTH], "big")


def with_txid(payload: bytes, new_id: int) -> bytes:
    """Return ``payload`` with its transaction id replaced and nothing else touched."""
    if len(payload) < TXID_OFFSET + TXID_LENGTH:
        return payload
    head = (new_id & 0xFFFF).to_bytes(TXID_LENGTH, "big")
    return head + payload[TXID_OFFSET + TXID_LENGTH :]


def random_txid() -> int:
    """A fresh id for a query we put on the wire.

    ``secrets`` rather than ``random``: a predictable id is the oldest DNS spoofing
    primitive there is, and this resolver forwards on behalf of a whole machine.
    """
    return secrets.randbits(16)


def is_truncated(payload: bytes) -> bool:
    """Whether the TC bit is set, meaning the answer did not fit in a datagram."""
    if len(payload) < HEADER_LENGTH:
        return False
    return bool(payload[2] & 0x02)


def rcode(payload: bytes) -> int:
    if len(payload) < HEADER_LENGTH:
        return 0
    return payload[3] & 0x0F
