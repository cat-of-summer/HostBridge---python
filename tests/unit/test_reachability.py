"""Telling "nothing is listening" apart from "a filter refuses to let you through".

The distinction is the whole point of the module. A machine behind a VPN with DNS-leak
protection binds its socket, shows it in ``netstat``, registers its resolution rules, and
answers nothing -- because the query is dropped before delivery. Every visible sign is
green, so the only way to notice is to ask the question this module asks.
"""

from __future__ import annotations

import errno
import socket

from system import reachability


class _Boom(OSError):
    """An OSError with the winerror attribute Windows sockets carry."""

    def __init__(self, errno_value: int, winerror: int | None = None) -> None:
        super().__init__(errno_value, "probe")
        if winerror is not None:
            self.winerror = winerror


def _with_connect(monkeypatch, outcome):
    """Replace the probe's connect with a scripted one."""

    class _Socket:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def settimeout(self, _value):
            pass

        def connect(self, _target):
            if isinstance(outcome, BaseException):
                raise outcome

        def close(self):
            pass

    monkeypatch.setattr(reachability.socket, "socket", _Socket)


# ---- what counts as filtered ---------------------------------------------------------


def test_a_permission_error_is_a_filter(monkeypatch):
    """WSAEACCES on connect is a packet filter, not a closed port."""
    _with_connect(monkeypatch, _Boom(errno.EACCES, reachability.WSAEACCES))
    assert reachability.filtered("127.0.0.1", 53) is True


def test_the_windows_code_alone_is_enough(monkeypatch):
    """Some builds map the errno to something else but always carry winerror."""
    _with_connect(monkeypatch, _Boom(errno.ENOENT, reachability.WSAEACCES))
    assert reachability.filtered("127.0.0.1", 53) is True


def test_a_posix_reject_rule_also_counts(monkeypatch):
    """An nftables reject-with-prohibited surfaces as EPERM, and means the same thing."""
    _with_connect(monkeypatch, _Boom(errno.EPERM))
    assert reachability.filtered("127.0.0.1", 53) is True


def test_a_refused_connection_is_not_a_filter(monkeypatch):
    """Nothing is listening. Ordinary, and must not trigger a workaround."""
    _with_connect(monkeypatch, _Boom(errno.ECONNREFUSED))
    assert reachability.filtered("127.0.0.1", 53) is False


def test_a_timeout_is_not_a_filter(monkeypatch):
    _with_connect(monkeypatch, TimeoutError())
    assert reachability.filtered("127.0.0.1", 53) is False


def test_a_successful_connect_is_not_a_filter(monkeypatch):
    """True of our own listener: the probe must work while we are already bound."""
    _with_connect(monkeypatch, None)
    assert reachability.filtered("127.0.0.1", 53) is False


# ---- choosing the address to add -----------------------------------------------------


def test_nothing_is_added_when_nothing_is_filtered(monkeypatch):
    """The ordinary machine. The second probe is never even reached."""
    monkeypatch.setattr(reachability, "filtered", lambda *_a, **_k: False)
    monkeypatch.setattr(
        reachability, "outbound_address", lambda: (_ for _ in ()).throw(AssertionError("asked"))
    )
    assert reachability.bypass_for("127.0.0.1", 53) == ""


def test_the_outbound_address_is_used_when_loopback_is_filtered(monkeypatch):
    calls = []

    def _filtered(address, _port, **_kwargs):
        calls.append(address)
        return address == "127.0.0.1"

    monkeypatch.setattr(reachability, "filtered", _filtered)
    monkeypatch.setattr(reachability, "outbound_address", lambda: "172.18.0.1")

    assert reachability.bypass_for("127.0.0.1", 53) == "172.18.0.1"
    assert calls == ["127.0.0.1", "172.18.0.1"], "the candidate must be probed too"


def test_a_candidate_that_is_also_filtered_is_refused(monkeypatch):
    """Then the filter is not the interface-scoped kind and nothing here can help.

    Saying so beats binding an address that will not work either, because a second dead
    listener would make the diagnosis harder rather than easier.
    """
    monkeypatch.setattr(reachability, "filtered", lambda *_a, **_k: True)
    monkeypatch.setattr(reachability, "outbound_address", lambda: "10.0.0.5")
    assert reachability.bypass_for("127.0.0.1", 53) == ""


def test_no_route_means_no_candidate(monkeypatch):
    monkeypatch.setattr(reachability, "filtered", lambda *_a, **_k: True)
    monkeypatch.setattr(reachability, "outbound_address", lambda: "")
    assert reachability.bypass_for("127.0.0.1", 53) == ""


# ---- the route probe -----------------------------------------------------------------


def test_the_route_probe_sends_nothing_and_uses_a_port_that_is_not_53():
    """Connecting a datagram socket only picks a route.

    The port matters: probing on 53 would be judged by the very filter being detected.
    """
    assert reachability.ROUTE_PROBE_PORT != 53
    assert reachability.ROUTE_PROBE_HOST.startswith("192.0.2."), "must be TEST-NET-1"


def test_a_loopback_answer_is_reported_as_no_candidate(monkeypatch):
    """A machine with no route out would otherwise offer loopback as its own workaround."""

    class _Probe:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def connect(self, _target):
            pass

        def getsockname(self):
            return ("127.0.0.1", 9)

        def close(self):
            pass

    monkeypatch.setattr(reachability.socket, "socket", _Probe)
    assert reachability.outbound_address() == ""


def test_an_unroutable_machine_answers_empty(monkeypatch):
    def _explode(*_args, **_kwargs):
        raise OSError(errno.ENETUNREACH, "no route")

    monkeypatch.setattr(reachability.socket, "socket", _explode)
    assert reachability.outbound_address() == ""


def test_the_real_probe_names_a_local_address():
    """Against the actual stack: whatever it returns must be an address of this machine."""
    found = reachability.outbound_address()
    if not found:
        return  # a container with no default route; nothing to assert
    socket.inet_aton(found)
    assert not found.startswith("127.")
