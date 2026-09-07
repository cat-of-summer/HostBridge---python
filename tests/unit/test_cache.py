from __future__ import annotations

from resolver.cache import MAX_NEGATIVE_TTL, MAX_TTL, MIN_TTL, Cache
from resolver.wire import txid, with_txid


class FakeClock:
    """A clock the test drives, so no test ever sleeps to watch a TTL expire."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _response(rcode: int = 0, request_id: int = 0x1234) -> bytes:
    """A 12-octet header, which is all the cache ever inspects."""
    header = bytearray(12)
    header[0:2] = request_id.to_bytes(2, "big")
    header[2] = 0x80  # QR
    header[3] = rcode
    return bytes(header)


def test_a_stored_answer_comes_back_stamped_with_the_asking_id():
    cache = Cache(clock=FakeClock())
    key = ("shop.test", 1, 1, False)

    cache.put(key, _response(request_id=0xAAAA), ttl=30)
    served = cache.get(key, 0x5678)

    assert served is not None
    assert txid(served) == 0x5678, "a cached reply carrying someone else's id is dropped"


def test_the_stored_copy_has_a_zeroed_id():
    cache = Cache(clock=FakeClock())
    key = ("shop.test", 1, 1, False)
    cache.put(key, _response(request_id=0xAAAA), ttl=30)
    assert txid(cache.get(key, 0)) == 0


def test_a_miss_is_reported_and_counted():
    cache = Cache(clock=FakeClock())
    assert cache.get(("nope.test", 1, 1, False), 1) is None
    assert cache.stats.misses == 1
    assert cache.stats.hits == 0


def test_an_entry_expires_on_the_injected_clock():
    clock = FakeClock()
    cache = Cache(clock=clock)
    key = ("shop.test", 1, 1, False)
    cache.put(key, _response(), ttl=10)

    clock.advance(9)
    assert cache.get(key, 1) is not None

    clock.advance(2)
    assert cache.get(key, 1) is None
    assert cache.stats.expired == 1
    assert len(cache) == 0, "an expired entry is dropped rather than left to accumulate"


def test_ttl_is_clamped_at_both_ends():
    clock = FakeClock()
    cache = Cache(clock=clock)

    cache.put(("a.test", 1, 1, False), _response(), ttl=0)
    clock.advance(MIN_TTL - 0.5)
    assert cache.get(("a.test", 1, 1, False), 1) is not None

    cache.put(("b.test", 1, 1, False), _response(), ttl=999999)
    clock.advance(MAX_TTL + 1)
    assert cache.get(("b.test", 1, 1, False), 1) is None


def test_a_failure_response_is_cached_only_briefly():
    """An upstream hiccup must not persist as a stale SERVFAIL after the network returns."""
    clock = FakeClock()
    cache = Cache(clock=clock)
    key = ("shop.test", 1, 1, False)

    cache.put(key, _response(rcode=2), ttl=3600)
    clock.advance(MAX_NEGATIVE_TTL + 1)
    assert cache.get(key, 1) is None


def test_the_dnssec_bit_is_part_of_the_key():
    cache = Cache(clock=FakeClock())
    plain = ("shop.test", 1, 1, False)
    signed = ("shop.test", 1, 1, True)

    cache.put(plain, _response(), ttl=30)
    assert cache.get(signed, 1) is None, "a DNSSEC-aware client must not get a plain answer"


def test_eviction_is_bounded_and_drops_the_oldest():
    cache = Cache(clock=FakeClock(), max_entries=3)
    for index in range(5):
        cache.put((f"d{index}.test", 1, 1, False), _response(), ttl=60)

    assert len(cache) == 3
    assert cache.stats.evicted == 2
    assert cache.get(("d0.test", 1, 1, False), 1) is None
    assert cache.get(("d4.test", 1, 1, False), 1) is not None


def test_clear_empties_the_cache():
    cache = Cache(clock=FakeClock())
    cache.put(("shop.test", 1, 1, False), _response(), ttl=60)
    cache.clear()
    assert len(cache) == 0


def test_with_txid_touches_only_the_first_two_octets():
    original = _response(request_id=0x1111)
    patched = with_txid(original, 0x2222)
    assert patched[2:] == original[2:]
    assert txid(patched) == 0x2222


def test_wire_helpers_tolerate_a_runt_packet():
    assert txid(b"") == 0
    assert with_txid(b"\x01", 5) == b"\x01"
