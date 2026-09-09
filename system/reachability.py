r"""Can anything on this machine actually reach our resolver?

Binding a socket proves nothing. A VPN client can hold a packet filter that drops the
query before it is delivered, and then every visible sign is green -- the socket is bound,
``netstat`` lists it, the NRPT rule is in place and effective -- while the browser says the
name does not exist. That is not hypothetical; it is what sing-box does.

**What sing-box's ``strict_route`` installs.** A WFP filter at
``FWPM_LAYER_ALE_AUTH_CONNECT_V4``, weight 10, action BLOCK, with exactly one condition:

.. code-block:: text

    FWPM_CONDITION_IP_REMOTE_PORT == 53

There is no address condition, so loopback is caught with everything else, and a connect to
``127.0.0.1:53`` fails with ``WSAEACCES``. Two permits sit above it: weight 13 for
sing-box's own ``ALE_APP_ID``, and **weight 11 for its own interface index**.

That second permit is the way through, and it is not a trick played on the VPN -- it is the
VPN's own rule. Traffic on the tunnel interface is allowed to use port 53. The tunnel's
local address belongs to this machine, so a socket bound there receives normally, and the
DNS client reaching it is doing something the filter explicitly permits.

Hence the two functions here: ask whether the ordinary address is filtered, and if it is,
find the address of the interface that outbound traffic leaves by.

Note that this is a general answer, not a sing-box-specific hack. Any leak-prevention
filter written the same way -- block port 53, permit our own interface -- is handled by the
same two probes, and a machine with no such filter never reaches the second one.
"""

from __future__ import annotations

import contextlib
import errno
import socket

#: Windows' "forbidden by its access permissions". A packet filter refused the connection;
#: an ordinary closed port answers with a reset instead.
WSAEACCES = 10013

PROBE_TIMEOUT = 0.5

#: TEST-NET-1 (RFC 5737). Never routed to a real host, so connecting a datagram socket to
#: it only consults the routing table -- no packet is sent, and nothing is contacted.
ROUTE_PROBE_HOST = "192.0.2.1"

#: Deliberately not 53: the probe must not be judged by the very filter we are testing for.
ROUTE_PROBE_PORT = 9


def filtered(address: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """True when a packet filter forbids reaching ``address:port`` from this machine.

    A TCP connect is used because it gives a verdict rather than a silence: a filtered port
    fails immediately with a permission error, a closed one is refused, and an open one
    connects. UDP would only time out, which is indistinguishable from a busy resolver.

    Works whether or not we are already listening there -- the filter is consulted on the
    client's connect, not on the server's bind, so a bound socket does not mask it.
    """
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((address, port))
    except OSError as exc:
        # On Windows the WSA code arrives as .winerror; POSIX uses errno. Checking both
        # keeps this honest on a Linux box behind an nftables rule that rejects with EPERM.
        if getattr(exc, "winerror", None) == WSAEACCES:
            return True
        return exc.errno in (errno.EACCES, errno.EPERM)
    else:
        return False
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def outbound_address() -> str:
    """The local address that outbound traffic would leave by, or "" if that is unknowable.

    Connecting a datagram socket sends nothing; it only makes the kernel choose a route and
    bind a local address, which is then readable. That address belongs to whichever
    interface holds the default route -- the tunnel, when a VPN is up, which is exactly the
    interface such a VPN permits port 53 on.
    """
    try:
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as probe:
            probe.connect((ROUTE_PROBE_HOST, ROUTE_PROBE_PORT))
            found = probe.getsockname()[0]
    except OSError:
        return ""
    return "" if found.startswith("127.") or found == "0.0.0.0" else found


def bypass_for(address: str, port: int) -> str:
    """An extra address to listen on when ``address`` is filtered, or "" when it is not.

    Returns nothing at all on a machine with no such filter, which is the ordinary case:
    the first probe answers "not filtered" and the second is never reached.
    """
    if not filtered(address, port):
        return ""
    candidate = outbound_address()
    if not candidate or candidate == address:
        return ""
    if filtered(candidate, port):
        # The filter is not the interface-scoped kind. Nothing here can help, and saying so
        # is better than binding an address that will not work either.
        return ""
    return candidate
