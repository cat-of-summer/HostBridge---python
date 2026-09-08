"""The control API over a real loopback socket.

The authorisation tests are the important half. This application's whole job is convincing
browsers to talk to 127.0.0.1, which makes its own control port an attractive
DNS-rebinding target, so the rules that turn browser-shaped requests away are asserted
individually rather than assumed.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading
import time

import pytest

from core.config import DaemonSettings
from core.model import Domain
from core.store import DomainStore
from core.version import API_VERSION
from daemon import auth
from daemon.policy import Policy, PolicyState
from daemon.runner import Runner

pytestmark = pytest.mark.network


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class QuietPolicy(Policy):
    name = "test"

    def present(self):
        return []

    def apply(self, namespaces, servers):  # noqa: ARG002
        return PolicyState(namespaces=tuple(namespaces))

    def remove(self, state):
        return None

    def remove_all(self):
        return None

    def flush(self) -> bool:
        return True


class Harness:
    """A daemon on its own thread and event loop, driven by ordinary blocking clients.

    The thread is not incidental. The client calls below block, and issuing one from inside
    the loop that also has to answer it deadlocks -- which is exactly what happened when
    this was first written as a set of coroutines. Running the daemon separately is both
    correct and closer to how it really works: two processes, not one.
    """

    def __init__(self) -> None:
        self.store = DomainStore()
        self.store.add(Domain(name="etm39.ru", address="127.0.0.1"))
        self.settings = DaemonSettings(
            listen_address="127.0.0.1",
            listen_port=_free_port(),
            listen_ipv6=False,
            upstreams=["192.0.2.1"],
            traefik_enabled=False,
        )
        self.runner = Runner(self.settings, self.store, QuietPolicy())
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def main() -> None:
            try:
                await self.runner.start()
                self.port = self.runner.api.port
                self.token = self.runner.api.token
            except BaseException as exc:  # noqa: BLE001 - reported to the test thread
                self._failure = exc
                self._ready.set()
                return
            self._ready.set()
            await self.runner._stop.wait()
            await self.runner.stop()

        self.loop.run_until_complete(main())

    def __enter__(self) -> Harness:
        self._thread.start()
        assert self._ready.wait(15), "the daemon did not start"
        if self._failure is not None:
            raise self._failure
        return self

    def __exit__(self, *exc) -> None:
        self.loop.call_soon_threadsafe(self.runner.request_stop)
        self._thread.join(timeout=15)
        self.loop.close()

    # ---- raw requests -------------------------------------------------------------

    def request(self, method: str, path: str, *, headers=None, body=None):
        # Generous on purpose. Five seconds is ample against a loopback daemon but not
        # against a CI runner under load, and a timeout here would look like a server bug
        # rather than the scheduling delay it is.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            sent = {
                "Authorization": f"Bearer {self.token}",
                auth.API_HEADER: str(API_VERSION),
            }
            if headers is not None:
                sent = headers
            if payload is not None:
                sent = {**sent, "Content-Type": "application/json"}
            connection.request(method, path, body=payload, headers=sent)
            response = connection.getresponse()
            raw = response.read()
            parsed = json.loads(raw.decode()) if raw else {}
            return response.status, parsed
        finally:
            connection.close()

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", auth.API_HEADER: str(API_VERSION)}


# ---- authorisation ----------------------------------------------------------------


def test_health_needs_no_token_so_a_version_mismatch_is_diagnosable():
    with Harness() as harness:
        status, payload = harness.request("GET", "/v1/health", headers={})
        assert status == 200
        assert payload["api_version"] == API_VERSION


def test_everything_else_needs_a_token():
    with Harness() as harness:
        for path in ("/v1/status", "/v1/domains"):
            status, _ = harness.request("GET", path, headers={})
            assert status == 401, path


def test_a_wrong_token_is_refused():
    with Harness() as harness:
        status, _ = harness.request("GET", "/v1/status", headers={"Authorization": "Bearer wrong"})
        assert status == 401


def test_any_origin_header_is_refused_even_with_a_valid_token():
    """The DNS-rebinding defence. Not "an origin we dislike" -- any origin at all."""

    with Harness() as harness:
        for origin in ("http://evil.example", "null", "http://127.0.0.1"):
            status, _ = harness.request(
                "GET", "/v1/status", headers={**harness.auth_headers, "Origin": origin}
            )
            assert status == 403, origin


def test_fetch_metadata_headers_are_refused():
    """Catches a no-cors fetch, which carries no Origin but still comes from a browser."""

    with Harness() as harness:
        for header in ("Sec-Fetch-Site", "Sec-Fetch-Mode"):
            status, _ = harness.request(
                "GET", "/v1/status", headers={**harness.auth_headers, header: "cross-site"}
            )
            assert status == 403, header


def test_a_foreign_host_header_is_refused():
    """A rebinding attack arrives with the attacker's hostname in Host."""

    with Harness() as harness:
        status, _ = harness.request(
            "GET", "/v1/status", headers={**harness.auth_headers, "Host": "evil.example"}
        )
        assert status == 403


def test_a_control_api_version_mismatch_is_reported_as_such():
    with Harness() as harness:
        status, payload = harness.request(
            "GET", "/v1/status", headers={**harness.auth_headers, auth.API_HEADER: "999"}
        )
        assert status == 409
        assert "control API" in payload["error"]


def test_the_published_token_is_long_and_random():
    first, second = auth.generate_token(), auth.generate_token()
    assert first != second
    assert len(first) >= 32


# ---- the routes -------------------------------------------------------------------


def test_domains_are_listed():
    with Harness() as harness:
        status, payload = harness.request("GET", "/v1/domains")
        assert status == 200
        assert [d["name"] for d in payload["domains"]] == ["etm39.ru"]


def test_creating_a_domain_makes_it_resolvable_without_a_restart():
    with Harness() as harness:
        status, payload = harness.request(
            "POST", "/v1/domains", body={"name": "shop.test", "address": "127.0.0.9"}
        )
        assert status == 201
        assert payload["domain"]["name"] == "shop.test"

        # The zone the resolver is serving must already know about it.
        assert harness.runner.resolver.zone.lookup("shop.test") == "127.0.0.9"


def test_creating_an_invalid_domain_is_refused_with_the_reason():
    with Harness() as harness:
        status, payload = harness.request("POST", "/v1/domains", body={"name": "localhost"})
        assert status == 400
        assert payload["error"] == "domain.error_single_label"


def test_a_warning_is_returned_but_does_not_block():
    with Harness() as harness:
        status, payload = harness.request("POST", "/v1/domains", body={"name": "shop.local"})
        assert status == 201
        assert any(w["key"] == "domain.warn_mdns" for w in payload["warnings"])


def test_a_duplicate_is_a_conflict():
    with Harness() as harness:
        status, _ = harness.request("POST", "/v1/domains", body={"name": "etm39.ru"})
        assert status == 409


def test_toggling_removes_the_name_from_the_zone():
    with Harness() as harness:
        _, listed = harness.request("GET", "/v1/domains")
        identifier = listed["domains"][0]["id"]

        status, payload = harness.request("POST", f"/v1/domains/{identifier}/toggle")
        assert status == 200
        assert payload["domain"]["enabled"] is False
        # Disabled means "fall through to upstream", so the zone must not answer.
        assert harness.runner.resolver.zone.lookup("etm39.ru") is None


def test_updating_an_address_reaches_the_zone():
    with Harness() as harness:
        _, listed = harness.request("GET", "/v1/domains")
        identifier = listed["domains"][0]["id"]

        harness.request("PATCH", f"/v1/domains/{identifier}", body={"address": "10.1.2.3"})
        assert harness.runner.resolver.zone.lookup("etm39.ru") == "10.1.2.3"


def test_deleting_removes_it_everywhere():
    with Harness() as harness:
        _, listed = harness.request("GET", "/v1/domains")
        identifier = listed["domains"][0]["id"]

        status, _ = harness.request("DELETE", f"/v1/domains/{identifier}")
        assert status == 200
        assert harness.runner.resolver.zone.lookup("etm39.ru") is None
        assert harness.request("DELETE", f"/v1/domains/{identifier}")[0] == 404


def test_an_unknown_route_is_a_404():
    with Harness() as harness:
        assert harness.request("GET", "/v1/nonsense")[0] == 404


def test_status_reports_what_the_daemon_is_doing():
    with Harness() as harness:
        _, payload = harness.request("GET", "/v1/status")
        assert payload["running"] is True
        assert payload["mechanism"] == "test"
        assert payload["claimed"] == ["etm39.ru"]


# ---- daemon.json ------------------------------------------------------------------


def test_the_daemon_publishes_its_port_and_token_and_clears_them_on_exit():
    with Harness() as harness:
        info = auth.read()
        assert info is not None
        assert info.port == harness.port
        assert info.token == harness.token

    assert auth.read() is None, "a stopped daemon must not leave a live-looking token behind"


# ---- the client bridge -------------------------------------------------------------


def test_the_bridge_finds_a_running_daemon_and_can_change_things():
    from client.api import Bridge

    with Harness() as harness:
        bridge = Bridge.connect()
        assert bridge.online

        bridge.add("shop.test", "127.0.0.8")
        snapshot = bridge.snapshot()
        assert {d.name for d in snapshot.domains} == {"etm39.ru", "shop.test"}
        assert harness.runner.resolver.zone.lookup("shop.test") == "127.0.0.8"


def test_without_a_daemon_the_bridge_still_edits_but_reports_the_resolver_as_down():
    """Read-only here would be a regression: editing before starting the resolver is
    ordinary, and a running daemon notices the change through its store watcher anyway.
    What must not happen is pretending the change took effect."""
    from client.api import Bridge

    store = DomainStore()
    store.add(Domain(name="offline.test"))

    bridge = Bridge.connect(store)
    assert not bridge.online

    snapshot = bridge.snapshot()
    assert not snapshot.read_only
    assert snapshot.status == {"running": False}
    assert [d.name for d in snapshot.domains] == ["offline.test"]

    bridge.add("another.test")
    assert {d.name for d in bridge.snapshot().domains} == {"offline.test", "another.test"}


def test_the_bridge_treats_a_stale_daemon_file_as_offline():
    """A daemon.json left by a killed process must not make the bridge look online."""
    from client.api import Bridge

    auth.write(port=_free_port(), token="nobody-is-listening")
    assert not Bridge.connect().online


# ---- server-sent events ------------------------------------------------------------


def test_a_change_is_announced_on_the_event_stream():
    from client.api import Bridge

    with Harness() as harness:
        bridge = Bridge.connect()
        stop = threading.Event()
        received: list[dict] = []

        def _listen() -> None:
            for event in bridge.events(stop):
                received.append(event)
                if event.get("kind") == "domains":
                    return

        listener = threading.Thread(target=_listen, daemon=True)
        listener.start()
        time.sleep(0.5)  # let the stream attach before the change is made

        harness.request("POST", "/v1/domains", body={"name": "streamed.test"})
        listener.join(timeout=5)

        assert any(
            event.get("kind") == "domains" and event.get("name") == "streamed.test"
            for event in received
        )
        stop.set()
